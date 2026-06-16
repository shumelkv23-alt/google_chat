"""Smoke интеграционного харнесса: коннект к chatbot_test, схема на месте,
предохранитель активен, очистка между тестами работает.

Эти тесты не трогают пайплайн и LLM — только проверяют, что фундамент стоит,
прежде чем нести на него сценарии из scripts/.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal, engine
from app.services.hashing import sha256_hex

pytestmark = pytest.mark.anyio

_CORE_TABLES = {
    "chat_messages",
    "chat_messages_embeddings",
    "vacancies",
    "vacancy_revisions",
    "conversations",
}


async def test_schema_has_core_tables():
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        ).scalars().all()

    assert _CORE_TABLES.issubset(set(rows))


async def test_pgvector_extension_present():
    async with AsyncSessionLocal() as session:
        ext = (
            await session.execute(
                text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
            )
        ).scalar()

    assert ext == "vector"


async def test_engine_points_at_test_db():
    # Предохранитель пропустил прогон — значит имя базы тестовое.
    assert "test" in (engine.url.database or "")


async def test_insert_visible_within_test():
    # clean_tables дал чистый старт → ровно одна вставленная строка видна.
    async with AsyncSessionLocal() as session:
        session.add(
            ChatMessage(
                message_id="smoke-1",
                space_id="spaces/TEST",
                thread_id=None,
                author_id="users/a",
                author_name="A",
                text="привет",
                text_hash=sha256_hex("привет"),
                created_at=datetime.now(timezone.utc),
                source="chat_a",
            )
        )
        await session.commit()

    async with AsyncSessionLocal() as session:
        count = (
            await session.execute(text("SELECT count(*) FROM chat_messages"))
        ).scalar()

    assert count == 1


async def test_tables_cleaned_between_tests():
    # Строку вставил предыдущий тест; autouse clean_tables очистил перед этим —
    # доказательство изоляции.
    async with AsyncSessionLocal() as session:
        count = (
            await session.execute(text("SELECT count(*) FROM chat_messages"))
        ).scalar()

    assert count == 0
