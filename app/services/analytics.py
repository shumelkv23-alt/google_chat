"""Аналитические функции для бота: count, list_recent, group_count.

Используются после intent_router'а: маршрутизация в /chat/interaction решает,
какую из них дёргать, и подставляет параметры из Intent.
"""

from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import text

from app.config import settings
from app.db.session import AsyncSessionLocal

_openai = AsyncOpenAI(api_key=settings.openai_api_key)


_TOPIC_THRESHOLD = 0.5

# Защита от SQL injection при динамической подстановке имени колонки в GROUP BY.
# Маппинг Intent.group_by → реальная колонка таблицы vacancies.
_GROUP_BY_COLUMN = {
    "team": "team",
    "status": "status",
    "owner": "owner_name",
}


async def _embed_topic(topic: str) -> str:
    """Embed темы → строка vector-литерала для подстановки в SQL."""
    resp = await _openai.embeddings.create(
        model=settings.openai_embedding_model, input=topic
    )
    return "[" + ",".join(str(x) for x in resp.data[0].embedding) + "]"


def _build_filters(
    *,
    topic_vec: str | None,
    status: str,
    days: int | None,
    hours: int | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Собирает WHERE-условия и параметры из общих фильтров."""
    # Удалённые вакансии не участвуют ни в одной аналитике.
    parts: list[str] = ["is_deleted = false"]
    params: dict[str, Any] = {}

    if status == "open":
        parts.append("status != 'closed'")
    elif status == "closed":
        parts.append("status = 'closed'")
    # status == "any" → без фильтра

    # hours имеет приоритет над days (мельче — точнее).
    if hours is not None:
        parts.append("created_at >= NOW() - make_interval(hours => :hours)")
        params["hours"] = hours
    elif days is not None:
        parts.append("created_at >= NOW() - make_interval(days => :days)")
        params["days"] = days

    if topic_vec is not None:
        parts.append(
            "embedding IS NOT NULL "
            "AND 1 - (embedding <=> CAST(:vec AS vector(1536))) > :threshold"
        )
        params["vec"] = topic_vec
        params["threshold"] = _TOPIC_THRESHOLD

    return parts, params


async def count_vacancies(
    *,
    topic: str | None = None,
    status: str = "any",
    days: int | None = None,
    hours: int | None = None,
) -> int:
    """Кол-во вакансий с фильтрами. topic фильтруется через cosine similarity."""
    topic_vec = await _embed_topic(topic) if topic else None
    where_parts, params = _build_filters(
        topic_vec=topic_vec, status=status, days=days, hours=hours
    )
    where_sql = " AND ".join(where_parts) if where_parts else "TRUE"

    sql = text(f"SELECT COUNT(*) FROM vacancies WHERE {where_sql}")
    async with AsyncSessionLocal() as session:
        return (await session.execute(sql, params)).scalar_one()


async def list_recent(
    *,
    days: int | None = None,
    hours: int | None = None,
    status: str = "any",
    limit: int = 10,
) -> list[dict]:
    """Список вакансий за последний период. Хотя бы один из days/hours должен быть задан."""
    where_parts, params = _build_filters(
        topic_vec=None, status=status, days=days, hours=hours
    )
    where_sql = " AND ".join(where_parts) if where_parts else "TRUE"
    params["limit"] = limit

    sql = text(
        f"""
        SELECT title, status, team, salary_min, salary_max, currency,
               owner_name, created_at
        FROM vacancies
        WHERE {where_sql}
        ORDER BY created_at DESC
        LIMIT :limit
        """
    )
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).fetchall()
    return [dict(r._mapping) for r in rows]


async def list_vacancy_salaries(
    *,
    topic: str | None = None,
    status: str = "any",
    days: int | None = None,
    hours: int | None = None,
    limit: int = 15,
) -> list[dict]:
    """Список вакансий с зарплатами для построения bar chart.

    Возвращает только вакансии, у которых задана хотя бы одна планка (min или max).
    Сортировка по salary_max DESC (выше — интереснее), NULL в конце.
    """
    topic_vec = await _embed_topic(topic) if topic else None
    where_parts, params = _build_filters(
        topic_vec=topic_vec, status=status, days=days, hours=hours
    )
    where_parts.append("(salary_min IS NOT NULL OR salary_max IS NOT NULL)")
    where_sql = " AND ".join(where_parts)
    params["limit"] = limit

    sql = text(
        f"""
        SELECT title, salary_min, salary_max, currency
        FROM vacancies
        WHERE {where_sql}
        ORDER BY salary_max DESC NULLS LAST, salary_min DESC NULLS LAST
        LIMIT :limit
        """
    )
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).fetchall()
    return [dict(r._mapping) for r in rows]


async def group_count(
    *, group_by: str, status: str = "any"
) -> list[tuple[str, int]]:
    """GROUP BY по полю team/status/owner с подсчётом. Для построения графика.

    Returns: список (label, count), отсортированный по count DESC.
    NULL-значения превращаются в строку 'не указано'.
    """
    if group_by not in _GROUP_BY_COLUMN:
        raise ValueError(f"unsupported group_by: {group_by!r}")
    column = _GROUP_BY_COLUMN[group_by]

    where_parts, params = _build_filters(topic_vec=None, status=status, days=None)
    where_sql = " AND ".join(where_parts) if where_parts else "TRUE"

    sql = text(
        f"""
        SELECT COALESCE({column}, 'не указано') AS label, COUNT(*) AS cnt
        FROM vacancies
        WHERE {where_sql}
        GROUP BY label
        ORDER BY cnt DESC
        """
    )
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).fetchall()
    return [(r.label, r.cnt) for r in rows]
