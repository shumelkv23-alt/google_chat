"""Маршрутизация запросов бота: intent_router → analytics / charts / RAG.

Точка входа — handle_query(query, conversation). Возвращает (payload, turn_text):
- payload: dict для Google Chat (либо {"text": ...} либо {"cardsV2": [...]})
- turn_text: строка для лога диалога в conversations.recent_turns
"""

from datetime import datetime

from app.db.models import Conversation
from app.llm.answerer import generate_answer
from app.llm.intent_router import Intent, classify_intent
from app.logger import logger
from app.services.analytics import (
    count_vacancies,
    group_count,
    list_recent,
    list_vacancy_salaries,
)
from app.services.chat_cards import build_chart_card
from app.services.charts import (
    build_chart_config,
    build_salary_chart_config,
    create_chart_url,
)
from app.services.rag import build_rag_context

_LIST_RECENT_LIMIT = 10
_LIST_RECENT_DEFAULT_DAYS = 7
_CHART_DEFAULT_GROUP_BY = "team"
_CHART_DEFAULT_TYPE = "bar"


async def handle_query(
    query: str, conversation: Conversation | None
) -> tuple[dict, str]:
    intent = await classify_intent(query)
    logger.info(
        "interaction_intent",
        kind=intent.kind,
        topic=intent.topic,
        days=intent.days,
        status=intent.status,
        group_by=intent.group_by,
    )

    if intent.kind == "count":
        return await _handle_count(intent)
    if intent.kind == "list_recent":
        return await _handle_list_recent(intent)
    if intent.kind == "chart":
        return await _handle_chart(intent)
    if intent.kind == "salary_chart":
        return await _handle_salary_chart(intent)
    return await _handle_search(query, conversation)


async def _handle_search(
    query: str, conversation: Conversation | None
) -> tuple[dict, str]:
    """Семантический RAG — текущая логика."""
    try:
        context = await build_rag_context(query, conversation)
        answer = await generate_answer(query, context)
    except Exception as exc:
        logger.error("interaction_rag_failed", error=str(exc))
        answer = "Извини, что-то пошло не так. Попробуй ещё раз."
    return {"text": answer}, answer


async def _handle_count(intent: Intent) -> tuple[dict, str]:
    n = await count_vacancies(
        topic=intent.topic,
        status=intent.status,
        days=intent.days,
        hours=intent.hours,
    )
    parts = []
    if intent.topic:
        parts.append(f"по «{intent.topic}»")
    if intent.status == "open":
        parts.append("открытых")
    elif intent.status == "closed":
        parts.append("закрытых")
    period_phrase = _period_phrase(intent.days, intent.hours)
    if period_phrase:
        parts.append(period_phrase)
    qualifier = " ".join(parts) or "всего"

    if n == 0:
        text = f"По таким условиям ({qualifier}) ничего не нашёл."
    else:
        text = f"Нашёл {n} {_pluralize_vacancies(n)} {qualifier}."
    return {"text": text}, text


async def _handle_list_recent(intent: Intent) -> tuple[dict, str]:
    if intent.days is None and intent.hours is None:
        days, hours = _LIST_RECENT_DEFAULT_DAYS, None
    else:
        days, hours = intent.days, intent.hours
    rows = await list_recent(
        days=days, hours=hours, status=intent.status, limit=_LIST_RECENT_LIMIT
    )
    period_phrase = _period_phrase(days, hours) or "за последние 7 дней"

    if not rows:
        text = f"{period_phrase.capitalize()} ничего нового."
        return {"text": text}, text

    lines = [
        f"{period_phrase.capitalize()} добавлено {len(rows)} {_pluralize_vacancies(len(rows))}:"
    ]
    for r in rows:
        lines.append(_format_vacancy_line(r))
    text = "\n".join(lines)
    return {"text": text}, text


async def _handle_chart(intent: Intent) -> tuple[dict, str]:
    group_by = intent.group_by or _CHART_DEFAULT_GROUP_BY
    chart_type = intent.chart_type or _CHART_DEFAULT_TYPE
    data = await group_count(group_by=group_by, status=intent.status)

    if not data:
        text = "Нет данных для графика."
        return {"text": text}, text

    title = _chart_title(group_by, intent.status)
    config = build_chart_config(data, chart_type=chart_type, title=title)
    url = await create_chart_url(config)

    if url is None:
        # Fallback на текстовый ответ — лучше дать числа, чем ничего.
        text = _format_chart_fallback(data, title)
        return {"text": text}, text

    summary = ", ".join(f"{label}: {cnt}" for label, cnt in data[:5])
    card_payload = build_chart_card(title=title, image_url=url, summary=summary)
    return card_payload, f"[график] {title}: {summary}"


async def _handle_salary_chart(intent: Intent) -> tuple[dict, str]:
    rows = await list_vacancy_salaries(
        topic=intent.topic,
        status=intent.status,
        days=intent.days,
        hours=intent.hours,
    )
    if not rows:
        text = "Не нашёл вакансий с указанными зарплатами."
        return {"text": text}, text

    scope = {"open": "открытых", "closed": "закрытых", "any": "всех"}[intent.status]
    title = f"Зарплаты по {scope} вакансиям"
    if intent.topic:
        title += f" (тема: {intent.topic})"

    config = build_salary_chart_config(rows, title=title)
    url = await create_chart_url(config)

    if url is None:
        text = _format_salary_fallback(rows, title)
        return {"text": text}, text

    top_summary = ", ".join(
        f"{r['title']}: {_format_salary_range(r)}" for r in rows[:3]
    )
    card_payload = build_chart_card(title=title, image_url=url, summary=top_summary)
    return card_payload, f"[график зарплат] {top_summary}"


def _format_salary_range(row: dict) -> str:
    lo, hi = row.get("salary_min"), row.get("salary_max")
    cur = row.get("currency") or "RUB"
    if lo and hi:
        return f"{lo}–{hi} {cur}"
    if hi:
        return f"до {hi} {cur}"
    if lo:
        return f"от {lo} {cur}"
    return "не указано"


def _format_salary_fallback(rows: list[dict], title: str) -> str:
    lines = [title + ":"]
    for r in rows:
        lines.append(f"- {r.get('title') or '?'}: {_format_salary_range(r)}")
    return "\n".join(lines)


def _pluralize_vacancies(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "вакансию"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "вакансии"
    return "вакансий"


def _pluralize_hours(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "час"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "часа"
    return "часов"


def _pluralize_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "дня"
    return "дней"


def _period_phrase(days: int | None, hours: int | None) -> str | None:
    """Готовая русская фраза «за последний час / за последние 3 дня». None если период не задан."""
    if hours is not None:
        unit = _pluralize_hours(hours)
        prefix = "за последний" if hours == 1 else "за последние"
        return f"{prefix} {hours} {unit}"
    if days is not None:
        unit = _pluralize_days(days)
        prefix = "за последний" if days == 1 else "за последние"
        return f"{prefix} {days} {unit}"
    return None


def _format_vacancy_line(row: dict) -> str:
    title = row.get("title") or "?"
    status = row.get("status") or "open"
    team = f", команда: {row['team']}" if row.get("team") else ""
    salary = ""
    lo, hi = row.get("salary_min"), row.get("salary_max")
    if lo or hi:
        cur = row.get("currency") or "RUB"
        salary = f", {lo or '?'}–{hi or '?'} {cur}"
    created = row.get("created_at")
    when = ""
    if isinstance(created, datetime):
        when = f" ({created.strftime('%Y-%m-%d')})"
    return f"- {title} [{status}]{salary}{team}{when}"


def _chart_title(group_by: str, status: str) -> str:
    scope = {"open": "открытых", "closed": "закрытых", "any": "всех"}[status]
    by = {"team": "по командам", "status": "по статусам", "owner": "по овнерам"}[group_by]
    return f"Распределение {scope} вакансий {by}"


def _format_chart_fallback(data: list[tuple[str, int]], title: str) -> str:
    lines = [title + ":"]
    for label, cnt in data:
        lines.append(f"- {label}: {cnt}")
    return "\n".join(lines)
