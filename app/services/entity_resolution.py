"""Entity resolution: финальное решение create/update/close на основе LLM + БД.

Контракт:
- Extractor выдаёт action как hint (видит только тред).
- Здесь решаем финально: совпадает ли entity_ref с существующей open-вакансией.
- Решение основано на эмбеддинг-поиске + LLM-резолвере.
"""

import json
import uuid
from datetime import datetime, timezone

from openai import AsyncOpenAI
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db.models import ChatMessage, Vacancy, VacancyRevision
from app.db.session import AsyncSessionLocal
from app.llm.extractor import ExtractionResult
from app.llm.resolver import ResolutionResult, resolve_entity
from app.logger import logger
from app.schemas.incoming import IncomingMessage

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

_SIMILARITY_THRESHOLD = 0.75
# Если первичный отбор пуст, для update/close пробуем top-5 без threshold.
_FALLBACK_LIMIT = 5

_CONFIDENCE_MIN = 0.6


_FIELD_MAP = {
    "title": "title",
    "salary_min": "salary_min",
    "salary_max": "salary_max",
    "currency": "currency",
    "owner": "owner_name",
    "team": "team",
    "description": "description",
}


async def resolve_and_save(msg: IncomingMessage, result: ExtractionResult) -> None:
    """Главный вход: embed entity_ref → подобрать кандидатов → решить → записать."""
    entity_ref = result.entity_ref or result.fields.get("title", "")
    if result.action == "none" or not entity_ref:
        logger.info(
            "resolution_skipped_no_ref", message_id=msg.message_id, action=result.action
        )
        return

    ref_vector = await _embed(entity_ref)
    msg_uuid = await _get_message_uuid(msg.message_id)

    
    candidates = await _find_candidates(ref_vector, threshold=_SIMILARITY_THRESHOLD)

    
    if not candidates and result.action in ("update", "close"):
        candidates = await _find_candidates(ref_vector, threshold=None)
        if candidates:
            logger.info(
                "resolution_fallback_candidates",
                message_id=msg.message_id,
                n=len(candidates),
            )

    # 3) Нет кандидатов вообще → либо создаём новую, либо логируем промах.
    if not candidates:
        if result.action == "create":
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    await _create_vacancy(session, result, ref_vector, msg_uuid, msg)
            logger.info("resolution_created_new", message_id=msg.message_id)
        else:
            logger.warning(
                "resolution_no_candidates_for_update",
                message_id=msg.message_id,
                action=result.action,
                entity_ref=entity_ref,
            )
        return

    # 4) Есть кандидаты — спрашиваем LLM-резолвер.
    llm: ResolutionResult = await resolve_entity(entity_ref, candidates, msg.text)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            if llm.vacancy_id and llm.confidence >= _CONFIDENCE_MIN:
                # Резолвер уверенно матчит → апдейтим существующую.
                # Если extractor сказал "create", переписываем на "update":
                # это не дубль, а дополнение к уже существующей записи.
                effective_action = "update" if result.action == "create" else result.action
                if effective_action != result.action:
                    logger.info(
                        "resolution_create_to_update",
                        message_id=msg.message_id,
                        matched_vacancy_id=llm.vacancy_id,
                    )
                await _update_vacancy(
                    session,
                    uuid.UUID(llm.vacancy_id),
                    result,
                    effective_action,
                    ref_vector,
                    msg_uuid,
                )
            elif not llm.vacancy_id and result.action == "create":
                # Резолвер уверенно сказал "это новая позиция" → создаём.
                await _create_vacancy(session, result, ref_vector, msg_uuid, msg)
            elif llm.vacancy_id:
                # Матч есть, но уверенности мало → пишем pending для разбора.
                await _add_revision(
                    session,
                    vacancy_id=uuid.UUID(llm.vacancy_id),
                    action="pending",
                    changed_field=None,
                    old_value=None,
                    new_value=json.dumps(result.fields, ensure_ascii=False),
                    source_message_id=msg_uuid,
                    confidence=llm.confidence,
                )
                logger.info(
                    "resolution_pending_low_confidence",
                    message_id=msg.message_id,
                    confidence=llm.confidence,
                )
            else:
                # Резолвер не нашёл матч, action=update/close → промах.
                logger.warning(
                    "resolution_unmatched_update",
                    message_id=msg.message_id,
                    action=result.action,
                    entity_ref=entity_ref,
                )

    logger.info(
        "resolution_done",
        message_id=msg.message_id,
        action=result.action,
        entity_ref=entity_ref,
    )


async def _add_revision(session, **values) -> None:
    """INSERT ревизии с защитой от дубля по (source_message_id, action).

    Идемпотентность: повторная доставка/обработка того же сообщения не создаёт
    вторую ревизию того же действия (unique partial index из миграции 0002).
    """
    stmt = (
        pg_insert(VacancyRevision)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["source_message_id", "action"],
            index_where=text("source_message_id IS NOT NULL"),
        )
    )
    await session.execute(stmt)


async def _embed(textual: str) -> list[float]:
    resp = await _openai.embeddings.create(
        model=settings.openai_embedding_model, input=textual
    )
    return resp.data[0].embedding


async def _get_message_uuid(message_id: str) -> uuid.UUID | None:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(ChatMessage.id).where(ChatMessage.message_id == message_id)
            )
        ).scalar_one_or_none()


async def _find_candidates(
    vector: list[float], threshold: float | None
) -> list[dict]:
    """Top-5 open-вакансий по cosine. С threshold=None — без отсечки по similarity.
    """
    vec_str = "[" + ",".join(str(x) for x in vector) + "]"
    where_threshold = (
        "AND 1 - (embedding <=> CAST(:vec AS vector(1536))) > :threshold"
        if threshold is not None
        else ""
    )
    sql = text(
        f"""
        SELECT id::text, title, description, status, team,
               salary_min, salary_max, currency, owner_name
        FROM vacancies
        WHERE status != 'closed'
          AND embedding IS NOT NULL
          AND is_deleted = false
          {where_threshold}
        ORDER BY embedding <=> CAST(:vec AS vector(1536))
        LIMIT :limit
        """
    )
    params: dict = {"vec": vec_str, "limit": _FALLBACK_LIMIT}
    if threshold is not None:
        params["threshold"] = threshold
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).fetchall()
    return [dict(r._mapping) for r in rows]


async def _create_vacancy(
    session,
    result: ExtractionResult,
    embedding: list[float],
    msg_uuid: uuid.UUID | None,
    msg: IncomingMessage,
) -> None:
    fields = result.fields
    vac = Vacancy(
        title=fields.get("title") or result.entity_ref,
        status=fields.get("status", "open"),
        salary_min=fields.get("salary_min"),
        salary_max=fields.get("salary_max"),
        currency=fields.get("currency", "RUB"),
        owner_id=msg.author_id,
        owner_name=fields.get("owner") or msg.author_name,
        team=fields.get("team"),
        description=fields.get("description"),
        last_message_id=msg_uuid,
        embedding=embedding,
        confidence=result.confidence,
    )
    session.add(vac)
    await session.flush()  # нужен vac.id для ревизии

    await _add_revision(
        session,
        vacancy_id=vac.id,
        action="create",
        changed_field=None,
        old_value=None,
        new_value=json.dumps(fields, ensure_ascii=False),
        source_message_id=msg_uuid,
        confidence=result.confidence,
    )


async def _update_vacancy(
    session,
    vacancy_id: uuid.UUID,
    result: ExtractionResult,
    effective_action: str,
    embedding: list[float],
    msg_uuid: uuid.UUID | None,
) -> None:
    vac = (
        await session.execute(select(Vacancy).where(Vacancy.id == vacancy_id))
    ).scalar_one()

    old_vals: dict = {}
    new_vals: dict = {}

    for ext_key, vac_attr in _FIELD_MAP.items():
        if ext_key not in result.fields:
            continue
        new_val = result.fields[ext_key]
        old_val = getattr(vac, vac_attr)
        if old_val != new_val:
            old_vals[vac_attr] = old_val
            new_vals[vac_attr] = new_val
            setattr(vac, vac_attr, new_val)

    if effective_action == "close":
        if vac.status != "closed":
            old_vals["status"] = vac.status
            new_vals["status"] = "closed"
            vac.status = "closed"

    vac.last_message_id = msg_uuid
    vac.confidence = result.confidence
    # Обновляем embedding только если позиция всё ещё активна — иначе теряем индекс для close-ревизии.
    if effective_action != "close":
        vac.embedding = embedding
    vac.updated_at = datetime.now(timezone.utc)

    changed = ", ".join(new_vals.keys()) or None
    await _add_revision(
        session,
        vacancy_id=vacancy_id,
        action=effective_action,
        changed_field=changed,
        old_value=(
            json.dumps(old_vals, ensure_ascii=False, default=str)
            if old_vals
            else None
        ),
        new_value=(
            json.dumps(new_vals, ensure_ascii=False, default=str)
            if new_vals
            else None
        ),
        source_message_id=msg_uuid,
        confidence=result.confidence,
    )
