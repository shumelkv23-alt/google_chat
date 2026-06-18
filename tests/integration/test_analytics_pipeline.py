"""Интеграция аналитики трендов и кластеров на реальной БД chatbot_test.

Засеваем вакансии напрямую (минуя пайплайн — здесь проверяем SQL аналитики, а не
извлечение) с контролируемыми skills/company/role/seniority/salary/created_at и
сверяем агрегации с посчитанными руками. Граничные случаи (пусто/NULL) — отдельно.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Vacancy
from app.db.session import AsyncSessionLocal
from app.services import clusters, trends

pytestmark = pytest.mark.anyio

_NOW = datetime.now(timezone.utc)


async def _seed(
    title: str,
    *,
    skills: list[str] | None = None,
    company: str | None = None,
    role_category: str | None = None,
    seniority: str | None = None,
    salary_min: int | None = None,
    salary_max: int | None = None,
    status: str = "open",
    days_ago: int = 1,
) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            Vacancy(
                title=title,
                status=status,
                skills=skills or [],
                company=company,
                role_category=role_category,
                seniority=seniority,
                salary_min=salary_min,
                salary_max=salary_max,
                created_at=_NOW - timedelta(days=days_ago),
            )
        )
        await session.commit()


# --------------------------------------------------------------------------
# Тренды
# --------------------------------------------------------------------------


async def test_top_skills_ranks_by_demand():
    await _seed("v1", skills=["python", "docker"])
    await _seed("v2", skills=["python", "postgresql"])
    await _seed("v3", skills=["python"])
    await _seed("v4", skills=["docker"])

    result = await trends.top_skills(status="open", limit=10)

    assert result[0] == ("python", 3)
    assert ("docker", 2) in result
    assert ("postgresql", 1) in result


async def test_top_skills_ascending_returns_least_demanded():
    await _seed("v1", skills=["python", "python"])  # дедуп на записи нерелевантен — массив как есть
    await _seed("v2", skills=["python"])
    await _seed("v3", skills=["rust"])

    least = await trends.top_skills(status="open", ascending=True, limit=1)

    assert least == [("rust", 1)]


async def test_top_skills_respects_status_filter():
    await _seed("open1", skills=["go"], status="open")
    await _seed("open2", skills=["go"], status="open")
    await _seed("closed1", skills=["go"], status="closed")

    assert await trends.top_skills(status="open") == [("go", 2)]
    assert await trends.top_skills(status="any") == [("go", 3)]


async def test_rising_falling_detects_trends():
    # Окно month = 30 дней. cur: days_ago<30, prev: 30<=days_ago<60.
    for _ in range(3):
        await _seed("p_cur", skills=["python"], days_ago=5)
    await _seed("p_prev", skills=["python"], days_ago=45)

    await _seed("j_cur", skills=["java"], days_ago=5)
    for _ in range(4):
        await _seed("j_prev", skills=["java"], days_ago=45)

    # Шум: ниже min_count в обоих окнах — не должен попасть никуда.
    await _seed("noise", skills=["rust"], days_ago=5)

    result = await trends.rising_falling(bucket="month", min_count=3)

    rising_skills = {e["skill"] for e in result["rising"]}
    falling_skills = {e["skill"] for e in result["falling"]}
    assert "python" in rising_skills
    assert "java" in falling_skills
    assert "rust" not in rising_skills | falling_skills


async def test_skill_trend_returns_chronological_buckets():
    await _seed("a", skills=["python"], days_ago=2)
    await _seed("b", skills=["python"], days_ago=2)
    await _seed("c", skills=["python"], days_ago=40)

    series = await trends.skill_trend(skill="python", bucket="month", windows=6)

    counts = [cnt for _, cnt in series]
    assert sum(counts) == 3
    # Хронология: старый бакет раньше свежего.
    dates = [b for b, _ in series]
    assert dates == sorted(dates)


# --------------------------------------------------------------------------
# Кластеры
# --------------------------------------------------------------------------


async def test_salary_by_company_uses_median():
    await _seed("a1", company="A", role_category="backend", salary_max=100)
    await _seed("a2", company="A", role_category="backend", salary_max=200)
    await _seed("a3", company="A", role_category="backend", salary_max=300)
    await _seed("b1", company="B", role_category="backend", salary_max=500)

    rows = await clusters.salary_by_company(status="open")
    by_company = {r["company"]: r for r in rows}

    assert by_company["A"]["median_max"] == 200  # медиана [100,200,300]
    assert by_company["A"]["vacancies"] == 3
    assert by_company["B"]["median_max"] == 500
    # Сортировка по median_max DESC → B выше A.
    assert rows[0]["company"] == "B"


async def test_salary_by_company_filters_role_and_seniority():
    await _seed("s1", company="A", role_category="backend", seniority="senior", salary_max=300)
    await _seed("j1", company="A", role_category="backend", seniority="junior", salary_max=100)
    await _seed("f1", company="A", role_category="frontend", seniority="senior", salary_max=250)

    rows = await clusters.salary_by_company(
        role_category="backend", seniority="senior"
    )

    assert len(rows) == 1
    assert rows[0]["median_max"] == 300


async def test_role_salary_matrix_groups_by_role_and_grade():
    await _seed("1", role_category="backend", seniority="senior", salary_max=300)
    await _seed("2", role_category="backend", seniority="junior", salary_max=150)
    await _seed("3", role_category="frontend", seniority="middle", salary_max=200)

    rows = await clusters.role_salary_matrix(status="open")
    cells = {(r["role_category"], r["seniority"]): r for r in rows}

    assert cells[("backend", "senior")]["median_salary"] == 300
    assert cells[("backend", "junior")]["median_salary"] == 150
    assert cells[("frontend", "middle")]["vacancies"] == 1


async def test_company_demand_counts_and_collects_roles():
    await _seed("a1", company="A", role_category="backend")
    await _seed("a2", company="A", role_category="backend")
    await _seed("a3", company="A", role_category="frontend")
    await _seed("b1", company="B", role_category="ml")

    rows = await clusters.company_demand(status="open")
    by_company = {r["company"]: r for r in rows}

    assert rows[0]["company"] == "A"  # больше вакансий
    assert by_company["A"]["vacancies"] == 3
    assert set(by_company["A"]["roles"]) == {"backend", "frontend"}
    assert by_company["B"]["vacancies"] == 1


# --------------------------------------------------------------------------
# Граничные случаи: пусто / NULL не роняют аналитику
# --------------------------------------------------------------------------


async def test_empty_db_returns_empty():
    assert await trends.top_skills(status="open") == []
    assert await clusters.salary_by_company() == []
    assert await clusters.company_demand() == []
    assert await trends.rising_falling() == {"rising": [], "falling": []}


async def test_null_company_and_role_do_not_crash():
    await _seed("x", company=None, role_category=None, salary_max=200)

    salary_rows = await clusters.salary_by_company(status="open")
    demand_rows = await clusters.company_demand(status="open")

    assert salary_rows[0]["company"] == "не указано"
    assert demand_rows[0]["company"] == "не указано"
    # role_category был NULL → набор ролей пуст (FILTER отсёк NULL).
    assert demand_rows[0]["roles"] is None or demand_rows[0]["roles"] == []
