
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal, engine
from app.services.hashing import sha256_hex

SPACE = "spaces/AAQAmmGOtCo"
THREAD = "spaces/AAQAmmGOtCo/threads/scala-T1"

# (message_id, text, thread_id, quoted_message_id)
SEED = [
    ("seed-th-1", "Ищем Scala-разработчика в команду Финтех, до 280k", THREAD, None),
    ("seed-th-2", "кстати, теперь полная удалёнка", THREAD, None),
    ("seed-q-1", "Открыли позицию Rust-разработчика, до 320k", None, None),
    ("seed-q-2", "поднимаем до 360k", None, "seed-q-1"),
]


async def _run() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "DELETE FROM chat_messages "
                "WHERE message_id LIKE 'seed-th-%' OR message_id LIKE 'seed-q-%'"
            )
        )
        base = datetime.now(timezone.utc) - timedelta(days=2)
        for i, (mid, txt, thread, quoted) in enumerate(SEED):
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
                    quoted_message_id=quoted,
                )
            )
        await session.commit()

    print("seeded:")
    for mid, txt, thread, quoted in SEED:
        tag = " [тред]" if thread else (f" [цитата на {quoted}]" if quoted else "")
        print(f"  {mid}{tag}: {txt}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
