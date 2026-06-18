"""Кластерный анализ: зависимость «какой спец × какая зп × какая компания».

Структурный подход (детерминированный SQL GROUP BY + PERCENTILE_CONT для медианы)
поверх нормализованных company / role_category / seniority / salary. Медиана, а
не среднее: один синьор за 500k иначе перекосил бы avg (грабли из roadmap).
NULL-компания/роль не роняет разрез — COALESCE(..., 'не указано').

ML-кластеризация эмбеддингов (Фаза 4b) намеренно НЕ здесь: недетерминирована и
плохо тестируется — сначала закрываем структурный разрез.
"""

from typing import Any

from sqlalchemy import text

from app.db.session import AsyncSessionLocal


def _status_clause(status: str) -> str:
    if status == "open":
        return " AND status != 'closed'"
    if status == "closed":
        return " AND status = 'closed'"
    return ""


async def salary_by_company(
    *,
    role_category: str | None = None,
    seniority: str | None = None,
    status: str = "open",
) -> list[dict]:
    """Зарплатный разрез по компаниям: медиана/мин/макс + число вакансий.

    Опционально сужает выборку по роли и грейду («сколько платят бэкендам-сеньорам
    по компаниям»). Только вакансии хотя бы с одной планкой зарплаты.
    """
    where = (
        "is_deleted = false"
        + _status_clause(status)
        + " AND (salary_min IS NOT NULL OR salary_max IS NOT NULL)"
    )
    params: dict[str, Any] = {}
    if role_category is not None:
        where += " AND role_category = :role"
        params["role"] = role_category
    if seniority is not None:
        where += " AND seniority = :seniority"
        params["seniority"] = seniority

    sql = text(
        f"""
        SELECT COALESCE(company, 'не указано') AS company,
               COUNT(*) AS vacancies,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY salary_min) AS median_min,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY salary_max) AS median_max,
               MIN(salary_min) AS min_salary,
               MAX(salary_max) AS max_salary
        FROM vacancies
        WHERE {where}
        GROUP BY COALESCE(company, 'не указано')
        ORDER BY median_max DESC NULLS LAST, vacancies DESC
        """
    )
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).fetchall()
    return [dict(r._mapping) for r in rows]


async def role_salary_matrix(
    *, status: str = "open", days: int | None = None
) -> list[dict]:
    """Матрица «роль × грейд» с медианной зарплатой и числом вакансий.

    Прямой ответ на «каких спецов ищут и за сколько». Берёт вакансии с верхней
    планкой зарплаты (median по salary_max).
    """
    where = (
        "is_deleted = false" + _status_clause(status) + " AND salary_max IS NOT NULL"
    )
    params: dict[str, Any] = {}
    if days is not None:
        where += " AND created_at >= NOW() - make_interval(days => :days)"
        params["days"] = days

    sql = text(
        f"""
        SELECT COALESCE(role_category, 'не указано') AS role_category,
               COALESCE(seniority, 'не указано') AS seniority,
               COUNT(*) AS vacancies,
               PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY salary_max) AS median_salary
        FROM vacancies
        WHERE {where}
        GROUP BY COALESCE(role_category, 'не указано'),
                 COALESCE(seniority, 'не указано')
        ORDER BY role_category, median_salary DESC NULLS LAST
        """
    )
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).fetchall()
    return [dict(r._mapping) for r in rows]


async def company_demand(*, status: str = "open", limit: int = 10) -> list[dict]:
    """Каких спецов ищет каждая компания: число вакансий + набор ролей.

    Returns: по убыванию числа вакансий — кто активнее всех нанимает.
    """
    where = "is_deleted = false" + _status_clause(status)
    sql = text(
        f"""
        SELECT COALESCE(company, 'не указано') AS company,
               COUNT(*) AS vacancies,
               array_agg(DISTINCT role_category)
                   FILTER (WHERE role_category IS NOT NULL) AS roles
        FROM vacancies
        WHERE {where}
        GROUP BY COALESCE(company, 'не указано')
        ORDER BY vacancies DESC, company
        LIMIT :limit
        """
    )
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, {"limit": limit})).fetchall()
    return [dict(r._mapping) for r in rows]
