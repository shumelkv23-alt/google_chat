

import json
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.llm.batch_extractor import (
    BatchItem,
    _format_batch_messages,
    _format_history,
    _format_open_vacancies,
    _validate_coverage,
    extract_batch,
)

pytestmark = pytest.mark.anyio


def _markup(n: int) -> list[dict]:
    return [
        {"message_index": i, "author_name": "Иван", "text": f"сообщение {i}"}
        for i in range(n)
    ]


def _llm_json(items: list[dict]) -> str:
    return json.dumps({"items": items}, ensure_ascii=False)


# --------------------------------------------------------------------------
# _validate_coverage
# --------------------------------------------------------------------------


def test_coverage_drops_out_of_range_index():
    items = [
        BatchItem(message_index=0, action="none"),
        BatchItem(message_index=7, action="create", confidence=0.9),  # галлюцинация
    ]

    out = _validate_coverage(items, batch_len=2)

    assert [it.message_index for it in out] == [0]


def test_coverage_keeps_first_on_duplicate_index():
    items = [
        BatchItem(message_index=0, action="update", confidence=0.8),
        BatchItem(message_index=0, action="close", confidence=0.5),  # дубль
    ]

    out = _validate_coverage(items, batch_len=1)

    assert len(out) == 1
    assert out[0].action == "update"


def test_coverage_sorts_by_index():
    items = [
        BatchItem(message_index=2, action="none"),
        BatchItem(message_index=0, action="none"),
        BatchItem(message_index=1, action="none"),
    ]

    out = _validate_coverage(items, batch_len=3)

    assert [it.message_index for it in out] == [0, 1, 2]


# --------------------------------------------------------------------------
# extract_batch: разбор ответа
# --------------------------------------------------------------------------


async def test_extract_parses_links_and_explicit_none(monkeypatch):
    raw = _llm_json(
        [
            {
                "message_index": 0,
                "action": "create",
                "entity_ref": "Backend (Python)",
                "fields": {"title": "Backend (Python)", "salary_max": 300000},
                "confidence": 0.9,
            },
            {
                "message_index": 1,
                "action": "update",
                "entity_ref": "Backend (Python)",
                "fields": {"salary_max": 350000},
                "confidence": 0.8,
                "link_to_index": 0,
            },
            {
                "message_index": 2,
                "action": "update",
                "entity_ref": "DevOps",
                "fields": {"team": "infra"},
                "confidence": 0.7,
                "link_to_vacancy_ref": "v2",
            },
            {"message_index": 3, "action": "none", "confidence": 0.0},
        ]
    )
    monkeypatch.setattr("app.llm.batch_extractor.chat", AsyncMock(return_value=raw))

    items = await extract_batch(_markup(4))

    assert len(items) == 4  # вердикт по каждому индексу, none — явный
    assert items[1].link_to_index == 0
    assert items[2].link_to_vacancy_ref == "v2"
    assert items[3].action == "none"


async def test_extract_skips_invalid_item_keeps_rest(monkeypatch):
    raw = _llm_json(
        [
            {"message_index": 0, "action": "none"},
            {"message_index": 1, "action": "взорвать"},  # невалидный action
        ]
    )
    monkeypatch.setattr("app.llm.batch_extractor.chat", AsyncMock(return_value=raw))

    items = await extract_batch(_markup(2))

    assert [it.message_index for it in items] == [0]


async def test_extract_empty_batch_short_circuits(monkeypatch):
    mock_chat = AsyncMock()
    monkeypatch.setattr("app.llm.batch_extractor.chat", mock_chat)

    assert await extract_batch([]) == []
    mock_chat.assert_not_awaited()


# --------------------------------------------------------------------------
# extract_batch: самопочинка парсинга
# --------------------------------------------------------------------------


async def test_extract_retries_with_error_feedback_on_broken_json(monkeypatch):
    good = _llm_json([{"message_index": 0, "action": "none"}])
    mock_chat = AsyncMock(side_effect=["{битый json", good])
    monkeypatch.setattr("app.llm.batch_extractor.chat", mock_chat)

    items = await extract_batch(_markup(1))

    assert len(items) == 1
    assert mock_chat.await_count == 2
    # Второй вызов содержит битый ответ модели и просьбу исправить —
    # модель видит свою ошибку, а не повторяет её вслепую.
    retry_messages = mock_chat.await_args_list[1].kwargs["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": "{битый json"}
    assert "не распарсился" in retry_messages[-1]["content"]


async def test_extract_returns_empty_after_all_attempts_fail(monkeypatch):
    mock_chat = AsyncMock(side_effect=["{битый", "{опять битый"])
    monkeypatch.setattr("app.llm.batch_extractor.chat", mock_chat)

    assert await extract_batch(_markup(2)) == []
    assert mock_chat.await_count == 2


# --------------------------------------------------------------------------
# Форматирование контекста
# --------------------------------------------------------------------------


def test_format_open_vacancies_shows_refs():
    out = _format_open_vacancies(
        [
            {
                "ref": "v1",
                "title": "Backend (Python)",
                "status": "open",
                "salary_max": 300000,
                "_latest": True,
            },
            {"ref": "v2", "title": "DevOps", "status": "open"},
        ]
    )

    assert "[v1] Backend (Python)" in out
    assert "[v2] DevOps" in out
    assert "[последняя созданная]" in out


def test_format_history_shows_labels_and_binding():
    out = _format_history(
        [
            {
                "label": "h0",
                "author_name": "Иван",
                "text": "ищем питониста, до 300к",
                "created_at": datetime(2026, 6, 10, 16, 2),
                "vacancy_ref": "v1",
                "vacancy_title": "Backend (Python)",
            },
            {
                "label": "h1",
                "author_name": "Мария",
                "text": "всем привет",
                "created_at": datetime(2026, 6, 11, 9, 15),
                "vacancy_ref": None,
                "vacancy_title": None,
            },
        ]
    )

    assert "[h0] Иван" in out
    assert "→ вакансия [v1] «Backend (Python)»" in out
    assert "[h1] Мария" in out
    assert "НЕ возвращать" in out  # история — только контекст


def test_format_messages_marks_reply_to_history():
    out = _format_batch_messages(
        [
            {
                "message_index": 0,
                "author_name": "Саша",
                "text": "а какой грейд?",
                "thread_label": None,
                "reply_to_index": None,
                "reply_to_history": "h1",
                "reply_to_text": None,
            }
        ]
    )

    assert "↳ ответ на [h1]" in out
