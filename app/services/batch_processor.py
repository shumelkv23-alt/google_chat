"""Batch-конвейер: разбор накопленных сообщений пачкой (см. fork.md).

Повар-оркестратор. Берёт необработанные сообщения одного space, готовит для
batch_extractor разметку тредов/цитат, считает эмбеддинги одним вызовом, прогоняет
пачку через LLM и каждый результат — через существующий resolve_and_save, затем
помечает сообщения обработанными.

flush_batch — обработать пачку одного space.
flush_due_batches — найти «созревшие» space (по count или таймауту) и обработать.
"""

import asyncio
import string
from collections import Counter
from datetime import datetime, timezone

from openai import AsyncOpenAI
from sqlalchemy import func, select, text, update

from app.config import settings
from app.db.models import ChatMessage, ChatMessageEmbedding
from app.db.session import AsyncSessionLocal
from app.llm.batch_extractor import BatchItem, extract_batch
from app.logger import logger
from app.schemas.incoming import IncomingMessage
from app.services.entity_resolution import find_anchor_vacancy, resolve_and_save
from app.services.extraction import run_extraction
from app.services.ingest import embed_message

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

# Один процесс + последовательный тикер: глобального лока достаточно, чтобы два
# флаша не пересеклись. Для нескольких воркеров позже добавить FOR UPDATE SKIP
# LOCKED в _fetch_pending (см. fork.md, раздел «Окружение»).
_lock = asyncio.Lock()

_OPEN_VACANCIES_LIMIT = 30


async def _fetch_pending(space_id: str) -> list[dict]:
    """Необработанные живые сообщения space в хронопорядке, не больше BATCH_SIZE."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    ChatMessage.id,
                    ChatMessage.message_id,
                    ChatMessage.space_id,
                    ChatMessage.thread_id,
                    ChatMessage.author_id,
                    ChatMessage.author_name,
                    ChatMessage.text,
                    ChatMessage.created_at,
                    ChatMessage.source,
                    ChatMessage.quoted_message_id,
                )
                .where(
                    ChatMessage.space_id == space_id,
                    ChatMessage.is_processed.is_(False),
                    ChatMessage.is_deleted.is_(False),
                )
                .order_by(ChatMessage.created_at)
                .limit(settings.batch_size)
            )
        ).fetchall()
    return [dict(r._mapping) for r in rows]


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


async def _build_markup(batch: list[dict]) -> list[dict]:
    """Подготовить сообщения для extract_batch: индексы + разметка тредов/цитат.

    Тред-метку (A/B/…) даём только тредам, встречающимся в пачке ≥2 раз (есть что
    группировать). Цитату в пачке резолвим в reply_to_index; цитату вне пачки —
    подтягиваем текст процитированного из БД в reply_to_text.
    """
    by_id = {m["message_id"]: i for i, m in enumerate(batch)}
    thread_counts = Counter(m["thread_id"] for m in batch if m["thread_id"])

    external = [
        m["quoted_message_id"]
        for m in batch
        if m["quoted_message_id"] and m["quoted_message_id"] not in by_id
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
        reply_to_text = quoted_texts.get(qid) if qid and reply_to_index is None else None

        result.append(
            {
                "message_index": i,
                "author_name": m["author_name"],
                "text": m["text"],
                "thread_label": label,
                "reply_to_index": reply_to_index,
                "reply_to_text": reply_to_text,
            }
        )
    return result


def _label(n: int) -> str:
    return string.ascii_uppercase[n] if n < len(string.ascii_uppercase) else f"T{n}"


async def _fetch_open_vacancies() -> list[dict]:
    """Открытые вакансии как контекст для экстрактора (самая свежая помечена _latest).

    Прямой SQL: проще, чем тащить модель Vacancy ради 7 полей контекста.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id::text, title, status, team,
                           salary_min, salary_max, currency
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
    if vacancies:
        vacancies[0]["_latest"] = True  # самая свежая — частый адресат follow-up'ов
    return vacancies


async def _apply_items(batch: list[dict], items: list[BatchItem]) -> None:
    """Каждый результат экстрактора — через resolve_and_save (как в per-message)."""
    for item in items:
        idx = item.message_index
        if not (0 <= idx < len(batch)):
            logger.warning(
                "batch_item_index_out_of_range", index=idx, batch_size=len(batch)
            )
            continue
        if item.action == "none":
            continue
        row = batch[idx]
        incoming = IncomingMessage(
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
        try:
            await resolve_and_save(incoming, item)
        except Exception:
            logger.exception("batch_resolve_error", message_id=row["message_id"])


async def _mark_processed(ids: list) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(ChatMessage).where(ChatMessage.id.in_(ids)).values(is_processed=True)
        )
        await session.commit()


async def flush_batch(space_id: str) -> None:
    """Обработать одну пачку space: эмбеддинги → LLM → резолвер → пометить."""
    async with _lock:
        try:
            batch = await _fetch_pending(space_id)
            if not batch:
                return
            await _embed_batch(batch)
            messages = await _build_markup(batch)
            open_vacancies = await _fetch_open_vacancies()
            items = await extract_batch(messages, open_vacancies)
            await _apply_items(batch, items)
            await _mark_processed([m["id"] for m in batch])
            logger.info(
                "batch_flushed",
                space_id=space_id,
                messages=len(batch),
                actions=sum(1 for i in items if i.action != "none"),
            )
        except Exception:
            # Пачку не помечаем обработанной — переедет в следующий заход.
            logger.exception("batch_flush_error", space_id=space_id)


async def flush_due_batches() -> None:
    """Найти space с созревшей пачкой (count≥BATCH_SIZE или старше таймаута)."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    ChatMessage.space_id,
                    func.count().label("cnt"),
                    func.min(ChatMessage.created_at).label("oldest"),
                )
                .where(
                    ChatMessage.is_processed.is_(False),
                    ChatMessage.is_deleted.is_(False),
                )
                .group_by(ChatMessage.space_id)
            )
        ).fetchall()

    now = datetime.now(timezone.utc)
    for r in rows:
        age = (now - r.oldest).total_seconds()
        if r.cnt >= settings.batch_size or age >= settings.batch_timeout_seconds:
            logger.info(
                "batch_due", space_id=r.space_id, pending=r.cnt, oldest_age_s=int(age)
            )
            await flush_batch(r.space_id)


async def mark_processed_message(message_id: str) -> None:
    """Пометить одно сообщение обработанным (для онлайн-ветки batch-режима)."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(ChatMessage)
            .where(ChatMessage.message_id == message_id)
            .values(is_processed=True)
        )
        await session.commit()


async def route_created_batch(msg: IncomingMessage) -> None:
    """Онлайн-ветка batch-режима для нового сообщения.

    Есть структурная привязка к уже существующей вакансии (цитата/тред) →
    обрабатываем сразу старым путём (точно, без ожидания пачки) и помечаем
    обработанным. Иначе — оставляем сообщение пачке (эмбеддинг посчитает батч).
    """
    try:
        anchor_id, via = await find_anchor_vacancy(msg)
        if anchor_id is None:
            return  # нет привязки → ждёт пачку
        await embed_message(msg)
        await run_extraction(msg)
        await mark_processed_message(msg.message_id)
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
