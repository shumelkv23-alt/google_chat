"""Интеграция batch-конвейера на реальной БД chatbot_test (LLM/эмбеддинги — моки).

Переносит сценарии из scripts/ в asserting-тесты + закрывает регрессией баг
mark_processed (bulk-update), который 95 юнитов не видели — он вылезает только
на живом драйвере PostgreSQL.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.config import settings
from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal
from app.schemas.incoming import IncomingMessage
from app.services.batch_processor import flush_batch, flush_due_batches
from app.services.edits import handle_edit_batch
from app.services.hashing import sha256_hex

pytestmark = pytest.mark.anyio

SPACE = "spaces/TEST"
_BASE = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Хелперы посева и запросов
# --------------------------------------------------------------------------


async def _seed(
    message_id: str,
    text_: str,
    *,
    thread_id: str | None = None,
    quoted: str | None = None,
    created_at: datetime | None = None,
    author: str = "Иван",
) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            ChatMessage(
                message_id=message_id,
                space_id=SPACE,
                thread_id=thread_id,
                author_id="users/x",
                author_name=author,
                text=text_,
                text_hash=sha256_hex(text_),
                created_at=created_at or _BASE,
                source="chat_a",
                quoted_message_id=quoted,
            )
        )
        await session.commit()


def _items(*items: dict) -> str:
    return json.dumps({"items": list(items)}, ensure_ascii=False)


def _create(idx: int, title: str, **fields) -> dict:
    return {
        "message_index": idx,
        "action": "create",
        "entity_ref": title,
        "fields": {"title": title, **fields},
        "confidence": 0.9,
    }


def _none(idx: int) -> dict:
    return {"message_index": idx, "action": "none", "confidence": 0.0}


async def _scalar(sql: str, **params):
    async with AsyncSessionLocal() as session:
        return (await session.execute(text(sql), params)).scalar()


async def _status(message_id: str) -> str:
    return await _scalar(
        "SELECT process_status FROM chat_messages WHERE message_id = :m",
        m=message_id,
    )


async def _pending_count() -> int:
    return await _scalar(
        "SELECT count(*) FROM chat_messages "
        "WHERE space_id = :sp AND process_status = 'pending' AND is_deleted = false",
        sp=SPACE,
    )


async def _vacancy_count() -> int:
    return await _scalar("SELECT count(*) FROM vacancies WHERE is_deleted = false")


# --------------------------------------------------------------------------
# Применение результатов пачки
# --------------------------------------------------------------------------


async def test_flush_creates_vacancies_and_marks_all_processed(llm):
    # Регрессия на баг mark_processed: применилось к БД, но не пометилось →
    # дубли и бесконечная переобработка. Юниты с моками этого не ловили.
    await _seed("m0", "Открыли вакансию Backend (Python), до 300k", created_at=_BASE)
    await _seed("m1", "Ищем DevOps инженера", created_at=_BASE + timedelta(seconds=1))
    await _seed("m2", "Нужен QA, ручное тестирование", created_at=_BASE + timedelta(seconds=2))
    llm.batch.return_value = _items(
        _create(0, "Backend (Python)", salary_max=300000),
        _create(1, "DevOps"),
        _create(2, "QA Engineer"),
    )

    await flush_batch(SPACE)

    assert await _vacancy_count() == 3
    assert await _pending_count() == 0
    for mid in ("m0", "m1", "m2"):
        assert await _status(mid) == "processed"


async def test_link_to_index_keeps_single_vacancy(llm):
    # B8: create + update в одной пачке через link_to_index → одна вакансия,
    # две ревизии, без эмбеддинг-дубля.
    await _seed("m0", "Открыли Backend, до 300k", created_at=_BASE)
    await _seed("m1", "по бэкенду подняли до 350", created_at=_BASE + timedelta(seconds=1))
    llm.batch.return_value = _items(
        _create(0, "Backend", salary_max=300000),
        {
            "message_index": 1,
            "action": "update",
            "entity_ref": "Backend",
            "fields": {"salary_max": 350000},
            "confidence": 0.8,
            "link_to_index": 0,
        },
    )

    await flush_batch(SPACE)

    assert await _vacancy_count() == 1
    assert await _scalar("SELECT salary_max FROM vacancies WHERE is_deleted = false") == 350000
    assert await _scalar("SELECT count(*) FROM vacancy_revisions") == 2


async def test_explicit_none_marks_processed_without_vacancy(llm):
    # B9: осознанный none — честный processed, вакансия не заводится.
    await _seed("m0", "Всем привет, как дела", created_at=_BASE)
    llm.batch.return_value = _items(_none(0))

    await flush_batch(SPACE)

    assert await _vacancy_count() == 0
    assert await _status("m0") == "processed"


async def test_missing_index_stays_pending_and_bumps_attempts(llm):
    # B9: пропущенный LLM индекс — не none, а ошибка: остаётся pending на
    # повторный флаш, flush_attempts растёт.
    await _seed("m0", "Открыли Backend", created_at=_BASE)
    await _seed("m1", "Ищем фронтендера", created_at=_BASE + timedelta(seconds=1))
    llm.batch.return_value = _items(_create(0, "Backend"))  # индекс 1 пропущен

    await flush_batch(SPACE)

    assert await _status("m0") == "processed"
    assert await _status("m1") == "pending"
    assert await _scalar(
        "SELECT flush_attempts FROM chat_messages WHERE message_id = :m", m="m1"
    ) == 1


async def test_edit_during_flush_keeps_message_pending(llm):
    # B1: правка пришла, пока пачка в LLM. Условный mark видит расхождение
    # text_hash и НЕ помечает сообщение — правка не проглатывается.
    await _seed("m0", "Открыли Backend, до 300k", created_at=_BASE)

    async def _edit_then_return(*_args, **_kwargs):
        await handle_edit_batch(
            IncomingMessage(
                message_id="m0",
                space_id=SPACE,
                thread_id=None,
                author_id="users/x",
                author_name="Иван",
                text="Открыли Backend, до 350k",  # текст и hash меняются
                created_at=_BASE,
            )
        )
        return _items(_create(0, "Backend", salary_max=300000))

    llm.batch.side_effect = _edit_then_return

    await flush_batch(SPACE)

    assert await _status("m0") == "pending"  # переедет в следующий тик
    assert await _vacancy_count() == 0


# --------------------------------------------------------------------------
# Триггеры созревания пачки (flush_due_batches)
# --------------------------------------------------------------------------


async def test_due_by_count_triggers_flush(llm, monkeypatch):
    monkeypatch.setattr(settings, "batch_size", 3)
    now = datetime.now(timezone.utc)
    for i in range(3):  # свежие: ни quiet, ни timeout — срабатывает только count
        await _seed(f"c{i}", f"сообщение {i}", created_at=now)
    llm.batch.return_value = _items(_none(0), _none(1), _none(2))

    await flush_due_batches()

    assert await _pending_count() == 0


async def test_not_due_when_few_and_fresh(llm, monkeypatch):
    monkeypatch.setattr(settings, "batch_size", 3)
    now = datetime.now(timezone.utc)
    await _seed("f0", "сообщение 0", created_at=now)
    await _seed("f1", "сообщение 1", created_at=now)

    await flush_due_batches()

    assert await _pending_count() == 2  # ниже порога, разговор не затих
    llm.batch.assert_not_awaited()


async def test_due_by_quiet_window_triggers_flush(llm):
    # Затих на > quiet_seconds (75), но count не набран и timeout не прошёл.
    quiet_ago = datetime.now(timezone.utc) - timedelta(seconds=settings.quiet_seconds + 10)
    await _seed("q0", "сообщение 0", created_at=quiet_ago)
    await _seed("q1", "сообщение 1", created_at=quiet_ago)
    llm.batch.return_value = _items(_none(0), _none(1))

    await flush_due_batches()

    assert await _pending_count() == 0


async def test_due_by_timeout_triggers_flush(llm):
    # Жёсткий потолок: старейшее старше timeout, но разговор «живой» (свежее
    # сообщение секунду назад → quiet не сработал), count не набран.
    now = datetime.now(timezone.utc)
    await _seed("t_old", "старое объявление", created_at=now - timedelta(seconds=settings.batch_timeout_seconds + 20))
    await _seed("t_new", "свежий трёп", created_at=now)
    llm.batch.return_value = _items(_none(0), _none(1))

    await flush_due_batches()

    assert await _pending_count() == 0
