"""Entity resolution: находит/создаёт вакансию и сохраняет ревизию."""

import json
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from openai import AsyncOpenAI
from sqlalchemy import select, text

from app.config import settings
from app.db.models import Vacancy, VacancyRevision, ChatMessage
from app.db.session import AsyncSessionLocal
from app.llm.extractor import ExtractionResult
from app.llm.resolver import ResolutionResult, resolve_entity
from app.logger import logger
from app.schemas.incoming import IncomingMessage

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

_SIMILARITY_THRESHOLD = 0.75
_CONFIDENCE_MIN = 0.6

# Маппинг ключей extraction → атрибуты Vacancy
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
    """Точка входа: embed entity_ref → найти кандидатов → решить → сохранить."""
    entity_ref = result.entity_ref or result.fields.get("title", "")
    if result.action == "none" or not entity_ref:
        logger.info("resolution_skipped_no_ref", message_id=msg.message_id, action=result.action)
        return

    embed_resp = await _openai.embeddings.create(
        model=settings.openai_embedding_model,
        input=entity_ref,
    )
    ref_vector: list[float] = embed_resp.data[0].embedding

    # UUID строки в БД (может быть None если ingest ещё не завершился)
    msg_uuid = await _get_message_uuid(msg.message_id)

    candidates = await _find_candidates(ref_vector)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            if not candidates:
                if result.action == "create":
                    await _create_vacancy(session, result, ref_vector, msg_uuid, msg)
                else:
                    logger.warning(
                        "resolution_no_candidates",
                        action=result.action,
                        entity_ref=result.entity_ref,
                    )
                return

            llm: ResolutionResult = await resolve_entity(
                entity_ref, candidates, msg.text
            )

            if llm.vacancy_id and llm.confidence >= _CONFIDENCE_MIN:
                await _update_vacancy(
                    session, uuid.UUID(llm.vacancy_id), result, ref_vector, msg_uuid
                )
            elif not llm.vacancy_id and result.action == "create":
                await _create_vacancy(session, result, ref_vector, msg_uuid, msg)
            else:
                # Уверенности недостаточно — пишем pending, вакансию не трогаем
                best_id = uuid.UUID(str(candidates[0]["id"]))
                session.add(
                    VacancyRevision(
                        vacancy_id=best_id,
                        action="pending",
                        changed_field=None,
                        old_value=None,
                        new_value=json.dumps(result.fields, ensure_ascii=False),
                        source_message_id=msg_uuid,
                        confidence=llm.confidence,
                    )
                )

    logger.info(
        "resolution_done",
        message_id=msg.message_id,
        action=result.action,
        entity_ref=result.entity_ref,
    )


async def _get_message_uuid(message_id: str) -> uuid.UUID | None:
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ChatMessage.id).where(ChatMessage.message_id == message_id)
            )
        ).scalar_one_or_none()
    return row


async def _find_candidates(vector: list[float]) -> list[dict]:
    vec_str = "[" + ",".join(str(x) for x in vector) + "]"
    sql = text(f"""
        SELECT id::text, title, description, status, team,
               salary_min, salary_max, currency, owner_name
        FROM vacancies
        WHERE status != 'closed'
          AND embedding IS NOT NULL
          AND 1 - (embedding <=> '{vec_str}'::vector(1536)) > :threshold
        ORDER BY embedding <=> '{vec_str}'::vector(1536)
        LIMIT 5
    """)
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(sql, {"threshold": _SIMILARITY_THRESHOLD})).fetchall()
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
    await session.flush()  # получаем vac.id до INSERT ревизии

    session.add(
        VacancyRevision(
            vacancy_id=vac.id,
            action="create",
            changed_field=None,
            old_value=None,
            new_value=json.dumps(fields, ensure_ascii=False),
            source_message_id=msg_uuid,
            confidence=result.confidence,
        )
    )


async def _update_vacancy(
    session,
    vacancy_id: uuid.UUID,
    result: ExtractionResult,
    embedding: list[float],
    msg_uuid: uuid.UUID | None,
) -> None:
    vac = (
        await session.execute(select(Vacancy).where(Vacancy.id == vacancy_id))
    ).scalar_one()

    old_vals: dict = {}
    new_vals: dict = {}

    for ext_key, vac_attr in _FIELD_MAP.items():
        # status при close — обрабатываем отдельно ниже
        if ext_key == "status" and result.action == "close":
            continue
        if ext_key not in result.fields:
            continue
        new_val = result.fields[ext_key]
        old_val = getattr(vac, vac_attr)
        if old_val != new_val:
            old_vals[vac_attr] = old_val
            new_vals[vac_attr] = new_val
            setattr(vac, vac_attr, new_val)

    if result.action == "close":
        old_vals["status"] = vac.status
        new_vals["status"] = "closed"
        vac.status = "closed"

    vac.last_message_id = msg_uuid
    vac.confidence = result.confidence
    vac.embedding = embedding
    vac.updated_at = datetime.now(timezone.utc)

    changed = ", ".join(new_vals.keys()) or None
    session.add(
        VacancyRevision(
            vacancy_id=vacancy_id,
            action=result.action,
            changed_field=changed,
            old_value=json.dumps(old_vals, ensure_ascii=False, default=str) if old_vals else None,
            new_value=json.dumps(new_vals, ensure_ascii=False, default=str) if new_vals else None,
            source_message_id=msg_uuid,
            confidence=result.confidence,
        )
    )
