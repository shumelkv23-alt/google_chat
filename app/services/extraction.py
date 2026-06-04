"""Фоновая задача: pre-filter + extraction + entity resolution."""

from openai import AsyncOpenAI
from sqlalchemy import select, text

from app.config import settings
from app.db.models import ChatMessage
from app.db.session import AsyncSessionLocal
from app.llm.extractor import extract_vacancy
from app.llm.prefilter import is_vacancy_message
from app.logger import logger
from app.schemas.incoming import IncomingMessage
from app.services.entity_resolution import resolve_and_save

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

_CONTEXT_LIMIT = 7
_EXTRACTOR_SPACE_FALLBACK = 3
_MERGED_CONTEXT_LIMIT = 10
_OPEN_VACANCIES_K = 3


async def _fetch_recent(*, scope: str, scope_id: str, exclude_msg_id: str) -> list[dict]:
    """Последние N сообщений по scope ('thread'|'space'), кроме текущего, в хронологическом порядке."""
    column = ChatMessage.thread_id if scope == "thread" else ChatMessage.space_id
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    ChatMessage.message_id,
                    ChatMessage.author_name,
                    ChatMessage.text,
                    ChatMessage.created_at,
                )
                .where(column == scope_id, ChatMessage.message_id != exclude_msg_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(_CONTEXT_LIMIT)
            )
        ).fetchall()
    return [
        {
            "message_id": r.message_id,
            "author_name": r.author_name,
            "text": r.text,
            "created_at": r.created_at,
        }
        for r in reversed(rows)
    ]


def _merge_recent(thread_ctx: list[dict], space_ctx: list[dict]) -> list[dict]:
    """Слить тред (точная ветка reply) и space-фон в одно окно по времени.

    Дедуп по message_id (сообщения треда лежат и в space), сортировка по времени,
    обрезка до _MERGED_CONTEXT_LIMIT. Нужно, чтобы extractor видел и узкую ветку
    follow-up'ов, и соседние сообщения из других тредов (напр. объявление вакансии,
    к которому относится безымянная зарплата вроде «теперь 2000»).
    """
    by_id: dict[str, dict] = {}
    for m in (*space_ctx, *thread_ctx):
        by_id[m["message_id"]] = m
    merged = sorted(by_id.values(), key=lambda m: m["created_at"])
    return merged[-_MERGED_CONTEXT_LIMIT:]


async def _build_contexts(msg: IncomingMessage) -> tuple[list[dict], list[dict]]:
    """Возвращает (extractor_context, prefilter_context).

    space_ctx — окно последних сообщений пространства по времени (для prefilter и
    как «фон»). Если сообщение в непустом треде (есть ветка reply) — подмешиваем
    тред к space-фону: extractor видит и точную ветку follow-up'ов, и соседние
    сообщения из других тредов (напр. объявление вакансии, к которому относится
    безымянная зарплата).

    Одиночное сообщение в своём треде (типично для нового объявления в threaded-
    режиме) — узкое окно space, как раньше: своего контекста у него нет.
    """
    space_ctx = await _fetch_recent(
        scope="space", scope_id=msg.space_id, exclude_msg_id=msg.message_id
    )
    if not msg.thread_id:
        return space_ctx[-_EXTRACTOR_SPACE_FALLBACK:], space_ctx

    thread_ctx = await _fetch_recent(
        scope="thread", scope_id=msg.thread_id, exclude_msg_id=msg.message_id
    )
    if not thread_ctx:
        return space_ctx[-_EXTRACTOR_SPACE_FALLBACK:], space_ctx

    return _merge_recent(thread_ctx, space_ctx), space_ctx


async def _get_open_vacancies(query: str) -> list[dict]:
    """Кандидаты-вакансии для extractor'а: top-K по cosine + последняя созданная.

    Top-K по похожести закрывает длинные обсуждения, где упоминание давно вышло
    из окна. Но безымянные follow-up'ы («зп от 2000») по эмбеддингу тянутся к
    похожим зарплатам, а не к той вакансии, которую только что завели — поэтому
    отдельно подкладываем последнюю созданную открытую вакансию и помечаем её
    флагом `_latest` (extractor показывает её как [последняя созданная]).
    """
    resp = await _openai.embeddings.create(
        model=settings.openai_embedding_model, input=query
    )
    vec_str = "[" + ",".join(str(x) for x in resp.data[0].embedding) + "]"
    sql = text(
        """
        SELECT id::text, title, status, team, salary_min, salary_max, currency
        FROM vacancies
        WHERE status != 'closed' AND embedding IS NOT NULL AND is_deleted = false
        ORDER BY embedding <=> CAST(:vec AS vector(1536))
        LIMIT :k
        """
    )
    latest_sql = text(
        """
        SELECT id::text, title, status, team, salary_min, salary_max, currency
        FROM vacancies
        WHERE status != 'closed' AND is_deleted = false
        ORDER BY created_at DESC
        LIMIT 1
        """
    )
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(sql, {"vec": vec_str, "k": _OPEN_VACANCIES_K})
        ).fetchall()
        latest = (await session.execute(latest_sql)).fetchone()

    vacancies = [dict(r._mapping) for r in rows]
    if latest is not None:
        latest_id = latest._mapping["id"]
        existing = next((v for v in vacancies if v["id"] == latest_id), None)
        if existing is not None:
            existing["_latest"] = True
        else:
            vacancies.append({**dict(latest._mapping), "_latest": True})
    return vacancies


async def run_extraction(msg: IncomingMessage) -> None:
    try:
        extractor_ctx, prefilter_ctx = await _build_contexts(msg)

        if not await is_vacancy_message(msg.text):
            if not prefilter_ctx or not await is_vacancy_message(msg.text, prefilter_ctx):
                logger.info("extraction_skipped_not_vacancy", message_id=msg.message_id)
                return

        open_vacancies = await _get_open_vacancies(msg.text)

        result = await extract_vacancy(
            text=msg.text,
            author_name=msg.author_name,
            created_at=msg.created_at.isoformat(),
            context_messages=extractor_ctx or None,
            open_vacancies=open_vacancies or None,
        )

        logger.info(
            "extraction_result",
            message_id=msg.message_id,
            action=result.action,
            entity_ref=result.entity_ref,
            fields=result.fields,
            confidence=result.confidence,
            open_vacancies_count=len(open_vacancies),
        )

        if result.action != "none":
            await resolve_and_save(msg, result)
    except Exception:
        logger.exception("extraction_pipeline_error", message_id=msg.message_id)
