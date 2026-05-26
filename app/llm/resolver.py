"""LLM entity resolution: сопоставляет entity_ref с существующими вакансиями."""

import json

from pydantic import BaseModel

from app.config import settings
from app.llm.client import chat

_SYSTEM = """\
Ты — система сопоставления упоминаний вакансий в корпоративном чате с существующими записями.

Тебе дают JSON с полями:
- entity_ref: как упомянута вакансия в новом сообщении
- candidates: список существующих вакансий (id, title, team, description, status)
- text: полный текст нового сообщения

Определи, совпадает ли entity_ref с одной из вакансий-кандидатов.
Отвечай ТОЛЬКО валидным JSON без markdown-блоков.

Поля ответа:
- vacancy_id: id совпавшей вакансии (строка) или null если совпадения нет
- confidence: уверенность от 0.0 до 1.0\
"""


class ResolutionResult(BaseModel):
    vacancy_id: str | None = None
    confidence: float = 0.0


async def resolve_entity(
    entity_ref: str,
    candidates: list[dict],
    original_text: str,
) -> ResolutionResult:
    user_content = json.dumps(
        {"entity_ref": entity_ref, "candidates": candidates, "text": original_text},
        ensure_ascii=False,
    )

    raw = await chat(
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_content},
        ],
        model=settings.openrouter_model_resolve,
        response_format={"type": "json_object"},
        reasoning_effort="high",
    )

    try:
        data = json.loads(raw.strip())
        return ResolutionResult(**data)
    except Exception:
        return ResolutionResult(vacancy_id=None, confidence=0.0)
