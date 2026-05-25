"""Фоновая задача: пре-фильтр + extraction для входящего сообщения."""

from app.llm.extractor import extract_vacancy
from app.llm.prefilter import is_vacancy_message
from app.logger import logger
from app.schemas.incoming import IncomingMessage


async def run_extraction(msg: IncomingMessage) -> None:
    is_vacancy = await is_vacancy_message(msg.text)

    if not is_vacancy:
        logger.info("extraction_skipped_not_vacancy", message_id=msg.message_id)
        return

    result = await extract_vacancy(
        text=msg.text,
        author_name=msg.author_name,
        created_at=msg.created_at.isoformat(),
    )

    logger.info(
        "extraction_result",
        message_id=msg.message_id,
        action=result.action,
        entity_ref=result.entity_ref,
        fields=result.fields,
        confidence=result.confidence,
    )
