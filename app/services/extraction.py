"""Фоновая задача: пре-фильтр + extraction + entity resolution."""

from sqlalchemy import select, text

from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal
from app.llm.extractor import extract_vacancy
from app.llm.prefilter import is_vacancy_message
from app.logger import logger
from app.schemas.incoming import IncomingMessage
from app.services.entity_resolution import resolve_and_save

_CONTEXT_LIMIT = 10  # сколько предыдущих сообщений из пространства тянуть


async def _get_space_context(space_id: str, current_message_id: str) -> list[dict]:
    """Последние N сообщений из пространства, кроме текущего."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ChatMessage.author_name, ChatMessage.text)
                .where(
                    ChatMessage.space_id == space_id,
                    ChatMessage.message_id != current_message_id,
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(_CONTEXT_LIMIT)
            )
        ).fetchall()
    # возвращаем в хронологическом порядке (старые → новые)
    return [{"author_name": r.author_name, "text": r.text} for r in reversed(rows)]


async def run_extraction(msg: IncomingMessage) -> None:
    try:
        # Быстрая проверка без контекста
        if await is_vacancy_message(msg.text):
            context = None
        else:
            # Сообщение само по себе не выглядит как вакансия —
            # тянем контекст пространства и проверяем повторно
            context = await _get_space_context(msg.space_id, msg.message_id)
            if not context or not await is_vacancy_message(msg.text, context):
                logger.info("extraction_skipped_not_vacancy", message_id=msg.message_id)
                return

        result = await extract_vacancy(
            text=msg.text,
            author_name=msg.author_name,
            created_at=msg.created_at.isoformat(),
            context_messages=context,
        )

        logger.info(
            "extraction_result",
            message_id=msg.message_id,
            action=result.action,
            entity_ref=result.entity_ref,
            fields=result.fields,
            confidence=result.confidence,
        )

        if result.action != "none":
            await resolve_and_save(msg, result)
    except Exception:
        logger.exception("extraction_pipeline_error", message_id=msg.message_id)
