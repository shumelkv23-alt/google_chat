"""Интеграция онлайн-ветки batch-режима (route_created_batch).

Сообщение со структурным якорем (тред/цитата к существующей вакансии)
обрабатывается сразу. Но если в том же треде ждёт пачки более СТАРОЕ сообщение —
уходит в пачку вместе с ним (цельный разбор треда, страховка хронологии B3).
Без якоря — просто ждёт пачку.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal
from app.schemas.incoming import IncomingMessage
from app.services.batch_processor import route_created_batch
from app.services.extraction import run_extraction
from app.services.hashing import sha256_hex

pytestmark = pytest.mark.anyio

SPACE = "spaces/TEST"
_BASE = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)


async def _seed(
    message_id: str,
    text_: str,
    *,
    thread_id: str | None = None,
    quoted: str | None = None,
    created_at: datetime | None = None,
) -> IncomingMessage:
    async with AsyncSessionLocal() as session:
        session.add(
            ChatMessage(
                message_id=message_id,
                space_id=SPACE,
                thread_id=thread_id,
                author_id="users/x",
                author_name="Иван",
                text=text_,
                text_hash=sha256_hex(text_),
                created_at=created_at or _BASE,
                source="chat_a",
                quoted_message_id=quoted,
            )
        )
        await session.commit()
    return IncomingMessage(
        message_id=message_id,
        space_id=SPACE,
        thread_id=thread_id,
        author_id="users/x",
        author_name="Иван",
        text=text_,
        created_at=created_at or _BASE,
        quoted_message_id=quoted,
    )


def _extract(action: str, entity_ref: str, **fields) -> str:
    return json.dumps(
        {"action": action, "entity_ref": entity_ref, "fields": fields, "confidence": 0.9},
        ensure_ascii=False,
    )


async def _scalar(sql: str, **params):
    async with AsyncSessionLocal() as session:
        return (await session.execute(text(sql), params)).scalar()


async def _status(message_id: str) -> str:
    return await _scalar(
        "SELECT process_status FROM chat_messages WHERE message_id = :m", m=message_id
    )


async def _vacancy_count() -> int:
    return await _scalar("SELECT count(*) FROM vacancies WHERE is_deleted = false")


async def _make_thread_vacancy(thread_id: str, anchor_id: str, llm) -> None:
    """Завести вакансию, привязанную к треду, и пометить её источник обработанным
    (чтобы он не считался «старшим pending-соседом»)."""
    llm.prefilter.return_value = "yes"
    llm.extractor.return_value = _extract(
        "create", "Backend", title="Backend", salary_max=300000
    )
    msg = await _seed(anchor_id, "Открыли Backend, до 300k", thread_id=thread_id, created_at=_BASE)
    await run_extraction(msg)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE chat_messages SET process_status = 'processed', "
                "is_processed = true WHERE message_id = :m"
            ),
            {"m": anchor_id},
        )
        await session.commit()


# --------------------------------------------------------------------------


async def test_resolves_anchored_message_inline(llm):
    await _make_thread_vacancy("T1", "m_anchor", llm)
    llm.prefilter.return_value = "yes"
    llm.extractor.return_value = _extract("update", "Backend", salary_max=350000)
    mb = await _seed("m_b", "теперь подняли до 350", thread_id="T1", created_at=_BASE + timedelta(seconds=10))

    await route_created_batch(mb)

    assert await _status("m_b") == "processed"  # обработано сразу
    assert await _vacancy_count() == 1
    assert await _scalar("SELECT salary_max FROM vacancies") == 350000


async def test_defers_to_batch_when_older_pending_sibling_in_thread(llm):
    await _make_thread_vacancy("T1", "m_anchor", llm)
    # Старший pending-сосед в том же треде — mb должен уступить пачке.
    await _seed("m_older", "ещё вопрос по бэкенду", thread_id="T1", created_at=_BASE + timedelta(seconds=5))
    mb = await _seed("m_b", "теперь подняли до 350", thread_id="T1", created_at=_BASE + timedelta(seconds=10))

    await route_created_batch(mb)

    assert await _status("m_b") == "pending"  # ушёл в пачку вместе со старшим


async def test_leaves_unanchored_message_pending(llm):
    # Нет вакансии в треде и нет цитаты → структурного якоря нет → ждёт пачку.
    mb = await _seed("m_b", "Открыли что-то новое", thread_id="T_new", created_at=_BASE)

    await route_created_batch(mb)

    assert await _status("m_b") == "pending"
    assert await _vacancy_count() == 0
