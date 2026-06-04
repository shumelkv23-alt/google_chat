"""Integration-тест ingest pipeline: Pub/Sub payload → chat_messages + embedding.
"""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, text

from app.db.models import ChatMessage, ChatMessageEmbedding
from app.db.session import AsyncSessionLocal, engine
from app.schemas.incoming import IncomingMessage, parse_we_event
from app.services.ingest import embed_message, persist_message

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
    assert msg.event_type == "created"


def test_parse_we_event_skips_unknown_type() -> None:
    data = {**_WE_EVENT, "type": "google.workspace.chat.something.else"}
    assert parse_we_event(data) is None


def test_parse_we_event_updated() -> None:
    data = {**_WE_EVENT, "type": "google.workspace.chat.message.v1.updated"}
    msg = parse_we_event(data)
    assert msg is not None
    assert msg.event_type == "updated"


def test_parse_we_event_deleted_allows_empty_text() -> None:
    data = {
        "type": "google.workspace.chat.message.v1.deleted",
        "message": {"name": _FAKE_MSG_ID},  # у deleted текста может не быть
    }
    msg = parse_we_event(data)
    assert msg is not None
    assert msg.event_type == "deleted"
    assert msg.message_id == _FAKE_MSG_ID


def test_parse_we_event_skips_empty_text() -> None:
    event = {**_WE_EVENT, "message": {**_WE_EVENT["message"], "text": "  "}}
    assert parse_we_event(event) is None


def test_parse_we_event_extracts_quoted_message() -> None:
    quoted_name = "spaces/TEST/messages/quoted-1"
    event = {
        **_WE_EVENT,
        "message": {
            **_WE_EVENT["message"],
            "quotedMessageMetadata": {"name": quoted_name},
        },
    }
    msg = parse_we_event(event)
    assert msg is not None
    assert msg.quoted_message_id == quoted_name


def test_parse_we_event_no_quote_is_none() -> None:
    msg = parse_we_event(_WE_EVENT)
    assert msg is not None
    assert msg.quoted_message_id is None


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
        mock_create = AsyncMock(return_value=mock_resp)
        mock_openai.embeddings.create = mock_create

        # Первая доставка: новое сообщение + вектор.
        assert await persist_message(msg) is True
        await embed_message(msg)

        # Повторная доставка (at-least-once): persist видит дубль, embed
        # идемпотентен — второй вектор не создаётся, OpenAI не дёргается снова.
        assert await persist_message(msg) is False
        await embed_message(msg)

        assert mock_create.await_count == 1

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ChatMessage).where(ChatMessage.message_id == _FAKE_MSG_ID)
            )
        ).scalar_one()
        assert row.text == "Ищем Python разработчика, до 300к"
        assert row.source == "chat_a"

        embeddings = (
            await session.execute(
                select(ChatMessageEmbedding).where(
                    ChatMessageEmbedding.message_id == row.id
                )
            )
        ).scalars().all()
        assert len(embeddings) == 1  # ровно один вектор, без дублей
        assert len(embeddings[0].embedding) == 1536

    async with AsyncSessionLocal() as session:
        await session.execute(
            text("DELETE FROM chat_messages WHERE message_id = :mid"),
            {"mid": _FAKE_MSG_ID},
        )
        await session.commit()

    await engine.dispose()


def test_ingest_message() -> None:
    asyncio.run(_run_ingest())
