"""Тесты prefilter + extractor с мок OpenRouter.

Запуск:
    python -m pytest tests/test_extractor.py -v
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.llm.extractor import ExtractionResult, extract_vacancy
from app.llm.prefilter import is_vacancy_message

# ---------------------------------------------------------------------------
# Вспомогательная функция для мока chat()
# ---------------------------------------------------------------------------

def _mock_chat(return_value: str) -> AsyncMock:
    return AsyncMock(return_value=return_value)


# ---------------------------------------------------------------------------
# Prefilter
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Открыли вакансию питониста, до 300k на руки", True),
    ("Ищем тимлида в команду Платформа, опыт от 5 лет", True),
    ("Закрыли позицию Go-разработчика в Биллинг", True),
    ("Привет, как дела", False),
    ("Встреча по проекту в 15:00", False),
])
def test_prefilter(text: str, expected: bool) -> None:
    import asyncio

    answer = "yes" if expected else "no"
    with patch("app.llm.prefilter.chat", _mock_chat(answer)):
        result = asyncio.run(is_vacancy_message(text))
    assert result == expected


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw_json,expected_action,check", [
    (
        json.dumps({
            "action": "create",
            "entity_ref": "питонист в команду Платформа",
            "fields": {"salary_max": 300000, "currency": "RUB", "team": "Платформа"},
            "confidence": 0.92,
        }),
        "create",
        lambda r: r.fields.get("salary_max") == 300000,
    ),
    (
        json.dumps({
            "action": "update",
            "entity_ref": "питонист",
            "fields": {"salary_min": 320000, "salary_max": 350000},
            "confidence": 0.88,
        }),
        "update",
        lambda r: r.fields.get("salary_min") == 320000,
    ),
    (
        json.dumps({
            "action": "close",
            "entity_ref": "Go-разработчик Биллинг",
            "fields": {"status": "closed"},
            "confidence": 0.95,
        }),
        "close",
        lambda r: r.confidence > 0.9,
    ),
    (
        json.dumps({
            "action": "none",
            "entity_ref": "",
            "fields": {},
            "confidence": 0.1,
        }),
        "none",
        lambda r: r.entity_ref == "",
    ),
])
def test_extractor(raw_json: str, expected_action: str, check) -> None:
    import asyncio

    with patch("app.llm.extractor.chat", _mock_chat(raw_json)):
        result = asyncio.run(
            extract_vacancy("текст сообщения", "Test User", "2024-01-01T12:00:00")
        )
    assert result.action == expected_action
    assert check(result)


def test_extractor_invalid_json_returns_none() -> None:
    import asyncio

    with patch("app.llm.extractor.chat", _mock_chat("не json")):
        result = asyncio.run(
            extract_vacancy("текст", "User", "2024-01-01T12:00:00")
        )
    assert result.action == "none"
    assert result.confidence == 0.0
