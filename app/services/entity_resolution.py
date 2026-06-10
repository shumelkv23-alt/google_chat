import json
import uuid
from datetime import datetime, timezone

from openai import AsyncOpenAI
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db.models import ChatMessage, Vacancy, VacancyRevision
from app.db.session import AsyncSessionLocal
from app.llm.extractor import ExtractionResult
from app.llm.posting_classifier import classify_new_posting
from app.llm.resolver import ResolutionResult, resolve_entity
from app.logger import logger
from app.schemas.incoming import IncomingMessage
from app.services.posting_heuristic import is_new_posting

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

_SIMILARITY_THRESHOLD = 0.75
# Если первичный отбор пуст, для update/close пробуем top-5 без threshold.
_FALLBACK_LIMIT = 5

_CONFIDENCE_MIN = 0.6


_FIELD_MAP = {
    "title": "title",
    "salary_min": "salary_min",
    "salary_max": "salary_max",
    "currency": "currency",
    "owner": "owner_name",
    "team": "team",
    "description": "description",
}



# LLM-вердикту ниже этого порога не доверяем — падаем на regex.
_POSTING_CONFIDENCE_MIN = 0.6


async def _decide_new_posting(text: str, matched: dict | None) -> bool:

    regex_hit = is_new_posting(text)
    verdict = await classify_new_posting(text, matched)
    if verdict.confidence >= _POSTING_CONFIDENCE_MIN:
        return verdict.new_posting
    return regex_hit


def _inherit_identity(result: ExtractionResult, matched: dict | None) -> ExtractionResult:
    """Дополнить новый найм идентичностью найденного дубля (title/team).

    new_posting — отдельный найм на ту же роль, но extractor в update-режиме
    отдаёт лишь изменённые условия (зарплата, уровень) и роняет title/team —
    без этого новая запись теряет роль и компанию. Берём их из матча. Поля,
    заданные сообщением явно, не трогаем (result.fields в приоритете).
    """
    if not matched:
        return result
    inherited = {
        key: matched[key]
        for key in ("title", "team")
        if key not in result.fields and matched.get(key) is not None
    }
    if not inherited:
        return result
    return result.model_copy(update={"fields": {**inherited, **result.fields}})


async def find_anchor_vacancy(
    msg: IncomingMessage,
) -> tuple[uuid.UUID | None, str | None]:
    """Вакансия, к которой сообщение привязано структурно (сильнее эмбеддинга).

    Пользователь сам указал, к чему относится follow-up:
      1) quoted reply (цитата) → вакансия процитированного сообщения;
      2) reply в тред, уже принадлежащий вакансии.
    Цитата приоритетнее треда — явное указание на конкретное сообщение.

    Возвращает (vacancy_id, via): via ∈ {"quoted","thread"} для логов; (None, None)
    если структурной привязки к существующей вакансии нет. Этим же helper'ом
    batch-маршрутизатор решает «обработать сразу или отложить в пачку».
    """
    if msg.quoted_message_id:
        anchor_id = await _find_quoted_vacancy(msg.quoted_message_id)
        if anchor_id is not None:
            return anchor_id, "quoted"
    if msg.thread_id:
        anchor_id = await _find_thread_vacancy(msg.thread_id)
        if anchor_id is not None:
            return anchor_id, "thread"
    return None, None


async def resolve_and_save(msg: IncomingMessage, result: ExtractionResult) -> None:
    """Главный вход: reply в тред вакансии → прямая привязка; иначе embed + резолвер."""
    if result.action == "none":
        logger.info("resolution_skipped_none", message_id=msg.message_id)
        return

    msg_uuid = await _get_message_uuid(msg.message_id)

    # Прямая привязка по структурному сигналу (цитата/тред) — сильнее
    # эмбеддинга/резолвера, т.к. пользователь сам указал, к чему относится
    # follow-up. Логика вынесена в find_anchor_vacancy (ею же пользуется
    # batch-маршрутизатор).
    anchor_id, via = await find_anchor_vacancy(msg)

    if anchor_id is not None:
        # create поверх существующей вакансии — это дополнение, а не дубль.
        effective_action = "update" if result.action == "create" else result.action
        async with AsyncSessionLocal() as session:
            async with session.begin():
                # embedding=None: entity_ref экстрактора тут ненадёжен — вектор не трогаем.
                title = await _update_vacancy(
                    session, anchor_id, result, effective_action, None, msg_uuid
                )
        logger.info(
            "resolution_anchor_match",
            message_id=msg.message_id,
            vacancy=title,
            action=effective_action,
            via=via,
        )
        return

    entity_ref = result.entity_ref or result.fields.get("title", "")
    if not entity_ref:
        logger.info(
            "resolution_skipped_no_ref", message_id=msg.message_id, action=result.action
        )
        return

    ref_vector = await _embed(entity_ref)

    candidates = await _find_candidates(ref_vector, threshold=_SIMILARITY_THRESHOLD)

    
    if not candidates and result.action in ("update", "close"):
        candidates = await _find_candidates(ref_vector, threshold=None)
        if candidates:
            logger.info(
                "resolution_fallback_candidates",
                message_id=msg.message_id,
                n=len(candidates),
            )

    # 3) Нет кандидатов вообще → либо создаём новую, либо логируем промах.
    #    Явное объявление о найме создаём, даже если extractor ошибочно дал
    #    update (иначе сообщение молча терялось бы как no_candidates_for_update).
    if not candidates:
        if result.action == "create" or await _decide_new_posting(msg.text, None):
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    await _create_vacancy(session, result, ref_vector, msg_uuid, msg)
            logger.info("resolution_created_new", message_id=msg.message_id)
        else:
            logger.warning(
                "resolution_no_candidates_for_update",
                message_id=msg.message_id,
                action=result.action,
                entity_ref=entity_ref,
            )
        return

    # 4) Есть кандидаты — спрашиваем LLM-резолвер.
    llm: ResolutionResult = await resolve_entity(entity_ref, candidates, msg.text)

    confident = bool(llm.vacancy_id) and llm.confidence >= _CONFIDENCE_MIN
    matched = next((c for c in candidates if c["id"] == llm.vacancy_id), None)
    # Резолвер сказал «к какой записи относится», но не «новый это найм или правка».
    # Похожая открытая роль матчится в обоих случаях — намерение решает связка
    # regex + LLM, сверяясь с самой найденной вакансией.
    new_posting = confident and await _decide_new_posting(msg.text, matched)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            if new_posting:
                # Уверенный матч на похожую позицию, но это ОТДЕЛЬНЫЙ найм на ту же
                # роль, а не правка. Создаём новую, найденную не трогаем. title/team
                # наследуем из дубля — extractor в update-режиме их роняет.
                await _create_vacancy(
                    session, _inherit_identity(result, matched), ref_vector, msg_uuid, msg
                )
                logger.info(
                    "resolution_new_posting_created",
                    message_id=msg.message_id,
                    near_duplicate_id=llm.vacancy_id,
                )
            elif confident:
                # Резолвер уверенно матчит → апдейтим существующую.
                # Если extractor сказал "create", переписываем на "update":
                # это не дубль, а дополнение к уже существующей записи.
                effective_action = "update" if result.action == "create" else result.action
                if effective_action != result.action:
                    logger.info(
                        "resolution_create_to_update",
                        message_id=msg.message_id,
                        matched_vacancy_id=llm.vacancy_id,
                    )
                await _update_vacancy(
                    session,
                    uuid.UUID(llm.vacancy_id),
                    result,
                    effective_action,
                    ref_vector,
                    msg_uuid,
                )
            elif not llm.vacancy_id and result.action == "create":
                # Резолвер уверенно сказал "это новая позиция" → создаём.
                await _create_vacancy(session, result, ref_vector, msg_uuid, msg)
            elif llm.vacancy_id:
                # Матч есть, но уверенности мало → пишем pending для разбора.
                await _add_revision(
                    session,
                    vacancy_id=uuid.UUID(llm.vacancy_id),
                    action="pending",
                    changed_field=None,
                    old_value=None,
                    new_value=json.dumps(result.fields, ensure_ascii=False),
                    source_message_id=msg_uuid,
                    confidence=llm.confidence,
                )
                logger.info(
                    "resolution_pending_low_confidence",
                    message_id=msg.message_id,
                    confidence=llm.confidence,
                )
            else:
                # Резолвер не нашёл матч, action=update/close → промах.
                logger.warning(
                    "resolution_unmatched_update",
                    message_id=msg.message_id,
                    action=result.action,
                    entity_ref=entity_ref,
                )

    logger.info(
        "resolution_done",
        message_id=msg.message_id,
        action=result.action,
        entity_ref=entity_ref,
    )


async def _add_revision(session, **values) -> None:
    """INSERT ревизии с защитой от дубля по (source_message_id, action).

    Идемпотентность: повторная доставка/обработка того же сообщения не создаёт
    вторую ревизию того же действия (unique partial index из миграции 0002).
    """
    stmt = (
        pg_insert(VacancyRevision)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["source_message_id", "action"],
            index_where=text("source_message_id IS NOT NULL"),
        )
    )
    await session.execute(stmt)


async def _embed(textual: str) -> list[float]:
    resp = await _openai.embeddings.create(
        model=settings.openai_embedding_model, input=textual
    )
    return resp.data[0].embedding


async def _get_message_uuid(message_id: str) -> uuid.UUID | None:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(ChatMessage.id).where(ChatMessage.message_id == message_id)
            )
        ).scalar_one_or_none()


async def _find_quoted_vacancy(quoted_message_id: str) -> uuid.UUID | None:
    """Вакансия процитированного сообщения (quoted reply).

    Пользователь ответил цитатой на конкретное сообщение — это явное указание, к
    чему относится follow-up. Берём живую не-закрытую вакансию, к которой привязано
    процитированное сообщение (по самой свежей его ревизии).
    """
    sql = text(
        """
        SELECT vr.vacancy_id
        FROM vacancy_revisions vr
        JOIN chat_messages cm ON cm.id = vr.source_message_id
        JOIN vacancies v ON v.id = vr.vacancy_id
        WHERE cm.message_id = :qmid
          AND v.is_deleted = false
          AND v.status != 'closed'
        ORDER BY vr.created_at DESC
        LIMIT 1
        """
    )
    async with AsyncSessionLocal() as session:
        row = (await session.execute(sql, {"qmid": quoted_message_id})).first()
    return row[0] if row else None


async def _find_thread_vacancy(thread_id: str) -> uuid.UUID | None:
    """Вакансия, к которой уже привязан тред (по source-сообщениям её ревизий).

    Reply в тред — сильнейший сигнал принадлежности к позиции. Берём самую свежую
    по ревизиям живую не-закрытую вакансию, чьи ревизии ссылаются на сообщения
    этого треда.
    """
    sql = text(
        """
        SELECT vr.vacancy_id
        FROM vacancy_revisions vr
        JOIN chat_messages cm ON cm.id = vr.source_message_id
        JOIN vacancies v ON v.id = vr.vacancy_id
        WHERE cm.thread_id = :thread_id
          AND v.is_deleted = false
          AND v.status != 'closed'
        GROUP BY vr.vacancy_id
        ORDER BY MAX(vr.created_at) DESC
        LIMIT 1
        """
    )
    async with AsyncSessionLocal() as session:
        row = (await session.execute(sql, {"thread_id": thread_id})).first()
    return row[0] if row else None


async def _find_candidates(
    vector: list[float], threshold: float | None
) -> list[dict]:
    """Top-5 open-вакансий по cosine. С threshold=None — без отсечки по similarity.
    """
    vec_str = "[" + ",".join(str(x) for x in vector) + "]"
    where_threshold = (
        "AND 1 - (embedding <=> CAST(:vec AS vector(1536))) > :threshold"
        if threshold is not None
        else ""
    )
    sql = text(
        f"""
        SELECT id::text, title, description, status, team,
               salary_min, salary_max, currency, owner_name
        FROM vacancies
        WHERE status != 'closed'
          AND embedding IS NOT NULL
          AND is_deleted = false
          {where_threshold}
        ORDER BY embedding <=> CAST(:vec AS vector(1536))
        LIMIT :limit
        """
    )
    params: dict = {"vec": vec_str, "limit": _FALLBACK_LIMIT}
    if threshold is not None:
        params["threshold"] = threshold
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).fetchall()
    return [dict(r._mapping) for r in rows]


async def _create_vacancy(
    session,
    result: ExtractionResult,
    embedding: list[float],
    msg_uuid: uuid.UUID | None,
    msg: IncomingMessage,
) -> None:
    fields = result.fields
    vac = Vacancy(
        title=fields.get("title") or result.entity_ref,
        status=fields.get("status", "open"),
        salary_min=fields.get("salary_min"),
        salary_max=fields.get("salary_max"),
        currency=fields.get("currency", "RUB"),
        owner_id=msg.author_id,
        owner_name=fields.get("owner") or msg.author_name,
        team=fields.get("team"),
        description=fields.get("description"),
        last_message_id=msg_uuid,
        embedding=embedding,
        confidence=result.confidence,
    )
    session.add(vac)
    await session.flush()  # нужен vac.id для ревизии

    await _add_revision(
        session,
        vacancy_id=vac.id,
        action="create",
        changed_field=None,
        old_value=None,
        new_value=json.dumps(fields, ensure_ascii=False),
        source_message_id=msg_uuid,
        confidence=result.confidence,
    )
    return vac.title


async def _update_vacancy(
    session,
    vacancy_id: uuid.UUID,
    result: ExtractionResult,
    effective_action: str,
    embedding: list[float] | None,
    msg_uuid: uuid.UUID | None,
) -> str:
    """Применить правку к вакансии. Возвращает её title (для читаемых логов)."""
    vac = (
        await session.execute(select(Vacancy).where(Vacancy.id == vacancy_id))
    ).scalar_one()

    old_vals: dict = {}
    new_vals: dict = {}

    for ext_key, vac_attr in _FIELD_MAP.items():
        if ext_key not in result.fields:
            continue
        new_val = result.fields[ext_key]
        old_val = getattr(vac, vac_attr)
        if old_val != new_val:
            old_vals[vac_attr] = old_val
            new_vals[vac_attr] = new_val
            setattr(vac, vac_attr, new_val)

    if effective_action == "close":
        if vac.status != "closed":
            old_vals["status"] = vac.status
            new_vals["status"] = "closed"
            vac.status = "closed"

    vac.last_message_id = msg_uuid
    vac.confidence = result.confidence
    # Обновляем embedding только если позиция активна И вектор передан. При close
    # сохраняем старый (нужен для close-ревизии); при thread-match embedding=None
    # (entity_ref мог быть мусорным) — тоже не трогаем.
    if effective_action != "close" and embedding is not None:
        vac.embedding = embedding
    vac.updated_at = datetime.now(timezone.utc)

    changed = ", ".join(new_vals.keys()) or None
    await _add_revision(
        session,
        vacancy_id=vacancy_id,
        action=effective_action,
        changed_field=changed,
        old_value=(
            json.dumps(old_vals, ensure_ascii=False, default=str)
            if old_vals
            else None
        ),
        new_value=(
            json.dumps(new_vals, ensure_ascii=False, default=str)
            if new_vals
            else None
        ),
        source_message_id=msg_uuid,
        confidence=result.confidence,
    )
