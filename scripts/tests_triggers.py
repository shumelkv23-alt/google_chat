"""Тест триггеров пачки: count (>=BATCH_SIZE) и timeout (старше таймаута).

Запуск:
    python -m scripts.test_triggers
"""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.config import settings
from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal, engine
from app.logger import setup_logging
from app.services.hashing import sha256_hex
from app.services.batch_processor import flush_due_batches

SPACE = "spaces/AAQAmmGOtCo"


async def _pending() -> int:
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM chat_messages WHERE space_id = :sp "
                    "AND is_processed = false AND is_deleted = false"
                ),
                {"sp": SPACE},
            )
        ).scalar()


async def _seed(prefix: str, n: int, age_seconds: int) -> None:
    created = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    async with AsyncSessionLocal() as s:
        await s.execute(
            text("DELETE FROM chat_messages WHERE message_id LIKE :p"),
            {"p": f"{prefix}%"},
        )
        for i in range(n):
            s.add(
                ChatMessage(
                    message_id=f"{prefix}{i}",
                    space_id=SPACE,
                    thread_id=None,
                    author_id="users/seed-author",
                    author_name="Seed Author",
                    text=f"Открыли вакансию инженера №{i}, {100 + i}k",
                    text_hash=sha256_hex(f"Открыли вакансию инженера №{i}, {100 + i}k"),
                    created_at=created + timedelta(seconds=i),
                    source="chat_a",
                )
            )
        await s.commit()


async def _run() -> None:
    setup_logging()
    print(f"BATCH_SIZE={settings.batch_size}, TIMEOUT={settings.batch_timeout_seconds}s\n")

    # COUNT-триггер: BATCH_SIZE свежих (таймаут НЕ прошёл)
    await _seed("seed-cnt-", settings.batch_size, age_seconds=0)
    print(f"COUNT:   засеяно {settings.batch_size} свежих, pending={await _pending()}")
    await flush_due_batches()
    print(f"  после flush: pending={await _pending()}  (ждём 0 — count>=BATCH_SIZE)\n")

    # TIMEOUT-триггер: 1 старое (count < BATCH_SIZE)
    old = settings.batch_timeout_seconds + 60
    await _seed("seed-tmo-", 1, age_seconds=old)
    print(f"TIMEOUT: засеяно 1 старое ({old}s назад), pending={await _pending()}")
    await flush_due_batches()
    print(f"  после flush: pending={await _pending()}  (ждём 0 — timeout, хоть 1 < BATCH_SIZE)")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
