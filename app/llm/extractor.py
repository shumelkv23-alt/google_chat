import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.llm.client import chat
from app.logger import logger

_SYSTEM_BASE = """\
Ты — парсер сообщений о вакансиях в корпоративном чате.
Отвечай ТОЛЬКО валидным JSON без markdown-блоков.

Поля action: create | update | close | none
Поля fields: title, salary_min, salary_max, currency, status, owner, team, description.
Если поле не упомянуто — НЕ включай его в fields (не выдумывай).\
"""

_SYSTEM_FULL = """\
Ты — парсер сообщений о вакансиях в корпоративном чате.
Отвечай ТОЛЬКО валидным JSON без markdown-блоков.

Тебе могут дать:
1. Список открытых вакансий из БД ("Сейчас открыты позиции") — это источник истины
   о том, что сейчас активно обсуждается в фирме. Используй его в первую очередь
   для определения action и entity_ref.
2. Последние сообщения из чата ("Контекст") — окно недавних обсуждений.
3. Новое сообщение — то, что нужно распарсить.

Поля action (hint — финальное решение принимает другой модуль на основе БД):
- create: явное объявление НОВОЙ позиции, которой нет в списке открытых
  ("открыли вакансию X", "ищем Y" — и X/Y не совпадает с открытыми)
- update: уточнение условий или статуса позиции из открытых или контекста
  (зарплата, требования, команда, овнер; "по X подняли до 350k", "зп от 2000")
- close: явное закрытие позиции ("закрыли X", "вышел кандидат на Y")
- none: сообщение не про вакансии

ВАЖНО:
- Если новое сообщение упоминает или относится к одной из открытых вакансий —
  action = update или close (не create).
- Если в сообщении ЕСТЬ данные о позиции (зарплата, команда, требования) —
  action НЕ может быть "none". Короткий follow-up без названия ("зп от 2000",
  "на удалёнку") при наличии открытых вакансий — это update самой свежей/похожей.
- Сомневаешься между create и update → ставь update.

Поля entity_ref — короткая идентификационная фраза:
- create: название новой позиции из текущего сообщения
- update/close: title из списка открытых вакансий, к которой относится сообщение
  (если в БД нет — бери название из контекста)

Поля fields: title, salary_min, salary_max, currency, status, owner, team, description.
Если поле не упомянуто — НЕ включай его в fields.
confidence: 0.0–1.0 — насколько ты уверен в action и entity_ref.
Если action != "none", confidence должен быть > 0.3.\
"""


class ExtractionResult(BaseModel):
    action: Literal["create", "update", "close", "none"]
    entity_ref: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0


def _format_open_vacancies(vacancies: list[dict]) -> str:
    lines = ["Сейчас открыты позиции (из БД):"]
    for v in vacancies:
        salary = ""
        lo, hi = v.get("salary_min"), v.get("salary_max")
        if lo or hi:
            cur = v.get("currency") or "RUB"
            salary = f", {lo or '?'}–{hi or '?'} {cur}"
        team = f", команда: {v['team']}" if v.get("team") else ""
        status = v.get("status") or "open"
        lines.append(f"- {v['title']} [{status}]{salary}{team}")
    return "\n".join(lines)


def _format_context_messages(messages: list[dict]) -> str:
    lines = ["Контекст (последние сообщения из чата):"]
    for m in messages:
        lines.append(f"- {m.get('author_name') or 'unknown'}: {m['text']}")
    return "\n".join(lines)


async def extract_vacancy(
    text: str,
    author_name: str | None,
    created_at: str,
    context_messages: list[dict] | None = None,
    open_vacancies: list[dict] | None = None,
) -> ExtractionResult:
    blocks: list[str] = []
    if open_vacancies:
        blocks.append(_format_open_vacancies(open_vacancies))
    if context_messages:
        blocks.append(_format_context_messages(context_messages))
    blocks.append(
        f'Новое сообщение от {author_name or "unknown"} ({created_at}):\n"{text}"'
    )

    system = _SYSTEM_FULL if (open_vacancies or context_messages) else _SYSTEM_BASE
    user_content = "\n\n".join(blocks)

    raw = await chat(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        model=settings.openrouter_model_extract,
        response_format={"type": "json_object"},
    )

    try:
        data = json.loads(raw.strip())
        return ExtractionResult(**data)
    except Exception as exc:
        logger.warning("extractor_parse_error", raw=raw[:500], error=str(exc))
        return ExtractionResult(action="none", confidence=0.0)
