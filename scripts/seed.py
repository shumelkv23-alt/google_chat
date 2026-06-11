"""Засеять chat_messages тестовыми сообщениями про вакансии.

Полезно для отладки RAG/extraction без живого Pub/Sub.

Запуск:
    python -m scripts.seed
"""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal, engine
from app.services.hashing import sha256_hex

SEED_MESSAGES = [
    "Открыли вакансию питониста, до 300k на руки, ищем под команду Платформа",
    "По питонисту подняли вилку до 350",
    "Закрыли позицию Go-разработчика в Биллинг",
    "У нас есть джун-фронт? Реакт, до 150",
    "Привет, как дела",  # мусорное, для проверки pre-filter
    "Ищем тимлида в команду Платформа, опыт от 5 лет",
    "Senior DevOps, Kubernetes обязателен, до 400k",
    "Закрыли вакансию питониста — нашли кандидата",
]


async def _run() -> None:
    async with AsyncSessionLocal() as session:
        # Очистим прошлые seed-записи
        await session.execute(
            text("DELETE FROM chat_messages WHERE message_id LIKE 'seed-%'")
        )

        base_time = datetime.now(timezone.utc) - timedelta(days=2)
        for i, msg_text in enumerate(SEED_MESSAGES):
            session.add(
                ChatMessage(
                    message_id=f"seed-{i + 1}",
                    space_id="spaces/AAQAmmGOtCo",
                    thread_id=None,
                    author_id="users/seed-author",
                    author_name="Seed Author",
                    text=msg_text,
                    text_hash=sha256_hex(msg_text),
                    created_at=base_time + timedelta(hours=i),
                    source="chat_a",
                )
            )
        await session.commit()

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT message_id, text FROM chat_messages "
                "WHERE message_id LIKE 'seed-%' "
                "ORDER BY created_at"
            )
        )
        for row in result.all():
            print(f"  {row.message_id}: {row.text}")

    print(f"\nseeded {len(SEED_MESSAGES)} messages")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
