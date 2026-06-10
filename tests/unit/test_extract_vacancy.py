"""Юнит-тесты extract_vacancy с подменой LLM-вызова.

extract_vacancy — ядро извлечения: собирает запрос, зовёт LLM (chat) и
превращает ответ в ExtractionResult. Подменяем chat на AsyncMock и проверяем
НАШУ логику вокруг него:
- парсинг валидного JSON (+ нормализация salary валидатором модели);
- retry на пустой ответ;
- fallback в action="none" при пустых/битых ответах на всех попытках;
- выбор system-промпта (базовый vs полный) в зависимости от контекста.
"""

from unittest.mock import AsyncMock

import pytest

from app.llm.extractor import (
    _MAX_ATTEMPTS,
    _SYSTEM_BASE,
    _SYSTEM_FULL,
    extract_vacancy,
)

pytestmark = pytest.mark.anyio

_CREATED_AT = "2026-06-10T12:00:00+00:00"


async def test_parses_valid_json_response(monkeypatch):
    # Валидный JSON → разобранный ExtractionResult; salary прогнан через валидатор
    raw = (
        '{"action": "create", "entity_ref": "Backend",'
        ' "fields": {"title": "Backend", "salary_min": "300к"}, "confidence": 0.9}'
    )
    monkeypatch.setattr("app.llm.extractor.chat", AsyncMock(return_value=raw))

    result = await extract_vacancy("ищем бэкендера", author_name="Аня", created_at=_CREATED_AT)

    assert result.action == "create"
    assert result.entity_ref == "Backend"
    assert result.fields["salary_min"] == 300000  # "300к" → int на границе модели
    assert result.confidence == 0.9


async def test_retries_on_empty_then_succeeds(monkeypatch):
    # Первый ответ пустой → вторая попытка возвращает валидный JSON
    mock_chat = AsyncMock(side_effect=["   ", '{"action": "update", "confidence": 0.7}'])
    monkeypatch.setattr("app.llm.extractor.chat", mock_chat)

    result = await extract_vacancy("теперь удалёнка", author_name="Аня", created_at=_CREATED_AT)

    assert result.action == "update"
    assert mock_chat.await_count == 2


async def test_falls_back_to_none_when_all_attempts_empty(monkeypatch):
    # Все попытки пустые → безопасный fallback action="none", без исключения
    mock_chat = AsyncMock(return_value="")
    monkeypatch.setattr("app.llm.extractor.chat", mock_chat)

    result = await extract_vacancy("привет", author_name="Аня", created_at=_CREATED_AT)

    assert result.action == "none"
    assert result.confidence == 0.0
    assert mock_chat.await_count == _MAX_ATTEMPTS


async def test_falls_back_to_none_on_malformed_json(monkeypatch):
    # Битый JSON на всех попытках → тоже fallback в none (а не падение)
    mock_chat = AsyncMock(return_value="это не json")
    monkeypatch.setattr("app.llm.extractor.chat", mock_chat)

    result = await extract_vacancy("ищем QA", author_name="Аня", created_at=_CREATED_AT)

    assert result.action == "none"
    assert mock_chat.await_count == _MAX_ATTEMPTS


async def test_uses_base_prompt_without_extra_context(monkeypatch):
    # Нет открытых вакансий и контекста → базовый system-промпт
    mock_chat = AsyncMock(return_value='{"action": "none", "confidence": 0.0}')
    monkeypatch.setattr("app.llm.extractor.chat", mock_chat)

    await extract_vacancy("привет", author_name="Аня", created_at=_CREATED_AT)

    assert mock_chat.await_args.kwargs["messages"][0]["content"] == _SYSTEM_BASE


async def test_uses_full_prompt_with_open_vacancies(monkeypatch):
    # Есть открытые вакансии → полный system-промпт с инструкциями по сопоставлению
    mock_chat = AsyncMock(return_value='{"action": "none", "confidence": 0.0}')
    monkeypatch.setattr("app.llm.extractor.chat", mock_chat)

    await extract_vacancy(
        "зп от 2000",
        author_name="Аня",
        created_at=_CREATED_AT,
        open_vacancies=[{"title": "Backend", "status": "open"}],
    )

    assert mock_chat.await_args.kwargs["messages"][0]["content"] == _SYSTEM_FULL
