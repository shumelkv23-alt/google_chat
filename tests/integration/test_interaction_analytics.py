"""Интеграция диспатча handle_query → аналитика на реальной БД chatbot_test.

classify_intent замокан (интент подставляем явно — LLM не дёргаем), графики
форсим в текстовый fallback (create_chart_url → None), чтобы не ходить в сеть.
Проверяем сквозной путь: интент → аналитическая функция → текст ответа.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.db.models import Vacancy
from app.db.session import AsyncSessionLocal
from app.llm.intent_router import Intent
from app.services import interaction_handler
from app.services.interaction_handler import handle_query

pytestmark = pytest.mark.anyio

_NOW = datetime.now(timezone.utc)


async def _seed(title: str, **kwargs) -> None:
    days_ago = kwargs.pop("days_ago", 1)
    async with AsyncSessionLocal() as session:
        session.add(
            Vacancy(
                title=title,
                status=kwargs.pop("status", "open"),
                skills=kwargs.pop("skills", []),
                created_at=_NOW - timedelta(days=days_ago),
                **kwargs,
            )
        )
        await session.commit()


@pytest.fixture(autouse=True)
def _no_network_charts(monkeypatch):
    # Форсим текстовый fallback вместо запроса к QuickChart.
    monkeypatch.setattr(
        interaction_handler, "create_chart_url", AsyncMock(return_value=None)
    )


def _force_intent(monkeypatch, intent: Intent) -> None:
    monkeypatch.setattr(
        interaction_handler, "classify_intent", AsyncMock(return_value=intent)
    )


async def test_skill_demand_dispatch(monkeypatch):
    await _seed("v1", skills=["python", "docker"])
    await _seed("v2", skills=["python"])
    _force_intent(monkeypatch, Intent(kind="skill_demand", status="open"))

    payload, _ = await handle_query("какие технологии в топе", None)

    assert "python" in payload["text"]


async def test_salary_by_company_dispatch(monkeypatch):
    await _seed("v1", company="Avito", role_category="backend", salary_max=300)
    _force_intent(monkeypatch, Intent(kind="salary_by_company", status="open"))

    payload, _ = await handle_query("сколько платят по компаниям", None)

    assert "Avito" in payload["text"]


async def test_role_matrix_dispatch(monkeypatch):
    await _seed("v1", role_category="backend", seniority="senior", salary_max=350)
    _force_intent(monkeypatch, Intent(kind="role_matrix", status="open"))

    payload, _ = await handle_query("зарплаты по специальностям", None)

    assert "backend" in payload["text"]
    assert "senior" in payload["text"]


async def test_trends_dispatch_rising(monkeypatch):
    for _ in range(3):
        await _seed("cur", skills=["python"], days_ago=5)
    await _seed("prev", skills=["python"], days_ago=45)
    _force_intent(monkeypatch, Intent(kind="trends", status="open"))

    payload, _ = await handle_query("что растёт", None)

    assert "python" in payload["text"]


async def test_skill_demand_empty_db(monkeypatch):
    _force_intent(monkeypatch, Intent(kind="skill_demand", status="open"))

    payload, _ = await handle_query("какие технологии в топе", None)

    assert "нет данных" in payload["text"].lower()


# --------------------------------------------------------------------------
# count
# --------------------------------------------------------------------------


async def test_count_dispatch_with_results(monkeypatch):
    await _seed("v1", status="open")
    await _seed("v2", status="open")
    _force_intent(monkeypatch, Intent(kind="count", status="open"))

    payload, _ = await handle_query("сколько открытых вакансий", None)

    assert "Нашёл 2" in payload["text"]


async def test_count_dispatch_zero(monkeypatch):
    _force_intent(monkeypatch, Intent(kind="count", status="open"))

    payload, _ = await handle_query("сколько открытых вакансий", None)

    assert "ничего не нашёл" in payload["text"].lower()


# --------------------------------------------------------------------------
# list_recent
# --------------------------------------------------------------------------


async def test_list_recent_dispatch_with_results(monkeypatch):
    await _seed("Python разработчик", days_ago=1)
    _force_intent(monkeypatch, Intent(kind="list_recent", days=7))

    payload, _ = await handle_query("что добавили за неделю", None)

    assert "Python разработчик" in payload["text"]
    assert "добавлено" in payload["text"].lower()


async def test_list_recent_dispatch_empty(monkeypatch):
    _force_intent(monkeypatch, Intent(kind="list_recent", days=7))

    payload, _ = await handle_query("что добавили за неделю", None)

    assert "ничего нового" in payload["text"].lower()


# --------------------------------------------------------------------------
# chart (текстовый fallback — create_chart_url замокан в None)
# --------------------------------------------------------------------------


async def test_chart_dispatch_text_fallback(monkeypatch):
    await _seed("v1", team="Backend")
    await _seed("v2", team="Backend")
    _force_intent(
        monkeypatch, Intent(kind="chart", group_by="team", status="open")
    )

    payload, _ = await handle_query("график по командам", None)

    assert "Backend" in payload["text"]


async def test_chart_dispatch_empty(monkeypatch):
    _force_intent(
        monkeypatch, Intent(kind="chart", group_by="team", status="open")
    )

    payload, _ = await handle_query("график по командам", None)

    assert "нет данных" in payload["text"].lower()


# --------------------------------------------------------------------------
# salary_chart
# --------------------------------------------------------------------------


async def test_salary_chart_dispatch_fallback(monkeypatch):
    await _seed("Senior Go", salary_min=250, salary_max=400, status="open")
    _force_intent(monkeypatch, Intent(kind="salary_chart", status="open"))

    payload, _ = await handle_query("график зарплат", None)

    assert "Senior Go" in payload["text"]
    assert "400" in payload["text"]


async def test_salary_chart_dispatch_empty(monkeypatch):
    await _seed("Без зарплаты", status="open")  # нет salary_min/max → отсеётся
    _force_intent(monkeypatch, Intent(kind="salary_chart", status="open"))

    payload, _ = await handle_query("график зарплат", None)

    assert "не нашёл вакансий" in payload["text"].lower()


# --------------------------------------------------------------------------
# search (RAG-fallback) — build_rag_context / generate_answer замоканы
# --------------------------------------------------------------------------


async def test_search_fallback_returns_answer(monkeypatch):
    _force_intent(monkeypatch, Intent(kind="search"))
    monkeypatch.setattr(
        interaction_handler, "build_rag_context", AsyncMock(return_value="ctx")
    )
    monkeypatch.setattr(
        interaction_handler, "generate_answer", AsyncMock(return_value="Вот ответ")
    )

    payload, turn = await handle_query("открыта ли вакансия питониста", None)

    assert payload["text"] == "Вот ответ"
    assert turn == "Вот ответ"


async def test_search_fallback_handles_error(monkeypatch):
    _force_intent(monkeypatch, Intent(kind="search"))
    monkeypatch.setattr(
        interaction_handler, "build_rag_context", AsyncMock(return_value="ctx")
    )
    monkeypatch.setattr(
        interaction_handler,
        "generate_answer",
        AsyncMock(side_effect=RuntimeError("LLM упал")),
    )

    payload, _ = await handle_query("открыта ли вакансия", None)

    assert "что-то пошло не так" in payload["text"].lower()


# --------------------------------------------------------------------------
# Пустые ответы аналитических веток (форматтеры «нет данных»)
# --------------------------------------------------------------------------


async def test_trends_empty(monkeypatch):
    _force_intent(monkeypatch, Intent(kind="trends", status="open"))

    payload, _ = await handle_query("что растёт", None)

    assert "недостаточно данных" in payload["text"].lower()


async def test_salary_by_company_empty(monkeypatch):
    _force_intent(monkeypatch, Intent(kind="salary_by_company", status="open"))

    payload, _ = await handle_query("сколько платят по компаниям", None)

    assert "нет данных" in payload["text"].lower()


async def test_role_matrix_empty(monkeypatch):
    _force_intent(monkeypatch, Intent(kind="role_matrix", status="open"))

    payload, _ = await handle_query("зарплаты по специальностям", None)

    assert "нет данных" in payload["text"].lower()
