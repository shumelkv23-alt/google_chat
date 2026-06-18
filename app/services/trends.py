"""Аналитика трендов спроса на технологии.

Поверх нормализованного vacancies.skills (TEXT[]) и created_at. Запросы
детерминированные (SQL GROUP BY + date_trunc по unnest скиллов). Логика
«растёт/падает» вынесена в чистую compute_trend_direction — юнит-тест без БД.

Грабли (см. docs/analytics_roadmap.md): на малых данных тренд — это шум, поэтому
rising_falling режет по min_count. Bucket валидируется по白списку.
"""

from typing import Any

from sqlalchemy import text

from app.db.session import AsyncSessionLocal

# Один бакет тренда → длина в днях. month≈30 — для бакетирования спроса достаточно.
_BUCKET_DAYS: dict[str, int] = {"day": 1, "week": 7, "month": 30}


def _status_clause(status: str) -> str:
    """Доп. WHERE по статусу. open → активные, closed → закрытые, any → без фильтра."""
    if status == "open":
        return " AND status != 'closed'"
    if status == "closed":
        return " AND status = 'closed'"
    return ""


def compute_trend_direction(prev: int, cur: int) -> dict[str, Any]:
    """Направление и величина изменения спроса между двумя окнами.

    Чистая функция (без БД) — ядро rising_falling, тестируется изолированно.
    growth — относительный прирост (cur-prev)/prev. Для prev=0 при cur>0 роста в
    процентах нет (деление на ноль) → growth=None, direction="new".
    """
    delta = cur - prev
    if prev == 0:
        growth = None
        direction = "new" if cur > 0 else "flat"
    else:
        growth = delta / prev
        direction = "rising" if delta > 0 else "falling" if delta < 0 else "flat"
    return {"prev": prev, "cur": cur, "delta": delta, "growth": growth, "direction": direction}


async def top_skills(
    *,
    status: str = "open",
    days: int | None = None,
    limit: int = 15,
    ascending: bool = False,
) -> list[tuple[str, int]]:
    """Технологии по числу вакансий. ascending=False — востребованные (топ),
    ascending=True — менее востребованные (хвост). Returns: (skill, count)."""
    where = "is_deleted = false" + _status_clause(status)
    params: dict[str, Any] = {"limit": limit}
    if days is not None:
        where += " AND created_at >= NOW() - make_interval(days => :days)"
        params["days"] = days

    order = "ASC" if ascending else "DESC"
    sql = text(
        f"""
        SELECT skill, COUNT(*) AS cnt
        FROM vacancies, unnest(skills) AS skill
        WHERE {where}
        GROUP BY skill
        ORDER BY cnt {order}, skill
        LIMIT :limit
        """
    )
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).fetchall()
    return [(r.skill, r.cnt) for r in rows]


async def skill_trend(
    *, skill: str, bucket: str = "week", windows: int = 8, status: str = "open"
) -> list[tuple[Any, int]]:
    """Временной ряд спроса на одну технологию: последние `windows` бакетов.

    Returns: список (дата_начала_бакета, count) в хронологическом порядке.
    """
    if bucket not in _BUCKET_DAYS:
        raise ValueError(f"unsupported bucket: {bucket!r}")
    where = "is_deleted = false AND :skill = ANY(skills)" + _status_clause(status)
    sql = text(
        f"""
        SELECT date_trunc(:bucket, created_at)::date AS b, COUNT(*) AS cnt
        FROM vacancies
        WHERE {where}
        GROUP BY b
        ORDER BY b DESC
        LIMIT :windows
        """
    )
    params = {"skill": skill, "bucket": bucket, "windows": windows}
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).fetchall()
    # БД отдала свежие сверху (DESC + LIMIT) — разворачиваем в хронологию.
    return [(r.b, r.cnt) for r in reversed(rows)]


async def rising_falling(
    *, bucket: str = "month", status: str = "open", min_count: int = 3, limit: int = 10
) -> dict[str, list[dict]]:
    """Восходящие / нисходящие технологии: текущее окно против предыдущего.

    Окно = один бакет. Сравниваем спрос за [now-бакет; now] с [now-2*бакет;
    now-бакет]. min_count отсекает шум (тренд на 1-2 вакансиях не считаем).
    Returns: {"rising": [...], "falling": [...]} с полями skill + метрики тренда.
    """
    if bucket not in _BUCKET_DAYS:
        raise ValueError(f"unsupported bucket: {bucket!r}")
    span = _BUCKET_DAYS[bucket]
    where = "is_deleted = false" + _status_clause(status)
    sql = text(
        f"""
        SELECT skill,
               COUNT(*) FILTER (
                   WHERE created_at >= NOW() - make_interval(days => :win)
               ) AS cur,
               COUNT(*) FILTER (
                   WHERE created_at >= NOW() - make_interval(days => :win2)
                     AND created_at <  NOW() - make_interval(days => :win)
               ) AS prev
        FROM vacancies, unnest(skills) AS skill
        WHERE {where}
          AND created_at >= NOW() - make_interval(days => :win2)
        GROUP BY skill
        """
    )
    params = {"win": span, "win2": span * 2}
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).fetchall()

    rising: list[dict] = []
    falling: list[dict] = []
    for r in rows:
        # Шум: отбрасываем технологии, где оба окна ниже порога.
        if max(r.cur, r.prev) < min_count:
            continue
        trend = compute_trend_direction(r.prev, r.cur)
        entry = {"skill": r.skill, **trend}
        if trend["direction"] in ("rising", "new"):
            rising.append(entry)
        elif trend["direction"] == "falling":
            falling.append(entry)

    # Растущие — по абсолютному приросту вниз; новые (growth=None) — наверх.
    rising.sort(key=lambda e: (e["growth"] is not None, -e["delta"]))
    falling.sort(key=lambda e: e["delta"])
    return {"rising": rising[:limit], "falling": falling[:limit]}
