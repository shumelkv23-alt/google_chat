"""Сборка графиков через QuickChart.io.

QuickChart принимает chart.js-конфиг → возвращает URL на картинку.
Картинку потом вставляем в Google Chat Cards V2 как image widget.
"""

import json
from typing import Literal

import httpx

from app.logger import logger

ChartType = Literal["bar", "pie", "line"]

# POST /chart/create возвращает {"success": true, "url": "https://quickchart.io/chart/render/..."}
# вместо длинного inline GET — удобнее для логов и кеша.
_QUICKCHART_URL = "https://quickchart.io/chart/create"
_HTTP_TIMEOUT = 10.0


def build_chart_config(
    data: list[tuple[str, int]],
    chart_type: ChartType = "bar",
    title: str = "",
) -> dict:
    """Из результата group_count собирает chart.js-конфиг."""
    labels = [label for label, _ in data]
    values = [count for _, count in data]

    config: dict = {
        "type": chart_type,
        "data": {
            "labels": labels,
            "datasets": [{"label": title or "Вакансии", "data": values}],
        },
        "options": {
            "title": {"display": bool(title), "text": title},
            "legend": {"display": chart_type == "pie"},
        },
    }
    return config


def build_salary_chart_config(rows: list[dict], title: str = "") -> dict:
    labels = [r.get("title") or "?" for r in rows]
    mins = [r.get("salary_min") for r in rows]
    maxs = [r.get("salary_max") for r in rows]

    currencies = [r.get("currency") or "RUB" for r in rows]
    currency = max(set(currencies), key=currencies.count) if currencies else "RUB"

    config: dict = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [
                {"label": f"min, {currency}", "data": mins},
                {"label": f"max, {currency}", "data": maxs},
            ],
        },
        "options": {
            "title": {"display": bool(title), "text": title},
            "legend": {"display": True},
        },
    }
    return config


async def create_chart_url(config: dict) -> str | None:
    """Отправляет конфиг в QuickChart → возвращает URL картинки. None при ошибке."""
    payload = {"chart": config, "backgroundColor": "white", "format": "png"}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(_QUICKCHART_URL, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        logger.warning("quickchart_request_failed", error=str(exc))
        return None

    if not data.get("success"):
        logger.warning("quickchart_no_success", body=data)
        return None
    return data.get("url")
