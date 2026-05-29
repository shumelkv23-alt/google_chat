import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.db.models import ChatMessage, ChatMessageEmbedding
from app.db.session import AsyncSessionLocal, engine


async def _run() -> None:
    # 1. INSERT в chat_messages
    async with AsyncSessionLocal() as session:
        msg = ChatMessage(
            message_id="smoke-test-msg-1",
            space_id="spaces/SMOKE",
            thread_id=None,
            author_id="users/smoke",
            author_name="Smoke",
            text="открыли вакансию python-разработчика",
            created_at=datetime.now(timezone.utc),
            source="chat_a",
        )
        session.add(msg)
        await session.flush()
        msg_id = msg.id

        # 2. INSERT в chat_messages_embeddings (вектор-болванка)
        emb = ChatMessageEmbedding(
            message_id=msg_id,
            embedding=[0.1] * 1536,
            model="text-embedding-3-small",
        )
        session.add(emb)
        await session.commit()

    # 3. Cosine top-K поиск по чисто SQL
    async with AsyncSessionLocal() as session:
        query_vec = "[" + ",".join(["0.1"] * 1536) + "]"
        result = await session.execute(
            text(
                "SELECT cm.message_id, 1 - (cme.embedding <=> CAST(:q AS vector)) AS score "
                "FROM chat_messages_embeddings cme "
                "JOIN chat_messages cm ON cm.id = cme.message_id "
                "ORDER BY cme.embedding <=> CAST(:q AS vector) "
                "LIMIT 5"
            ),
            {"q": query_vec},
        )
        rows = result.all()
        assert rows, "ожидали хотя бы один результат"
        assert rows[0].message_id == "smoke-test-msg-1"
        assert rows[0].score > 0.99, f"cosine sim должен быть ~1, получили {rows[0].score}"
        print(f"top-K результат: message_id={rows[0].message_id}, score={rows[0].score:.4f}")

    # 4. Cleanup
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM chat_messages WHERE message_id = 'smoke-test-msg-1'")
        )
        await session.commit()

    await engine.dispose()


def test_db_smoke() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    asyncio.run(_run())
    print("smoke ok")
