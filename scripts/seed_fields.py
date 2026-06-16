
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal, engine
from app.services.hashing import sha256_hex

SPACE = "spaces/AAQAmmGOtCo"
THREAD = "spaces/AAQAmmGOtCo/threads/java-fields"

# (message_id, text, thread_id)
SEED = [
    (
        "seed-fld-1",
        "Открыли Java-разработчика в Москве, офис. Нужен английский B2, "
        "есть ДМС и помощь с релокацией, до 350k",
        THREAD,
    ),
    ("seed-fld-2", "по джаве теперь гибрид 3/2", THREAD),
]


async def _run() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM chat_messages WHERE message_id LIKE 'seed-fld-%'")
        )
        base = datetime.now(timezone.utc) - timedelta(days=2)
        for i, (mid, txt, thread) in enumerate(SEED):
            session.add(
                ChatMessage(
                    message_id=mid,
                    space_id=SPACE,
                    thread_id=thread,
                    author_id="users/seed-author",
                    author_name="Seed Author",
                    text=txt,
                    text_hash=sha256_hex(txt),
                    created_at=base + timedelta(minutes=i),
                    source="chat_a",
                )
            )
        await session.commit()

    print("seeded:")
    for mid, txt, _ in SEED:
        print(f"  {mid}: {txt}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
