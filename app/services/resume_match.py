from openai import AsyncOpenAI
from sqlalchemy import text

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.llm.resume_matcher import MatchRanking, rank_matches
from app.logger import logger

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

_TOP_K = 5
_MAX_RESUME_CHARS = 8000
_RESUME_PREFIX = "резюме"


def detect_resume_text(message_text: str) -> str | None:
    stripped = (message_text or "").strip()
    if not stripped.lower().startswith(_RESUME_PREFIX):
        return None
    body = stripped[len(_RESUME_PREFIX):].lstrip(" :\n\t.—-")
    return body or None


async def match_resume(resume_text: str) -> tuple[dict, str]:
    resume_text = resume_text.strip()[:_MAX_RESUME_CHARS]
    try:
        candidates = await _search_candidates(resume_text)
        if not candidates:
            msg = (
                "Под это резюме подходящих открытых вакансий не нашёл. "
                "Возможно, их ещё не заводили в базу."
            )
            return {"text": msg}, msg
        ranking = await rank_matches(resume_text, candidates)
        result = _format_result(ranking, candidates)
        return {"text": result}, result
    except Exception as exc:
        logger.error("resume_match_failed", error=str(exc))
        msg = "Извини, не получилось обработать резюме. Попробуй ещё раз."
        return {"text": msg}, msg


async def _search_candidates(resume_text: str) -> list[dict]:
    resp = await _openai.embeddings.create(
        model=settings.openai_embedding_model, input=resume_text
    )
    vec_str = "[" + ",".join(str(x) for x in resp.data[0].embedding) + "]"
    sql = text(
        """
        SELECT id::text, title, status, salary_min, salary_max, currency,
               team, owner_name, description
        FROM vacancies
        WHERE status NOT IN ('closed', 'filled')
          AND embedding IS NOT NULL
          AND is_deleted = false
        ORDER BY embedding <=> CAST(:vec AS vector(1536))
        LIMIT :k
        """
    )
    async with AsyncSessionLocal() as session:
        await session.execute(text("SET ivfflat.probes = 50"))
        rows = (await session.execute(sql, {"vec": vec_str, "k": _TOP_K})).fetchall()
    return [dict(r._mapping) for r in rows]


def _format_meta(c: dict) -> str:
    parts = []
    lo, hi = c.get("salary_min"), c.get("salary_max")
    if lo or hi:
        cur = c.get("currency") or "RUB"
        parts.append(f"{lo or '?'}–{hi or '?'} {cur}")
    if c.get("team"):
        parts.append(f"команда {c['team']}")
    return ", ".join(parts)


def _format_result(ranking: MatchRanking, candidates: list[dict]) -> str:
    by_id = {c["id"]: c for c in candidates}
    matches = [m for m in ranking.matches if m.vacancy_id in by_id]
    if not matches:
        return "Просмотрел открытые вакансии, но под это резюме ничего толком не подходит."

    lines = ["Подобрал вакансии под твоё резюме:\n"]
    for i, m in enumerate(matches, 1):
        c = by_id[m.vacancy_id]
        lines.append(f"{i}. {c.get('title') or '?'} — {m.score}% совпадение")
        meta = _format_meta(c)
        if meta:
            lines.append(f"   {meta}")
        lines.append(f"   Почему: {m.reason}")
    return "\n".join(lines)
