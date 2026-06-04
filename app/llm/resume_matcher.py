import json

from pydantic import BaseModel, ValidationError

from app.config import settings
from app.llm.client import chat
from app.logger import logger


class VacancyMatch(BaseModel):
    vacancy_id: str
    score: int  
    reason: str  


class MatchRanking(BaseModel):
    matches: list[VacancyMatch] = []


_SYSTEM = """\
Ты — рекрутёр-ассистент. На входе резюме кандидата и список вакансий-кандидатов
(каждая с id и деталями). Оцени, насколько каждая вакансия подходит кандидату.

Отвечай ТОЛЬКО валидным JSON без markdown-блоков. Формат:
{"matches":[{"vacancy_id":"<id>","score":<целое 0-100>,"reason":"<кратко по-русски>"}]}

Правила:
- score: 0-100, насколько кандидат подходит под вакансию (навыки, опыт, стек, уровень).
- reason: 1-2 предложения — почему подходит и чего не хватает.
- Используй ТОЛЬКО переданные vacancy_id, не выдумывай свои.
- Сортируй matches по убыванию score.
- Не подходящие совсем (score < 30) можно не включать.
- Если ничего не подходит — верни {"matches":[]}.\
"""


def _format_candidates(candidates: list[dict]) -> str:
    lines = []
    for c in candidates:
        parts = [f"id={c['id']}", f"title={c.get('title') or '?'}"]
        if c.get("team"):
            parts.append(f"team={c['team']}")
        lo, hi = c.get("salary_min"), c.get("salary_max")
        if lo or hi:
            cur = c.get("currency") or "RUB"
            parts.append(f"salary={lo or '?'}-{hi or '?'} {cur}")
        if c.get("description"):
            parts.append(f"description={c['description'].strip()}")
        lines.append("- " + ", ".join(parts))
    return "\n".join(lines)


async def rank_matches(resume_text: str, candidates: list[dict]) -> MatchRanking:
    user = (
        f"Резюме кандидата:\n{resume_text}\n\n"
        f"Вакансии-кандидаты:\n{_format_candidates(candidates)}"
    )
    raw = await chat(
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user},
        ],
        model=settings.openrouter_model_answer,
        response_format={"type": "json_object"},
    )

    try:
        data = json.loads(raw.strip())
        return MatchRanking(**data)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("resume_matcher_parse_error", raw=raw[:300], error=str(exc))
        return MatchRanking()
