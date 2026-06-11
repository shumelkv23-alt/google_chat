"""Batch-конвейер v2: разбор накопленных сообщений пачкой (см. batch_mode_v2.md).

Повар-оркестратор. Берёт необработанные сообщения одного space (тредами,
в пределах токен-бюджета), готовит разметку тредов/цитат + read-only историю,
считает эмбеддинги одним вызовом, прогоняет пачку через LLM и применяет
результаты: структурные связи (link_to_index / link_to_vacancy_ref) — напрямую,
остальное — через resolve_and_save. Помечает обработанным УСЛОВНО: только
сообщения, не изменившиеся и не удалённые за время флаша (B1/B2).

Устойчивость: счётчик flush_attempts, бисекция ядовитой пачки, dead-letter
(process_status='failed'). Триггер флаша — quiet window + предохранители
(count и max-age). Lock — per space, с пропуском тика вместо ожидания.

flush_batch — обработать пачку одного space.
flush_due_batches — найти «созревшие» space и обработать.
"""

import asyncio
import string
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from uuid import uuid4

from openai import AsyncOpenAI
from sqlalchemy import exists, func, select, text, tuple_, update

from app.config import settings
from app.db.models import ChatMessage, ChatMessageEmbedding
from app.db.session import AsyncSessionLocal
from app.llm.batch_extractor import BatchItem, extract_batch
from app.logger import logger
from app.schemas.incoming import IncomingMessage
from app.services.entity_resolution import (
    apply_to_vacancy,
    find_anchor_vacancy,
    resolve_and_save,
    verify_open_vacancy,
)
from app.services.extraction import run_extraction
from app.services.hashing import sha256_hex
from app.services.ingest import embed_message

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

# Lock per space: медленный флаш одного чата не блокирует остальные (B12).
# Внутрипроцессного лока достаточно для одного воркера; мультиворкер — через
# claimed_at (поле уже в схеме, механика в batch_mode_v2.md, этап 7.3).
_space_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

_OPEN_VACANCIES_LIMIT = 30


# --------------------------------------------------------------------------
# Выборка пачки: дозабор тредов + токен-бюджет (B6)
# --------------------------------------------------------------------------

_PENDING_COLUMNS = (
    ChatMessage.id,
    ChatMessage.message_id,
    ChatMessage.space_id,
    ChatMessage.thread_id,
    ChatMessage.author_id,
    ChatMessage.author_name,
    ChatMessage.text,
    ChatMessage.text_hash,
    ChatMessage.flush_attempts,
    ChatMessage.created_at,
    ChatMessage.source,
    ChatMessage.quoted_message_id,
)


async def _fetch_pending(space_id: str) -> list[dict]:
    """Необработанные живые сообщения space в хронопорядке.

    После основной выборки (LIMIT BATCH_SIZE) добирает хвосты попавших тредов —
    тред атомарен, резать его по LIMIT значит терять родителя follow-up'а (B6).
    Затем режет результат по токен-бюджету (по границе треда).
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(*_PENDING_COLUMNS)
                .where(
                    ChatMessage.space_id == space_id,
                    ChatMessage.process_status == "pending",
                    ChatMessage.is_deleted.is_(False),
                )
                .order_by(ChatMessage.created_at)
                .limit(settings.batch_size)
            )
        ).fetchall()
        batch = [dict(r._mapping) for r in rows]

        thread_ids = {m["thread_id"] for m in batch if m["thread_id"]}
        if thread_ids and len(batch) == settings.batch_size:
            got = {m["id"] for m in batch}
            extra = (
                await session.execute(
                    select(*_PENDING_COLUMNS)
                    .where(
                        ChatMessage.space_id == space_id,
                        ChatMessage.process_status == "pending",
                        ChatMessage.is_deleted.is_(False),
                        ChatMessage.thread_id.in_(thread_ids),
                        ChatMessage.id.notin_(got),
                    )
                    .order_by(ChatMessage.created_at)
                )
            ).fetchall()
            batch += [dict(r._mapping) for r in extra]
            batch.sort(key=lambda m: m["created_at"])

    return _cap_by_tokens(batch)


def _estimate_tokens(text_: str | None) -> int:
    # Грубая оценка достаточна — важен порядок величины, не точность.
    return len(text_ or "") // 3


def _cap_by_tokens(
    batch: list[dict], budget: int | None = None
) -> list[dict]:
    """Обрезать пачку по токен-бюджету, не разрывая тред (B6 / этап 4.2).

    Группа — подряд идущие сообщения одного thread_id либо одиночное сообщение.
    Первая группа берётся всегда (иначе огромный тред заклинит очередь).
    Незабранный остаток остаётся pending — его возьмёт следующий тик.
    """
    if budget is None:
        budget = settings.batch_token_budget
    out: list[dict] = []
    used = 0
    i = 0
    while i < len(batch):
        j = i + 1
        tid = batch[i]["thread_id"]
        if tid:
            while j < len(batch) and batch[j]["thread_id"] == tid:
                j += 1
        group = batch[i:j]
        cost = sum(_estimate_tokens(m["text"]) for m in group)
        if out and used + cost > budget:
            break  # остаток уйдёт в следующий тик
        out += group
        used += cost
        i = j
    return out


def _split_on_thread_boundary(batch: list[dict]) -> tuple[list[dict], list[dict]]:
    """Разрезать пачку пополам по границе тредов (бисекция, этап 6).

    Если граница рядом с серединой не находится (вся пачка — один тред),
    режем посередине как есть: изоляция ядовитого сообщения важнее
    целостности контекста уже сломанной пачки.
    """
    mid = len(batch) // 2

    def is_boundary(k: int) -> bool:
        return not (
            batch[k]["thread_id"]
            and batch[k]["thread_id"] == batch[k - 1]["thread_id"]
        )

    j = mid
    while j < len(batch) and not is_boundary(j):
        j += 1
    if j >= len(batch):
        j = mid
        while j > 0 and not is_boundary(j):
            j -= 1
    if j <= 0 or j >= len(batch):
        j = mid
    return batch[:j], batch[j:]


# --------------------------------------------------------------------------
# Эмбеддинги и разметка
# --------------------------------------------------------------------------


async def _embed_batch(batch: list[dict]) -> None:
    """Посчитать эмбеддинги сообщений пачки одним вызовом OpenAI (идемпотентно)."""
    ids = [m["id"] for m in batch]
    async with AsyncSessionLocal() as session:
        existing = set(
            (
                await session.execute(
                    select(ChatMessageEmbedding.message_id).where(
                        ChatMessageEmbedding.message_id.in_(ids)
                    )
                )
            ).scalars()
        )
    todo = [m for m in batch if m["id"] not in existing]
    if not todo:
        return
    resp = await _openai.embeddings.create(
        model=settings.openai_embedding_model, input=[m["text"] for m in todo]
    )
    async with AsyncSessionLocal() as session:
        for m, item in zip(todo, resp.data):
            session.add(
                ChatMessageEmbedding(
                    message_id=m["id"],
                    embedding=item.embedding,
                    model=settings.openai_embedding_model,
                )
            )
        await session.commit()


async def _build_markup(
    batch: list[dict], history_labels: dict[str, str] | None = None
) -> list[dict]:
    """Подготовить сообщения для extract_batch: индексы + разметка тредов/цитат.

    Тред-метку (A/B/…) даём только тредам, встречающимся в пачке ≥2 раз (есть что
    группировать). Цитату резолвим по силе сигнала: в пачке → reply_to_index;
    в обработанной истории → reply_to_history ([hN] — у него видна и привязка
    к вакансии); иначе подтягиваем текст процитированного из БД в reply_to_text.
    """
    history_labels = history_labels or {}
    by_id = {m["message_id"]: i for i, m in enumerate(batch)}
    thread_counts = Counter(m["thread_id"] for m in batch if m["thread_id"])

    external = [
        m["quoted_message_id"]
        for m in batch
        if m["quoted_message_id"]
        and m["quoted_message_id"] not in by_id
        and m["quoted_message_id"] not in history_labels
    ]
    quoted_texts: dict[str, str] = {}
    if external:
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(ChatMessage.message_id, ChatMessage.text).where(
                        ChatMessage.message_id.in_(external)
                    )
                )
            ).fetchall()
        quoted_texts = {r.message_id: r.text for r in rows}

    thread_label: dict[str, str] = {}
    result: list[dict] = []
    for i, m in enumerate(batch):
        tid = m["thread_id"]
        label = None
        if tid and thread_counts[tid] >= 2:
            if tid not in thread_label:
                thread_label[tid] = _label(len(thread_label))
            label = thread_label[tid]

        qid = m["quoted_message_id"]
        reply_to_index = by_id.get(qid) if qid else None
        reply_to_history = (
            history_labels.get(qid) if qid and reply_to_index is None else None
        )
        reply_to_text = (
            quoted_texts.get(qid)
            if qid and reply_to_index is None and reply_to_history is None
            else None
        )

        result.append(
            {
                "message_index": i,
                "author_name": m["author_name"],
                "text": m["text"],
                "thread_label": label,
                "reply_to_index": reply_to_index,
                "reply_to_history": reply_to_history,
                "reply_to_text": reply_to_text,
            }
        )
    return result


def _label(n: int) -> str:
    return string.ascii_uppercase[n] if n < len(string.ascii_uppercase) else f"T{n}"


# --------------------------------------------------------------------------
# Контекст: открытые вакансии (vN) + хвост обработанной истории (hN)
# --------------------------------------------------------------------------


async def _fetch_open_vacancies() -> tuple[list[dict], dict[str, uuid.UUID]]:
    """Открытые вакансии как контекст экстрактора + карта меток vN → id.

    Метка vN короткая и не галлюцинируется так легко, как UUID: LLM возвращает
    link_to_vacancy_ref="v2", резолв — точный lookup по карте (B8), с
    верификацией verify_open_vacancy на применении.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id::text, title, status, team,
                           salary_min, salary_max, currency, location
                    FROM vacancies
                    WHERE status != 'closed' AND is_deleted = false
                    ORDER BY created_at DESC
                    LIMIT :lim
                    """
                ),
                {"lim": _OPEN_VACANCIES_LIMIT},
            )
        ).fetchall()
    vacancies = [dict(r._mapping) for r in rows]
    ref_map: dict[str, uuid.UUID] = {}
    for i, v in enumerate(vacancies):
        v["ref"] = f"v{i + 1}"
        ref_map[v["ref"]] = uuid.UUID(v["id"])
    if vacancies:
        vacancies[0]["_latest"] = True  # самая свежая — частый адресат follow-up'ов
    return vacancies, ref_map


async def _fetch_history_tail(
    space_id: str, ref_by_vacancy_id: dict[str, str]
) -> tuple[list[dict], dict[str, str]]:
    """Хвост обработанной истории space с привязками к вакансиям (B7).

    Возвращает (история с метками hN в хронопорядке, карта message_id → hN).
    Follow-up через границу пачек резолвится подстановкой ссылки из истории,
    а не эмбеддинг-поиском с порогом 0.75.
    """
    sql = text(
        """
        SELECT m.message_id, m.author_name, m.text, m.created_at,
               v.id::text AS vacancy_id, v.title AS vacancy_title
        FROM chat_messages m
        LEFT JOIN LATERAL (
            SELECT r.vacancy_id
            FROM vacancy_revisions r
            WHERE r.source_message_id = m.id
            ORDER BY r.created_at DESC
            LIMIT 1
        ) rv ON true
        LEFT JOIN vacancies v ON v.id = rv.vacancy_id AND v.is_deleted = false
        WHERE m.space_id = :space_id
          AND m.process_status = 'processed'
          AND m.is_deleted = false
          AND m.created_at > now() - :hours * interval '1 hour'
        ORDER BY m.created_at DESC
        LIMIT :tail
        """
    )
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                sql,
                {
                    "space_id": space_id,
                    "hours": settings.history_hours,
                    "tail": settings.history_tail,
                },
            )
        ).fetchall()

    history: list[dict] = []
    labels: dict[str, str] = {}
    for n, r in enumerate(reversed(rows)):  # в промпт — хронологически
        label = f"h{n}"
        labels[r.message_id] = label
        history.append(
            {
                "label": label,
                "author_name": r.author_name,
                "text": r.text,
                "created_at": r.created_at,
                "vacancy_ref": (
                    ref_by_vacancy_id.get(r.vacancy_id) if r.vacancy_id else None
                ),
                "vacancy_title": r.vacancy_title,
            }
        )
    return history, labels


# --------------------------------------------------------------------------
# Применение результатов (B8: связи пачки → БД напрямую)
# --------------------------------------------------------------------------


async def _fetch_fresh_state(msg_pk: uuid.UUID) -> dict | None:
    """Текущие text_hash/is_deleted сообщения — перепроверка гонки перед apply."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ChatMessage.text_hash, ChatMessage.is_deleted).where(
                    ChatMessage.id == msg_pk
                )
            )
        ).first()
    return dict(row._mapping) if row else None


def _incoming_from_row(row: dict) -> IncomingMessage:
    return IncomingMessage(
        message_id=row["message_id"],
        space_id=row["space_id"],
        thread_id=row["thread_id"],
        author_id=row["author_id"],
        author_name=row["author_name"],
        text=row["text"],
        created_at=row["created_at"],
        source=row["source"],
        event_type="created",
        quoted_message_id=row["quoted_message_id"],
    )


async def _apply_one(
    row: dict,
    item: BatchItem,
    index_to_vacancy: dict[int, uuid.UUID],
    ref_map: dict[str, uuid.UUID],
) -> uuid.UUID | None:
    """Применить один item. Приоритет привязки: связь внутри пачки →
    прямая ссылка на вакансию → старый путь (якорь/эмбеддинг) как fallback."""
    incoming = _incoming_from_row(row)

    vacancy_id: uuid.UUID | None = None
    if item.link_to_index is not None:
        # Цепочки работают ([4]→[2]→[0]): items применяются в порядке
        # message_index, к моменту [4] карта уже содержит [2].
        vacancy_id = index_to_vacancy.get(item.link_to_index)

    if vacancy_id is None and item.link_to_vacancy_ref:
        candidate = ref_map.get(item.link_to_vacancy_ref.strip())
        # Верификация обязательна: галлюцинированный/закрытый ref тихо
        # деградирует в следующий уровень резолва, а не падает.
        if candidate is not None and await verify_open_vacancy(candidate):
            vacancy_id = candidate

    if vacancy_id is not None:
        return await apply_to_vacancy(vacancy_id, incoming, item)

    # Структурного сигнала нет — старый путь (якорь цитаты/треда внутри
    # resolve_and_save, дальше эмбеддинг entity_ref) остаётся fallback'ом.
    return await resolve_and_save(incoming, item)


async def _apply_items(
    batch: list[dict],
    items: list[BatchItem],
    snapshot: dict[uuid.UUID, str],
    ref_map: dict[str, uuid.UUID],
    batch_id: uuid.UUID,
) -> set[uuid.UUID]:
    """Применить items по одному; вернуть id успешно применённых сообщений.

    Упавший item не роняет пачку (B11): он остаётся pending с инкрементом
    flush_attempts, остальные применяются и помечаются. Перед каждым item —
    перепроверка text_hash/is_deleted: правка или удаление, пришедшие за время
    LLM-вызова, пропускают item (B1/B2, окно сужено до миллисекунд).
    """
    ok: set[uuid.UUID] = set()
    index_to_vacancy: dict[int, uuid.UUID] = {}
    for item in sorted(items, key=lambda it: it.message_index):
        row = batch[item.message_index]
        if item.action == "none":
            # Осознанный none — честный processed (условный mark ещё сверит hash).
            ok.add(row["id"])
            continue
        fresh = await _fetch_fresh_state(row["id"])
        if (
            fresh is None
            or fresh["is_deleted"]
            or fresh["text_hash"] != snapshot[row["id"]]
        ):
            logger.info(
                "batch_item_skipped_stale",
                message_id=row["message_id"],
                batch_id=str(batch_id),
            )
            continue
        try:
            vacancy_id = await _apply_one(row, item, index_to_vacancy, ref_map)
            if vacancy_id is not None:
                index_to_vacancy[item.message_index] = vacancy_id
            ok.add(row["id"])
        except Exception:
            logger.exception(
                "batch_apply_item_failed",
                message_id=row["message_id"],
                batch_id=str(batch_id),
            )
            await _bump_flush_attempts([row["id"]])
    return ok


# --------------------------------------------------------------------------
# Условный mark_processed (B1/B2) + учёт неудач (B10)
# --------------------------------------------------------------------------


async def _mark_processed_conditional(
    snapshot: dict[uuid.UUID, str], ok_ids: set[uuid.UUID]
) -> None:
    """Пометить processed только сообщения, что (а) успешно применены,
    (б) не изменились с момента fetch (text_hash совпал), (в) не удалены.
    Не прошедшие проверку остаются pending — следующий тик возьмёт их со
    свежим текстом и переразберёт."""
    pairs = [(mid, snapshot[mid]) for mid in ok_ids if mid in snapshot]
    if not pairs:
        return
    # Один UPDATE по парам (id, text_hash): сообщение помечается, только если его
    # содержимое не изменилось с момента fetch. tuple_-IN вместо executemany —
    # ORM-executemany по сущности уходит в bulk-update-by-PK и конфликтует с
    # дополнительным WHERE. synchronize_session=False: ORM identity map здесь
    # не используется, синхронизировать нечего.
    stmt = (
        update(ChatMessage)
        .where(
            tuple_(ChatMessage.id, ChatMessage.text_hash).in_(pairs),
            ChatMessage.is_deleted.is_(False),
        )
        .values(process_status="processed", is_processed=True)
        .execution_options(synchronize_session=False)
    )
    async with AsyncSessionLocal() as session:
        await session.execute(stmt)
        await session.commit()


async def _bump_flush_attempts(message_pks: list[uuid.UUID]) -> None:
    if not message_pks:
        return
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(ChatMessage)
            .where(ChatMessage.id.in_(message_pks))
            .values(flush_attempts=ChatMessage.flush_attempts + 1)
        )
        await session.commit()


async def _dead_letter(row: dict) -> None:
    """Терминальный статус failed: ядовитое сообщение выведено из очереди (B10).

    Возврат в работу — правка текста человеком (handle_edit_batch сбрасывает
    статус в pending и обнуляет flush_attempts).
    """
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(ChatMessage)
            .where(ChatMessage.id == row["id"])
            .values(process_status="failed")
        )
        await session.commit()
    logger.error(
        "batch_message_dead_lettered",
        message_id=row["message_id"],
        attempts=row["flush_attempts"],
        text_preview=(row["text"] or "")[:200],
    )


# --------------------------------------------------------------------------
# Флаш
# --------------------------------------------------------------------------


async def flush_batch(space_id: str) -> None:
    """Обработать пачку space. Lock per space; занятый space — пропуск тика (B12).

    Пачка с сообщением, пережившим BISECT_THRESHOLD неудач, делится пополам по
    границе тредов: здоровая половина проходит, больная делится дальше — за
    log2(N) тиков ядовитое сообщение изолируется до одиночного (B10).
    """
    lock = _space_locks[space_id]
    if lock.locked():
        logger.info("batch_flush_skipped_locked", space_id=space_id)
        return
    async with lock:
        batch = await _fetch_pending(space_id)
        if not batch:
            return
        max_attempts = max(m["flush_attempts"] for m in batch)
        if len(batch) > 1 and max_attempts >= settings.bisect_threshold:
            left, right = _split_on_thread_boundary(batch)
            logger.info(
                "batch_bisect", space_id=space_id, left=len(left), right=len(right)
            )
            await _flush_concrete(space_id, left)
            await _flush_concrete(space_id, right)
            return
        await _flush_concrete(space_id, batch)


async def _flush_concrete(space_id: str, batch: list[dict]) -> None:
    """Флаш конкретного набора сообщений: embed → markup+контекст → LLM →
    применение → условный mark. batch_id сквозной в логах флаша (этап 9)."""
    if not batch:
        return
    if (
        len(batch) == 1
        and batch[0]["flush_attempts"] >= settings.fail_threshold
    ):
        await _dead_letter(batch[0])
        return

    batch_id = uuid4()
    # Снапшот версий ДО любой работы: с ним сверяемся перед apply и при mark.
    snapshot = {m["id"]: m["text_hash"] for m in batch}
    try:
        await _embed_batch(batch)
        vacancies, ref_map = await _fetch_open_vacancies()
        ref_by_vacancy_id = {str(vid): ref for ref, vid in ref_map.items()}
        history, history_labels = await _fetch_history_tail(
            space_id, ref_by_vacancy_id
        )
        messages = await _build_markup(batch, history_labels)
        items = await extract_batch(messages, vacancies, history)

        # Полное покрытие индексов (B9): пропущенный LLM индекс — не «none»,
        # а ошибка; сообщение остаётся pending и уходит на повторный флаш.
        covered = {it.message_index for it in items}
        missing = sorted(set(range(len(batch))) - covered)
        if missing:
            await _bump_flush_attempts([batch[i]["id"] for i in missing])
            logger.warning(
                "batch_coverage_missing",
                space_id=space_id,
                batch_id=str(batch_id),
                missing=missing,
            )

        ok_ids = await _apply_items(batch, items, snapshot, ref_map, batch_id)
        await _mark_processed_conditional(snapshot, ok_ids)
        logger.info(
            "batch_flushed",
            space_id=space_id,
            batch_id=str(batch_id),
            messages=len(batch),
            processed=len(ok_ids),
            missing=len(missing),
            actions=sum(1 for it in items if it.action != "none"),
        )
    except Exception:
        # Пачка не помечена — переедет в следующий заход. Инкремент попыток
        # всем: стабильно ломающаяся пачка дозреет до бисекции, а не зациклится.
        logger.exception(
            "batch_flush_error", space_id=space_id, batch_id=str(batch_id)
        )
        try:
            await _bump_flush_attempts([m["id"] for m in batch])
        except Exception:
            logger.exception("batch_bump_attempts_error", space_id=space_id)


async def flush_due_batches() -> None:
    """Найти space с созревшей пачкой. Триггер трёхусловный (B13):

    1) cnt >= BATCH_SIZE — защита от распухания;
    2) разговор затих на QUIET_SECONDS — основной триггер: пачки совпадают со
       всплесками разговора, родственные сообщения оказываются вместе;
    3) oldest старше BATCH_TIMEOUT_SECONDS — жёсткий потолок: одинокое
       сообщение не висит дольше потолка даже при непрерывном фоновом трёпе.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    ChatMessage.space_id,
                    func.count().label("cnt"),
                    func.min(ChatMessage.created_at).label("oldest"),
                    func.max(ChatMessage.created_at).label("last_msg_at"),
                )
                .where(
                    ChatMessage.process_status == "pending",
                    ChatMessage.is_deleted.is_(False),
                )
                .group_by(ChatMessage.space_id)
            )
        ).fetchall()

    now = datetime.now(timezone.utc)
    for r in rows:
        oldest_age = (now - r.oldest).total_seconds()
        quiet_for = (now - r.last_msg_at).total_seconds()
        if (
            r.cnt >= settings.batch_size
            or quiet_for >= settings.quiet_seconds
            or oldest_age >= settings.batch_timeout_seconds
        ):
            logger.info(
                "batch_due",
                space_id=r.space_id,
                pending=r.cnt,
                oldest_age_s=int(oldest_age),
                quiet_s=int(quiet_for),
            )
            await flush_batch(r.space_id)


# --------------------------------------------------------------------------
# Онлайн-ветка
# --------------------------------------------------------------------------


async def mark_processed_message(msg: IncomingMessage) -> None:
    """Условно пометить одно сообщение обработанным (онлайн-ветка).

    Та же страховка, что у пачки: если текст успели править или сообщение
    удалили, пометка не происходит — переразберёт пачка.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(ChatMessage)
            .where(
                ChatMessage.message_id == msg.message_id,
                ChatMessage.text_hash == sha256_hex(msg.text),
                ChatMessage.is_deleted.is_(False),
            )
            .values(process_status="processed", is_processed=True)
        )
        await session.commit()


async def route_created_batch(msg: IncomingMessage) -> None:
    """Онлайн-ветка batch-режима для нового сообщения.

    Есть структурная привязка к существующей вакансии (цитата/тред) →
    обрабатываем сразу старым путём (точно, без ожидания пачки). Но если в том
    же треде ждёт пачки более СТАРОЕ сообщение — уходим в пачку вместе с ним:
    цельный разбор треда одним LLM-взглядом важнее секунд отклика (этап 8,
    страховка хронологии B3). Иначе — оставляем сообщение пачке.
    """
    try:
        anchor_id, via = await find_anchor_vacancy(msg)
        if anchor_id is None:
            return  # нет привязки → ждёт пачку

        if msg.thread_id:
            async with AsyncSessionLocal() as session:
                older_pending = await session.scalar(
                    select(
                        exists().where(
                            ChatMessage.thread_id == msg.thread_id,
                            ChatMessage.process_status == "pending",
                            ChatMessage.is_deleted.is_(False),
                            ChatMessage.created_at < msg.created_at,
                        )
                    )
                )
            if older_pending:
                logger.info(
                    "batch_online_deferred", message_id=msg.message_id, via=via
                )
                return  # уходим в пачку вместе со старшим соседом по треду

        await embed_message(msg)
        await run_extraction(msg)
        await mark_processed_message(msg)
        logger.info("batch_inline_resolved", message_id=msg.message_id, via=via)
    except Exception:
        logger.exception("batch_route_created_error", message_id=msg.message_id)


async def batch_ticker() -> None:
    """Фоновый цикл batch-режима: раз в BATCH_POLL_SECONDS разгребает созревшее."""
    logger.info("batch_ticker_started", poll_s=settings.batch_poll_seconds)
    try:
        while True:
            await asyncio.sleep(settings.batch_poll_seconds)
            try:
                await flush_due_batches()
            except Exception:
                logger.exception("batch_ticker_iteration_error")
    except asyncio.CancelledError:
        logger.info("batch_ticker_stopped")
        raise
