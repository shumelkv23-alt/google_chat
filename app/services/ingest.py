"""Сохранение входящего сообщения в БД + генерация эмбеддинга."""

from openai import AsyncOpenAI
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db.models import ChatMessage, ChatMessageEmbedding
from app.db.session import AsyncSessionLocal
from app.logger import logger
from app.schemas.incoming import IncomingMessage

_openai = AsyncOpenAI(api_key=settings.openai_api_key)


async def ingest_message(msg: IncomingMessage) -> None:
    async with AsyncSessionLocal() as session:
        stmt = (
            pg_insert(ChatMessage)
            .values(
                message_id=msg.message_id,
                space_id=msg.space_id,
                thread_id=msg.thread_id,
                author_id=msg.author_id,
                author_name=msg.author_name,
                text=msg.text,
                created_at=msg.created_at,
                source=msg.source,
            )
            .on_conflict_do_nothing(index_elements=["message_id"])
            .returning(ChatMessage.id)
        )

        result = await session.execute(stmt)
        row = result.first()
        await session.commit()

    if row is None:
        logger.info("ingest_duplicate_skipped", message_id=msg.message_id)
        return

    response = await _openai.embeddings.create(
        model=settings.openai_embedding_model,
        input=msg.text,
    )
    vector = response.data[0].embedding

    async with AsyncSessionLocal() as session:
        session.add(
            ChatMessageEmbedding(
                message_id=row.id,
                embedding=vector,
                model=settings.openai_embedding_model,
            )
        )
        await session.commit()

    logger.info("ingest_ok", message_id=msg.message_id, chars=len(msg.text))
