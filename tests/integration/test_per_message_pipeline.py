"""Интеграция per_message-конвейера на реальной БД (LLM/эмбеддинги — моки).

Покрывает старый путь run_extraction → resolve_and_save и общее ядро резолва:
якоря тред/цитата, эмбеддинг-резолв через резолвер, правку и удаление.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal
from app.schemas.incoming import IncomingMessage
from app.services.edits import handle_delete, handle_edit
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
    """Записать сообщение в БД (как persist_message) и вернуть совпадающий
    IncomingMessage для run_extraction/handle_*."""
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


async def _vacancy_count() -> int:
    return await _scalar("SELECT count(*) FROM vacancies WHERE is_deleted = false")


async def _open_vacancy_id() -> str:
    return await _scalar(
        "SELECT id::text FROM vacancies WHERE is_deleted = false ORDER BY created_at LIMIT 1"
    )


# --------------------------------------------------------------------------


async def test_creates_vacancy_from_hiring_message(llm):
    llm.prefilter.return_value = "yes"
    llm.extractor.return_value = _extract(
        "create", "Backend (Python)", title="Backend (Python)", salary_max=300000
    )
    msg = await _seed("m0", "Открыли вакансию Backend (Python), до 300k")

    await run_extraction(msg)

    assert await _vacancy_count() == 1
    assert await _scalar("SELECT title FROM vacancies") == "Backend (Python)"
    assert await _scalar("SELECT salary_max FROM vacancies") == 300000


async def test_prefilter_no_skips_extraction(llm):
    # Дефолтный prefilter='no' + нет контекста → экстрактор не зовётся.
    msg = await _seed("m0", "Открыли вакансию Backend, до 300k")

    await run_extraction(msg)

    assert await _vacancy_count() == 0
    llm.extractor.assert_not_awaited()


async def test_thread_followup_updates_same_vacancy(llm):
    llm.prefilter.return_value = "yes"
    llm.extractor.return_value = _extract(
        "create", "Backend", title="Backend", salary_max=300000
    )
    ma = await _seed("m_a", "Открыли Backend, до 300k", thread_id="T1", created_at=_BASE)
    await run_extraction(ma)
    assert await _vacancy_count() == 1

    # Follow-up в том же треде без названия — привязка структурная (тред).
    llm.extractor.return_value = _extract("update", "Backend", salary_max=350000)
    mb = await _seed(
        "m_b", "теперь подняли до 350", thread_id="T1", created_at=_BASE + timedelta(seconds=5)
    )
    await run_extraction(mb)

    assert await _vacancy_count() == 1  # без дубля
    assert await _scalar("SELECT salary_max FROM vacancies") == 350000


async def test_quoted_followup_updates_same_vacancy(llm):
    llm.prefilter.return_value = "yes"
    llm.extractor.return_value = _extract(
        "create", "Rust", title="Rust разработчик", salary_max=300000
    )
    ma = await _seed("m_a", "Открыли Rust разработчика", created_at=_BASE)
    await run_extraction(ma)
    assert await _vacancy_count() == 1

    # Цитата на объявление — привязка по quoted_message_id.
    llm.extractor.return_value = _extract("update", "Rust", salary_max=360000)
    mb = await _seed(
        "m_b", "поднимаем до 360k", quoted="m_a", created_at=_BASE + timedelta(seconds=5)
    )
    await run_extraction(mb)

    assert await _vacancy_count() == 1
    assert await _scalar("SELECT salary_max FROM vacancies") == 360000


async def test_resolver_match_updates_existing_without_duplicate(llm):
    llm.prefilter.return_value = "yes"
    llm.extractor.return_value = _extract(
        "create", "Backend", title="Backend", salary_max=300000
    )
    ma = await _seed("m_a", "Открыли Backend, до 300k", created_at=_BASE)
    await run_extraction(ma)
    vacancy_id = await _open_vacancy_id()

    # Follow-up без структурного якоря: резолвер уверенно матчит на ту же запись.
    llm.resolver.return_value = json.dumps({"vacancy_id": vacancy_id, "confidence": 0.9})
    llm.extractor.return_value = _extract("update", "Backend", salary_max=400000)
    mb = await _seed("m_b", "по бэкенду подняли до 400", created_at=_BASE + timedelta(seconds=5))
    await run_extraction(mb)

    assert await _vacancy_count() == 1  # резолвер матчнул, дубля нет
    assert await _scalar("SELECT salary_max FROM vacancies") == 400000


async def test_edit_processed_recomputes_vacancy(llm):
    llm.prefilter.return_value = "yes"
    llm.extractor.return_value = _extract(
        "create", "Backend", title="Backend", salary_max=200000
    )
    msg = await _seed("m0", "Открыли Backend, до 200k", created_at=_BASE)
    await run_extraction(msg)
    vacancy_id = await _open_vacancy_id()

    # Правка обработанного сообщения → пересчёт: новая зарплата.
    llm.resolver.return_value = json.dumps({"vacancy_id": vacancy_id, "confidence": 0.9})
    llm.extractor.return_value = _extract("update", "Backend", salary_max=260000)
    edited = IncomingMessage(
        message_id="m0",
        space_id=SPACE,
        thread_id=None,
        author_id="users/x",
        author_name="Иван",
        text="Открыли Backend, до 260k",
        created_at=_BASE,
    )
    await handle_edit(edited)

    assert await _vacancy_count() == 1
    assert await _scalar("SELECT salary_max FROM vacancies") == 260000
    assert await _scalar("SELECT is_edited FROM chat_messages WHERE message_id = 'm0'") is True


async def test_delete_single_source_soft_deletes_vacancy(llm):
    llm.prefilter.return_value = "yes"
    llm.extractor.return_value = _extract(
        "create", "Backend", title="Backend", salary_max=300000
    )
    msg = await _seed("m0", "Открыли Backend, до 300k", created_at=_BASE)
    await run_extraction(msg)
    assert await _vacancy_count() == 1

    await handle_delete(
        IncomingMessage(
            message_id="m0",
            space_id=SPACE,
            thread_id=None,
            author_id="users/x",
            author_name="Иван",
            text="",
            created_at=_BASE,
            event_type="deleted",
        )
    )

    assert await _vacancy_count() == 0  # единственный источник удалён → вакансия гаснет
    assert await _scalar("SELECT is_deleted FROM chat_messages WHERE message_id = 'm0'") is True
