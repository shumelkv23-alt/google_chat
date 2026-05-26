"""Extraction: извлекает структурированные данные о вакансии из текста сообщения."""

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.llm.client import chat

_SYSTEM = """\
Ты — парсер сообщений о вакансиях в корпоративном чате.
Твоя задача — извлечь структурированную информацию.
Отвечай ТОЛЬКО валидным JSON без markdown-блоков.

Поля action: create | update | close | none
Поля fields: title, salary_min, salary_max, currency, status, owner, team, description
Если поле неизвестно — не включай его в fields.\
"""

_SYSTEM_WITH_CONTEXT = """\
Ты — парсер сообщений о вакансиях в корпоративном чате.
Тебе дают последние сообщения из чата (контекст) и новое сообщение.
Используй контекст чтобы понять, к какой вакансии относится новое сообщение, и заполни entity_ref.
Отвечай ТОЛЬКО валидным JSON без markdown-блоков.

Поля action: create | update | close | none
Поля entity_ref: название вакансии из контекста, к которой относится сообщение
Поля fields: title, salary_min, salary_max, currency, status, owner, team, description
Если поле неизвестно — не включай его в fields.\
"""


class ExtractionResult(BaseModel):
    action: Literal["create", "update", "close", "none"]
    entity_ref: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0


async def extract_vacancy(
    text: str,
    author_name: str | None,
    created_at: str,
    context_messages: list[dict] | None = None,
) -> ExtractionResult:
    if context_messages:
        context_block = "Контекст (предыдущие сообщения из чата):\n"
        for m in context_messages:
            context_block += f"- {m.get('author_name') or 'unknown'}: {m['text']}\n"
        user_content = (
            f"{context_block}\n"
            f'Новое сообщение от {author_name or "unknown"} ({created_at}):\n"{text}"'
        )
        system = _SYSTEM_WITH_CONTEXT
    else:
        user_content = f'Сообщение от {author_name or "unknown"} ({created_at}):\n"{text}"'
        system = _SYSTEM

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
    except Exception:
        return ExtractionResult(action="none", confidence=0.0)
