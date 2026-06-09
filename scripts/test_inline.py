"""Тест онлайн-ветки batch-режима (конвейер 2).

Симулирует реальный поток: новое сообщение-reply к УЖЕ существующей вакансии
(тред Scala из seed_threads) должно обработаться СРАЗУ, а не ждать пачку.

Запуск:
    python -m scripts.test_inline
"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine
from app.logger import setup_logging
from app.schemas.incoming import IncomingMessage
from app.services.batch_processor import route_created_batch
from app.services.ingest import persist_message

SPACE = "spaces/AAQAmmGOtCo"
THREAD = "spaces/AAQAmmGOtCo/threads/scala-T1"  # тред вакансии Scala


async def _is_processed(message_id: str):
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(
                text("SELECT is_processed FROM chat_messages WHERE message_id = :m"),
                {"m": message_id},
            )
        ).scalar()


async def _run() -> None:
    setup_logging()
    msg = IncomingMessage(
        message_id="seed-inline-1",
        space_id=SPACE,
        thread_id=THREAD,  # тот же тред, что у Scala-вакансии
        author_id="users/seed-author",
        author_name="Seed Author",
        text="по Scala апаем до 300k",  # follow-up без названия
        created_at=datetime.now(timezone.utc),
    )

    # как в реальном потоке: сперва persist (синхронно), потом маршрутизация
    await persist_message(msg)
    print("после persist:              is_processed =", await _is_processed(msg.message_id),
          "(ждём False — ещё не обработано)")

    await route_created_batch(msg)
    print("после route_created_batch:  is_processed =", await _is_processed(msg.message_id),
          "(ждём True — обработано онлайн, пачку не ждёт)")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
