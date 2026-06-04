"""Сохранение входящего сообщения в БД + генерация эмбеддинга.

Разделено на два шага ради надёжности при at-least-once доставке Pub/Sub:
- persist_message — синхронная запись в chat_messages. Выполняется в хендлере
  ДО ack, чтобы сообщение не потерялось при падении воркера.
- embed_message — идемпотентная фоновая задача: создаёт вектор, если его ещё
  нет. Безопасна к повторному запуску — на сбое сетевого вызова embedding
  можно догнать при повторной доставке, не оставляя сообщение без вектора.
"""

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db.models import ChatMessage, ChatMessageEmbedding
from app.db.session import AsyncSessionLocal
from app.logger import logger
from app.schemas.incoming import IncomingMessage

_openai = AsyncOpenAI(api_key=settings.openai_api_key)


async def persist_message(msg: IncomingMessage) -> bool:
    """Сохранить сообщение в chat_messages. Возвращает True, если запись новая.

    False означает дубликат (Pub/Sub at-least-once) — повторно обрабатывать его
    (extraction) не нужно. Идемпотентность даёт on_conflict_do_nothing по
    message_id.
    """
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
        row = (await session.execute(stmt)).first()
        await session.commit()

    if row is None:
        logger.info("ingest_duplicate_skipped", message_id=msg.message_id)
        return False
    return True


async def embed_message(msg: IncomingMessage) -> None:
    """Создать эмбеддинг сообщения, если его ещё нет.

    Запускается фоном на каждое created-событие (в т.ч. при повторной доставке),
    но идемпотентна: если вектор уже есть — выходит сразу. Исключения гасятся
    здесь, чтобы сбой embedding не ронял остальные фоновые задачи.
    """
    try:
        async with AsyncSessionLocal() as session:
            msg_row = (
                await session.execute(
                    select(ChatMessage.id).where(
                        ChatMessage.message_id == msg.message_id
                    )
                )
            ).first()
            if msg_row is None:
                return  # сообщение ещё/уже не существует — нечего эмбеддить
            msg_uuid = msg_row.id
            existing = (
                await session.execute(
                    select(ChatMessageEmbedding.id).where(
                        ChatMessageEmbedding.message_id == msg_uuid
                    )
                )
            ).first()
        if existing is not None:
            return  # вектор уже посчитан — идемпотентность

        response = await _openai.embeddings.create(
            model=settings.openai_embedding_model,
            input=msg.text,
        )
        vector = response.data[0].embedding

        async with AsyncSessionLocal() as session:
            session.add(
                ChatMessageEmbedding(
                    message_id=msg_uuid,
                    embedding=vector,
                    model=settings.openai_embedding_model,
                )
            )
            await session.commit()
        logger.info("ingest_ok", message_id=msg.message_id, chars=len(msg.text))
    except Exception:
        logger.exception("embed_failed", message_id=msg.message_id)
