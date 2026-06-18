"""Юнит-тесты роутера интентов: парсинг JSON-вердикта LLM в Intent.

chat() замокан — проверяем НЕ качество классификации (это eval), а что новые
аналитические kind корректно разбираются в модель и битый JSON безопасно падает
в search.
"""

from unittest.mock import AsyncMock

import pytest

from app.llm.intent_router import classify_intent

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "raw, kind, extra",
    [
        ('{"kind":"skill_demand","status":"open"}', "skill_demand", {}),
        (
            '{"kind":"skill_demand","status":"open","least_demanded":true}',
            "skill_demand",
            {"least_demanded": True},
        ),
        ('{"kind":"trends","status":"open"}', "trends", {}),
        ('{"kind":"salary_by_company","status":"open"}', "salary_by_company", {}),
        ('{"kind":"role_matrix","status":"open"}', "role_matrix", {}),
    ],
)
async def test_classify_parses_new_kinds(monkeypatch, raw, kind, extra):
    monkeypatch.setattr(
        "app.llm.intent_router.chat", AsyncMock(return_value=raw)
    )

    intent = await classify_intent("любой запрос")

    assert intent.kind == kind
    for key, value in extra.items():
        assert getattr(intent, key) == value


async def test_least_demanded_defaults_false(monkeypatch):
    monkeypatch.setattr(
        "app.llm.intent_router.chat",
        AsyncMock(return_value='{"kind":"skill_demand","status":"open"}'),
    )

    intent = await classify_intent("какие технологии в топе")

    assert intent.least_demanded is False


async def test_bad_json_falls_back_to_search(monkeypatch):
    monkeypatch.setattr(
        "app.llm.intent_router.chat", AsyncMock(return_value="это не json")
    )

    intent = await classify_intent("что-то непонятное")

    assert intent.kind == "search"
