"""Обработка редактирования и удаления сообщений (этап 8).

- handle_edit: правит текст, помечает is_edited, пересчитывает embedding и state.
- handle_delete: мягко удаляет сообщение (is_deleted) и — если оно было единственным
  источником вакансии — мягко удаляет и вакансию.
"""

import uuid

from openai import AsyncOpenAI
from sqlalchemy import delete, select, text

from app.config import settings
from app.db.models import ChatMessage, ChatMessageEmbedding
from app.db.session import AsyncSessionLocal
from app.logger import logger
from app.schemas.incoming import IncomingMessage
from app.services.extraction import run_extraction

_openai = AsyncOpenAI(api_key=settings.openai_api_key)


async def handle_edit(msg: IncomingMessage) -> None:
    """MESSAGE_UPDATED: обновить text + is_edited, пересчитать embedding и extraction."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ChatMessage).where(ChatMessage.message_id == msg.message_id)
            )
        ).scalar_one_or_none()
        if row is None:
            # Сообщение мы не сохраняли (например, было не про вакансию) — нечего править.
            logger.info("edit_unknown_message", message_id=msg.message_id)
            return
        row.text = msg.text
        row.is_edited = True
        msg_uuid = row.id
        await session.commit()

    # Пересчёт embedding: старый вектор больше не соответствует тексту.
    response = await _openai.embeddings.create(
        model=settings.openai_embedding_model, input=msg.text
    )
    vector = response.data[0].embedding
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(ChatMessageEmbedding).where(
                ChatMessageEmbedding.message_id == msg_uuid
            )
        )
        session.add(
            ChatMessageEmbedding(
                message_id=msg_uuid,
                embedding=vector,
                model=settings.openai_embedding_model,
            )
        )
        await session.commit()

    # Пересчёт state: новый текст может менять вакансию. Дубль-ревизии отсекает
    # unique(source_message_id, action) + on_conflict_do_nothing в resolver.
    await run_extraction(msg)
    logger.info("edit_done", message_id=msg.message_id)


async def handle_delete(msg: IncomingMessage) -> None:
    """MESSAGE_DELETED: soft-delete сообщения + soft-delete вакансии, если источник единственный."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ChatMessage).where(ChatMessage.message_id == msg.message_id)
            )
        ).scalar_one_or_none()
        if row is None:
            logger.info("delete_unknown_message", message_id=msg.message_id)
            return
        row.is_deleted = True
        msg_uuid = row.id
        await session.commit()

    await _soft_delete_orphan_vacancies(msg_uuid, msg.message_id)
    logger.info("delete_done", message_id=msg.message_id)


async def _soft_delete_orphan_vacancies(
    msg_uuid: uuid.UUID, message_id: str
) -> None:
    """Мягко удалить вакансии, чьим единственным живым источником было это сообщение."""
    async with AsyncSessionLocal() as session:
        vacancy_ids = (
            await session.execute(
                text(
                    "SELECT DISTINCT vacancy_id FROM vacancy_revisions "
                    "WHERE source_message_id = :mid"
                ),
                {"mid": msg_uuid},
            )
        ).scalars().all()

        for vid in vacancy_ids:
            # Ревизии от ещё живых (не удалённых) сообщений. Текущее сообщение уже
            # помечено is_deleted, поэтому в счёт «живых» оно не попадёт.
            live_sources = (
                await session.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM vacancy_revisions vr
                        JOIN chat_messages cm ON cm.id = vr.source_message_id
                        WHERE vr.vacancy_id = :vid AND cm.is_deleted = false
                        """
                    ),
                    {"vid": vid},
                )
            ).scalar_one()

            if live_sources == 0:
                await session.execute(
                    text("UPDATE vacancies SET is_deleted = true WHERE id = :vid"),
                    {"vid": vid},
                )
                logger.info(
                    "vacancy_soft_deleted",
                    vacancy_id=str(vid),
                    source_message_id=message_id,
                )
            else:
                logger.info(
                    "vacancy_source_deleted_kept",
                    vacancy_id=str(vid),
                    live_sources=live_sources,
                )
        await session.commit()
