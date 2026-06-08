"""Юнит-тесты batch-экстрактора (разбор пачки одним LLM-контекстом).

Сеть/БД не нужны: chat (OpenRouter) мокается. Проверяем парсинг списка
действий, нормализацию зарплат, устойчивость к мусору и разметку промпта.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from app.llm import batch_extractor
from app.llm.batch_extractor import extract_batch

_MSGS = [
    {"message_index": 0, "author_name": "Иван", "text": "открыли питониста 300к",
     "thread_label": "A", "reply_to_index": None, "reply_to_text": None},
    {"message_index": 1, "author_name": "Пётр", "text": "нужен фронтендер",
     "thread_label": None, "reply_to_index": None, "reply_to_text": None},
    {"message_index": 2, "author_name": "Иван", "text": "подняли до 350",
     "thread_label": "A", "reply_to_index": 0, "reply_to_text": None},
]


def _run(fake_response: str, messages, open_vacancies=None):
    fake = AsyncMock(return_value=fake_response)
    with patch.object(batch_extractor, "chat", new=fake):
        return asyncio.run(extract_batch(messages, open_vacancies)), fake


def test_extract_batch_parses_items_and_normalizes_salary():
    resp = (
        '{"items":['
        '{"message_index":0,"action":"create","entity_ref":"питонист",'
        '"fields":{"salary_min":"300к"},"confidence":0.9},'
        '{"message_index":2,"action":"update","entity_ref":"питонист",'
        '"fields":{"salary_max":350000},"confidence":0.8}]}'
    )
    results, _ = _run(resp, _MSGS)
    assert len(results) == 2
    assert results[0].message_index == 0
    assert results[0].action == "create"
    assert results[0].fields["salary_min"] == 300000  # "300к" → 300000
    assert results[1].message_index == 2
    assert results[1].entity_ref == "питонист"


def test_extract_batch_skips_invalid_item():
    # Второй item без message_index невалиден — первый должен уцелеть.
    resp = (
        '{"items":['
        '{"message_index":0,"action":"create","entity_ref":"го","confidence":0.7},'
        '{"action":"update","entity_ref":"бок"}]}'
    )
    results, _ = _run(resp, _MSGS)
    assert len(results) == 1
    assert results[0].entity_ref == "го"


def test_extract_batch_falls_back_on_garbage():
    results, _ = _run("не json", _MSGS)
    assert results == []


def test_extract_batch_empty_messages_skips_llm():
    fake = AsyncMock(return_value='{"items":[]}')
    with patch.object(batch_extractor, "chat", new=fake):
        results = asyncio.run(extract_batch([], None))
    assert results == []
    fake.assert_not_called()  # пустой вход → LLM не дёргаем


def test_batch_prompt_includes_thread_and_quote_marks():
    _, fake = _run('{"items":[]}', _MSGS)
    user_content = fake.call_args.kwargs["messages"][1]["content"]
    assert "тред A" in user_content
    assert "↳ ответ на [0]" in user_content
    assert "[2]" in user_content
