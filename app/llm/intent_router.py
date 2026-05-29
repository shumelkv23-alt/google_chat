import json
from typing import Literal, Optional

from pydantic import BaseModel, Field, ValidationError

from app.config import settings
from app.llm.client import chat
from app.logger import logger

IntentKind = Literal["search", "count", "list_recent", "chart", "salary_chart"]
StatusFilter = Literal["open", "closed", "any"]
GroupBy = Literal["team", "status", "owner"]
ChartType = Literal["bar", "pie", "line"]


class Intent(BaseModel):
    kind: IntentKind
    topic: Optional[str] = None
    days: Optional[int] = Field(default=None, ge=1, le=365)
    hours: Optional[int] = Field(default=None, ge=1, le=720)
    status: StatusFilter = "any"
    group_by: Optional[GroupBy] = None
    chart_type: Optional[ChartType] = None


_SYSTEM = """\
Ты — классификатор запросов к боту по вакансиям. Определи тип запроса и параметры.
Отвечай ТОЛЬКО валидным JSON без markdown-блоков.

Возможные kind:
- search: семантический поиск ("открыта ли вакансия X", "что слышно про Y", "расскажи о Z")
- count: подсчёт ("сколько вакансий", "сколько открыто X")
- list_recent: список за период ("что добавили за неделю", "новые вакансии за 3 дня")
- chart: распределение по группе ("график", "распределение", "покажи по командам/статусам/овнерам")
- salary_chart: график зарплат по вакансиям ("график зарплат", "сколько платят", "зарплаты по позициям")
  Используй ТОЛЬКО если запрос явно про деньги/оклады/зарплаты — иначе chart.

Поля:
- topic: тема/название для фильтра (опц., если упомянуто: "python", "тимлид")
- days: период в днях (опц., целое 1-365). "неделя"=7, "месяц"=30, "год"=365
- hours: период в часах (опц., целое 1-720). "час"=1, "сутки"=24
  Используй hours для запросов короче суток ("за час", "за 30 минут" → hours=1)
  Если есть hours, days не указывай.
- status: "open" | "closed" | "any" (по умолчанию "any"; для list_recent — "any")
- group_by: для chart — "team" | "status" | "owner"
- chart_type: для chart — "bar" (по умолчанию) | "pie" | "line"

Примеры:
Запрос: "открыта ли вакансия питониста"
{"kind":"search","topic":"питонист"}

Запрос: "сколько вакансий по python открыто"
{"kind":"count","topic":"python","status":"open"}

Запрос: "что добавили за последние 3 дня"
{"kind":"list_recent","days":3,"status":"any"}

Запрос: "сколько вакансий за последний час"
{"kind":"count","hours":1,"status":"any"}

Запрос: "что появилось за последние сутки"
{"kind":"list_recent","hours":24,"status":"any"}

Запрос: "график открытых вакансий по командам"
{"kind":"chart","group_by":"team","chart_type":"bar","status":"open"}

Запрос: "какие новые вакансии за неделю"
{"kind":"list_recent","days":7,"status":"any"}

Запрос: "распределение по статусам"
{"kind":"chart","group_by":"status","chart_type":"pie","status":"any"}

Запрос: "график зарплат по вакансиям"
{"kind":"salary_chart","status":"open"}

Запрос: "сколько платят по открытым вакансиям"
{"kind":"salary_chart","status":"open"}

Запрос: "зарплаты по python вакансиям"
{"kind":"salary_chart","topic":"python","status":"open"}\
"""


async def classify_intent(query: str) -> Intent:
    raw = await chat(
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f'Запрос: "{query}"'},
        ],
        model=settings.openrouter_model_prefilter,
        response_format={"type": "json_object"},
    )

    try:
        data = json.loads(raw.strip())
        return Intent(**data)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("intent_router_parse_error", raw=raw[:300], error=str(exc))
        return Intent(kind="search")
