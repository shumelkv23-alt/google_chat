"""Integration-тест ingest pipeline: Pub/Sub payload → chat_messages + embedding.

Запуск (контейнер chatbot-pg должен быть запущен):
    python -m pytest tests/test_ingest.py -v
"""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, text

from app.db.models import ChatMessage, ChatMessageEmbedding
from app.db.session import AsyncSessionLocal, engine
from app.schemas.incoming import IncomingMessage, parse_we_event
from app.services.ingest import ingest_message

_FAKE_MSG_ID = "spaces/TEST/messages/ingest-test-1"

_WE_EVENT: dict = {
    "type": "google.workspace.chat.message.v1.created",
    "message": {
        "name": _FAKE_MSG_ID,
        "createTime": "2024-01-01T12:00:00Z",
        "sender": {"name": "users/test-user", "displayName": "Test User"},
        "text": "Ищем Python разработчика, до 300к",
        "thread": {"name": "spaces/TEST/threads/thread-1"},
        "space": {"name": "spaces/TEST"},
    },
}


def test_parse_we_event() -> None:
    msg = parse_we_event(_WE_EVENT)
    assert msg is not None
    assert msg.message_id == _FAKE_MSG_ID
    assert msg.text == "Ищем Python разработчика, до 300к"
    assert msg.space_id == "spaces/TEST"
    assert msg.thread_id == "spaces/TEST/threads/thread-1"
    assert msg.author_id == "users/test-user"
    assert msg.source == "chat_a"


def test_parse_we_event_skips_non_created() -> None:
    data = {**_WE_EVENT, "type": "google.workspace.chat.message.v1.deleted"}
    assert parse_we_event(data) is None


def test_parse_we_event_skips_empty_text() -> None:
    event = {**_WE_EVENT, "message": {**_WE_EVENT["message"], "text": "  "}}
    assert parse_we_event(event) is None


async def _run_ingest() -> None:
    # cleanup
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM chat_messages WHERE message_id = :mid"),
            {"mid": _FAKE_MSG_ID},
        )
        await session.commit()

    msg = parse_we_event(_WE_EVENT)
    assert msg is not None

    fake_vector = [0.1] * 1536
    mock_resp = AsyncMock()
    mock_resp.data = [AsyncMock(embedding=fake_vector)]

    with patch("app.services.ingest._openai") as mock_openai:
        mock_openai.embeddings.create = AsyncMock(return_value=mock_resp)
        await ingest_message(msg)

        # дубль — должен молча пропустить
        await ingest_message(msg)

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ChatMessage).where(ChatMessage.message_id == _FAKE_MSG_ID)
            )
        ).scalar_one()
        assert row.text == "Ищем Python разработчика, до 300к"
        assert row.source == "chat_a"

        emb = (
            await session.execute(
                select(ChatMessageEmbedding).where(
                    ChatMessageEmbedding.message_id == row.id
                )
            )
        ).scalar_one()
        assert len(emb.embedding) == 1536

    # cleanup
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM chat_messages WHERE message_id = :mid"),
            {"mid": _FAKE_MSG_ID},
        )
        await session.commit()

    await engine.dispose()


def test_ingest_message() -> None:
    asyncio.run(_run_ingest())
