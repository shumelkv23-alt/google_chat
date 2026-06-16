"""Регрессия: правка pending-сообщения не должна оставлять устаревший вектор.

Баг: handle_edit_batch для pending обновляет text + text_hash, но не трогает
embedding. Если вектор уже есть (например, после упавшего флаша), идемпотентный
_embed_batch на следующем тике увидит его и применит к НОВОМУ тексту старый
вектор → резолв по чужому смыслу. Фикс: удалить вектор при правке pending,
следующий флаш пересчитает его по свежему тексту.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.db.models import ChatMessage, ChatMessageEmbedding
from app.db.session import AsyncSessionLocal
from app.schemas.incoming import IncomingMessage
from app.services.edits import handle_edit_batch
from app.services.hashing import sha256_hex

pytestmark = pytest.mark.anyio

SPACE = "spaces/TEST"


async def _seed_pending_with_embedding(message_id: str, text_: str):
    async with AsyncSessionLocal() as session:
        row = ChatMessage(
            message_id=message_id,
            space_id=SPACE,
            thread_id=None,
            author_id="users/x",
            author_name="Иван",
            text=text_,
            text_hash=sha256_hex(text_),
            created_at=datetime.now(timezone.utc),
            source="chat_a",
        )
        session.add(row)
        await session.flush()
        # Вектор «из прошлого флаша» — он и устаревает после правки.
        session.add(
            ChatMessageEmbedding(
                message_id=row.id, embedding=[0.1] * 1536, model="test"
            )
        )
        await session.commit()
        return row.id


async def test_editing_pending_message_drops_stale_embedding():
    pk = await _seed_pending_with_embedding("m0", "Открыли Backend, до 300k")

    await handle_edit_batch(
        IncomingMessage(
            message_id="m0",
            space_id=SPACE,
            thread_id=None,
            author_id="users/x",
            author_name="Иван",
            text="Открыли DevOps инженера",  # совсем другой смысл
            created_at=datetime.now(timezone.utc),
        )
    )

    async with AsyncSessionLocal() as session:
        remaining = (
            await session.execute(
                text(
                    "SELECT count(*) FROM chat_messages_embeddings WHERE message_id = :p"
                ),
                {"p": pk},
            )
        ).scalar()

    # Устаревший вектор удалён → следующий флаш пересчитает по новому тексту.
    assert remaining == 0
