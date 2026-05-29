"""RAG: поиск релевантного контекста + сборка промпта для LLM-ответа."""

from datetime import datetime

from openai import AsyncOpenAI
from sqlalchemy import text

from app.config import settings
from app.db.models import Conversation
from app.db.session import AsyncSessionLocal

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

_TOP_K_MESSAGES = 8
_TOP_K_VACANCIES = 3


async def build_rag_context(user_query: str, conversation: Conversation | None) -> str:
    embed_resp = await _openai.embeddings.create(
        model=settings.openai_embedding_model,
        input=user_query,
    )
    vec = embed_resp.data[0].embedding
    vec_str = "[" + ",".join(str(x) for x in vec) + "]"

    messages, vacancies = await _search(vec_str)
    return _assemble_context(conversation, messages, vacancies)


async def _search(vec_str: str) -> tuple[list[dict], list[dict]]:
    msg_sql = text(f"""
        SELECT cm.text, cm.author_name, cm.created_at
        FROM chat_messages_embeddings cme
        JOIN chat_messages cm ON cm.id = cme.message_id
        WHERE cm.is_deleted = false
        ORDER BY cme.embedding <=> '{vec_str}'::vector(1536)
        LIMIT :k
    """)
    vac_sql = text(f"""
        SELECT title, status, salary_min, salary_max, currency,
               owner_name, team, description, updated_at
        FROM vacancies
        WHERE embedding IS NOT NULL AND is_deleted = false
        ORDER BY embedding <=> '{vec_str}'::vector(1536)
        LIMIT :kv
    """)
    async with AsyncSessionLocal() as session:
        msgs = (await session.execute(msg_sql, {"k": _TOP_K_MESSAGES})).fetchall()
        # ivfflat с lists=50 и probes=1 (дефолт) пропускает строки при малом количестве данных.
        # Устанавливаем probes=lists чтобы обойти все кластеры — для десятков вакансий.
        await session.execute(text("SET ivfflat.probes = 50"))
        vacs = (await session.execute(vac_sql, {"kv": _TOP_K_VACANCIES})).fetchall()
    return [dict(r._mapping) for r in msgs], [dict(r._mapping) for r in vacs]


def _assemble_context(
    conversation: Conversation | None,
    messages: list[dict],
    vacancies: list[dict],
) -> str:
    summary = (conversation.running_summary if conversation else None) or "Первый диалог"
    turns_str = _format_turns((conversation.recent_turns or []) if conversation else [])

    return (
        f"=== Память диалога ===\n{summary}\n\nПоследние реплики:\n{turns_str or '—'}\n\n"
        f"=== Актуальное состояние вакансий ===\n{_format_vacancies(vacancies) or 'Нет данных'}\n\n"
        f"=== Релевантные сообщения из пространства ===\n{_format_messages(messages) or 'Нет данных'}"
    )


def _format_turns(turns: list[dict]) -> str:
    lines = []
    for t in turns:
        role = "Пользователь" if t.get("role") == "user" else "Ассистент"
        lines.append(f"{role}: {t.get('text', '')}")
    return "\n".join(lines)


def _format_vacancies(vacancies: list[dict]) -> str:
    lines = []
    for v in vacancies:
        salary = ""
        if v.get("salary_min") or v.get("salary_max"):
            lo = v.get("salary_min", "?")
            hi = v.get("salary_max", "?")
            salary = f", зарплата {lo}–{hi} {v.get('currency', 'RUB')}"
        owner = f", овнер: {v['owner_name']}" if v.get("owner_name") else ""
        team = f", команда: {v['team']}" if v.get("team") else ""
        updated = ""
        if v.get("updated_at"):
            dt: datetime = v["updated_at"]
            updated = f", обновлено: {dt.strftime('%Y-%m-%d')}"
        lines.append(f"- {v['title']} [{v['status']}]{salary}{owner}{team}{updated}")
    return "\n".join(lines)


def _format_messages(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        author = m.get("author_name") or "неизвестный"
        ts = ""
        if m.get("created_at"):
            dt: datetime = m["created_at"]
            ts = f" ({dt.strftime('%Y-%m-%d %H:%M')})"
        lines.append(f"[{author}{ts}]: {m.get('text', '')}")
    return "\n".join(lines)
