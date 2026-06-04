"""Unit-тесты экстрактора (без БД/сети).

1. Свежесть — ключевой сигнал для коротких follow-up'ов: самое последнее
   сообщение должно быть явно помечено, чтобы LLM привязывал «а хотя нет 3000» к нему.
2. Пустой/битый ответ модели не должен ронять сообщение в action=none с первого раза.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from app.llm import extractor
from app.llm.extractor import (
    ExtractionResult,
    _format_context_messages,
    _format_open_vacancies,
    extract_vacancy,
)


def test_salary_string_coerced_to_int() -> None:
    # LLM отдал зарплату строкой — должно стать int, иначе падает запись в БД.
    result = ExtractionResult(action="update", fields={"salary_min": "2000", "currency": "USD"})
    assert result.fields["salary_min"] == 2000
    assert isinstance(result.fields["salary_min"], int)
    assert result.fields["currency"] == "USD"  # нечисловые поля не трогаем


def test_salary_with_units_and_thousands() -> None:
    result = ExtractionResult(
        action="update",
        fields={"salary_min": "150 000 руб", "salary_max": "300к"},
    )
    assert result.fields["salary_min"] == 150000
    assert result.fields["salary_max"] == 300000  # суффикс «к» → тысячи


def test_salary_unparseable_dropped() -> None:
    # Не вытащить число — поле выкидываем, а не роняем всю обработку.
    result = ExtractionResult(action="update", fields={"salary_min": "договорная"})
    assert "salary_min" not in result.fields


def test_salary_int_passthrough() -> None:
    result = ExtractionResult(action="create", fields={"salary_min": 2000, "salary_max": 3000})
    assert result.fields == {"salary_min": 2000, "salary_max": 3000}


def test_context_marks_most_recent_message() -> None:
    msgs = [
        {"author_name": "A", "text": "ищем питониста"},
        {"author_name": "B", "text": "ищем системного админа зп 1000"},
    ]
    lines = _format_context_messages(msgs).splitlines()

    assert "[самое свежее]" in lines[-1]
    assert "системного админа" in lines[-1]
    assert "[самое свежее]" not in lines[-2]  # более раннее — без пометки


def test_context_single_message_marked() -> None:
    lines = _format_context_messages([{"author_name": "A", "text": "ищем го"}]).splitlines()
    assert "[самое свежее]" in lines[-1]


def test_open_vacancies_marks_latest() -> None:
    vacs = [
        {"title": "джаваскрипт сеньор", "status": "open", "salary_min": 3000, "salary_max": 4000},
        {"title": "спеца по кибербезопасности", "status": "open", "_latest": True},
    ]
    out = _format_open_vacancies(vacs).splitlines()

    kiber = [line for line in out if "кибербезопасности" in line][0]
    js = [line for line in out if "джаваскрипт" in line][0]
    assert "[последняя созданная]" in kiber
    assert "[последняя созданная]" not in js


def test_extract_retries_on_empty_then_succeeds() -> None:
    # Первый ответ пустой (флап), второй — валидный JSON.
    fake_chat = AsyncMock(side_effect=["", '{"action": "update", "confidence": 0.8}'])

    async def _run():
        with patch.object(extractor, "chat", new=fake_chat):
            return await extract_vacancy("зп от 2000", author_name="A", created_at="2024-01-01")

    result = asyncio.run(_run())
    assert result.action == "update"
    assert fake_chat.await_count == 2  # был повтор


def test_extract_gives_up_after_max_attempts() -> None:
    fake_chat = AsyncMock(return_value="")  # всегда пусто

    async def _run():
        with patch.object(extractor, "chat", new=fake_chat):
            return await extract_vacancy("шум", author_name="A", created_at="2024-01-01")

    result = asyncio.run(_run())
    assert result.action == "none"
    assert fake_chat.await_count == extractor._MAX_ATTEMPTS
