"""Фоновая задача: pre-filter + extraction + entity resolution."""

from openai import AsyncOpenAI
from sqlalchemy import select, text

from app.config import settings
from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal
from app.llm.extractor import extract_vacancy
from app.llm.prefilter import is_vacancy_message
from app.logger import logger
from app.schemas.incoming import IncomingMessage
from app.services.entity_resolution import resolve_and_save

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

_CONTEXT_LIMIT = 7
_EXTRACTOR_SPACE_FALLBACK = 3
_OPEN_VACANCIES_K = 3


async def _fetch_recent(*, scope: str, scope_id: str, exclude_msg_id: str) -> list[dict]:
    """Последние N сообщений по scope ('thread'|'space'), кроме текущего, в хронологическом порядке."""
    column = ChatMessage.thread_id if scope == "thread" else ChatMessage.space_id
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChatMessage.author_name, ChatMessage.text)
                .where(column == scope_id, ChatMessage.message_id != exclude_msg_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(_CONTEXT_LIMIT)
            )
        ).fetchall()
    return [{"author_name": r.author_name, "text": r.text} for r in reversed(rows)]


async def _build_contexts(msg: IncomingMessage) -> tuple[list[dict], list[dict]]:
    """Возвращает (extractor_context, prefilter_context).

    Если есть содержательный тред — используем его (точный контекст).
    Если треда нет (Google Chat в threaded-режиме каждое «новое» сообщение
    кладёт в свой тред — типичная ситуация для follow-up'ов вида «зп от 2000»),
    даём extractor'у узкое окно последних сообщений из space — лучше так,
    чем извлекать поля из голого текста без контекста.
    """
    if msg.thread_id:
        thread_ctx = await _fetch_recent(
            scope="thread", scope_id=msg.thread_id, exclude_msg_id=msg.message_id
        )
        if thread_ctx:
            return thread_ctx, thread_ctx

    space_ctx = await _fetch_recent(
        scope="space", scope_id=msg.space_id, exclude_msg_id=msg.message_id
    )
    return space_ctx[-_EXTRACTOR_SPACE_FALLBACK:], space_ctx


async def _get_open_vacancies(query: str) -> list[dict]:
    """Top-K открытых вакансий из БД по cosine с запросом.

    Даётся extractor'у как «вот что сейчас открыто в фирме». Закрывает кейс
    длинных обсуждений, когда упоминание вакансии давно вышло из окна контекста.
    """
    resp = await _openai.embeddings.create(
        model=settings.openai_embedding_model, input=query
    )
    vec_str = "[" + ",".join(str(x) for x in resp.data[0].embedding) + "]"
    sql = text(
        """
        SELECT title, status, team, salary_min, salary_max, currency
        FROM vacancies
        WHERE status != 'closed' AND embedding IS NOT NULL AND is_deleted = false
        ORDER BY embedding <=> CAST(:vec AS vector(1536))
        LIMIT :k
        """
    )
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(sql, {"vec": vec_str, "k": _OPEN_VACANCIES_K})
        ).fetchall()
    return [dict(r._mapping) for r in rows]


async def run_extraction(msg: IncomingMessage) -> None:
    try:
        extractor_ctx, prefilter_ctx = await _build_contexts(msg)

        if not await is_vacancy_message(msg.text):
            if not prefilter_ctx or not await is_vacancy_message(msg.text, prefilter_ctx):
                logger.info("extraction_skipped_not_vacancy", message_id=msg.message_id)
                return

        open_vacancies = await _get_open_vacancies(msg.text)

        result = await extract_vacancy(
            text=msg.text,
            author_name=msg.author_name,
            created_at=msg.created_at.isoformat(),
            context_messages=extractor_ctx or None,
            open_vacancies=open_vacancies or None,
        )

        logger.info(
            "extraction_result",
            message_id=msg.message_id,
            action=result.action,
            entity_ref=result.entity_ref,
            fields=result.fields,
            confidence=result.confidence,
            open_vacancies_count=len(open_vacancies),
        )

        if result.action != "none":
            await resolve_and_save(msg, result)
    except Exception:
        logger.exception("extraction_pipeline_error", message_id=msg.message_id)
