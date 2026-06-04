"""Обработка редактирования и удаления сообщений (этап 8).

- handle_edit: правит текст, помечает is_edited, пересчитывает embedding и state.
- handle_delete: мягко удаляет сообщение (is_deleted) и — если оно было единственным
  источником вакансии — мягко удаляет и вакансию.
"""

import json
import uuid
from datetime import datetime, timezone

from openai import AsyncOpenAI
from sqlalchemy import delete, select, text

from app.config import settings
from app.db.models import ChatMessage, ChatMessageEmbedding, Vacancy, VacancyRevision
from app.db.session import AsyncSessionLocal
from app.logger import logger
from app.schemas.incoming import IncomingMessage
from app.services.extraction import run_extraction

_openai = AsyncOpenAI(api_key=settings.openai_api_key)

# Поля вакансии, которые умеем откатывать при удалении источника. Это имена
# колонок — ровно так resolver пишет их в old_value/new_value ревизий.
_REVERTABLE_FIELDS = {
    "title",
    "salary_min",
    "salary_max",
    "currency",
    "owner_name",
    "team",
    "description",
    "status",
}


async def handle_edit(msg: IncomingMessage) -> None:
    """MESSAGE_UPDATED: обновить text + is_edited, пересчитать embedding и extraction."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ChatMessage).where(ChatMessage.message_id == msg.message_id)
            )
        ).scalar_one_or_none()
        if row is None:
            # Сообщение мы не сохраняли (например, было не про вакансию) — нечего править.
            logger.info("edit_unknown_message", message_id=msg.message_id)
            return
        row.text = msg.text
        row.is_edited = True
        msg_uuid = row.id
        await session.commit()

    # Пересчёт embedding: старый вектор больше не соответствует тексту.
    response = await _openai.embeddings.create(
        model=settings.openai_embedding_model, input=msg.text
    )
    vector = response.data[0].embedding
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(ChatMessageEmbedding).where(
                ChatMessageEmbedding.message_id == msg_uuid
            )
        )
        session.add(
            ChatMessageEmbedding(
                message_id=msg_uuid,
                embedding=vector,
                model=settings.openai_embedding_model,
            )
        )
        await session.commit()

    # Пересчёт state: новый текст может менять вакансию. Дубль-ревизии отсекает
    # unique(source_message_id, action) + on_conflict_do_nothing в resolver.
    await run_extraction(msg)
    logger.info("edit_done", message_id=msg.message_id)


async def handle_delete(msg: IncomingMessage) -> None:
    """MESSAGE_DELETED: soft-delete сообщения + soft-delete вакансии, если источник единственный."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(ChatMessage).where(ChatMessage.message_id == msg.message_id)
            )
        ).scalar_one_or_none()
        if row is None:
            logger.info("delete_unknown_message", message_id=msg.message_id)
            return
        row.is_deleted = True
        msg_uuid = row.id
        await session.commit()

    await _soft_delete_orphan_vacancies(msg_uuid, msg.message_id)
    logger.info("delete_done", message_id=msg.message_id)


async def _soft_delete_orphan_vacancies(
    msg_uuid: uuid.UUID, message_id: str
) -> None:
    """Мягко удалить вакансии, чьим единственным живым источником было это сообщение."""
    async with AsyncSessionLocal() as session:
        vacancy_ids = (
            await session.execute(
                text(
                    "SELECT DISTINCT vacancy_id FROM vacancy_revisions "
                    "WHERE source_message_id = :mid"
                ),
                {"mid": msg_uuid},
            )
        ).scalars().all()

        for vid in vacancy_ids:
            # Ревизии от ещё живых (не удалённых) сообщений. Текущее сообщение уже
            # помечено is_deleted, поэтому в счёт «живых» оно не попадёт.
            live_sources = (
                await session.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM vacancy_revisions vr
                        JOIN chat_messages cm ON cm.id = vr.source_message_id
                        WHERE vr.vacancy_id = :vid AND cm.is_deleted = false
                        """
                    ),
                    {"vid": vid},
                )
            ).scalar_one()

            if live_sources == 0:
                await _mark_vacancy_deleted(session, vid)
                logger.info(
                    "vacancy_soft_deleted",
                    vacancy_id=str(vid),
                    source_message_id=message_id,
                )
            elif await _message_created_vacancy(session, vid, msg_uuid):
                # Удалено сообщение, создавшее вакансию → у записи больше нет
                # валидного «родителя». Удаляем вакансию целиком, даже если
                # остались живые правки (их источник — лишь уточнения).
                await _mark_vacancy_deleted(session, vid)
                logger.info(
                    "vacancy_create_source_deleted",
                    vacancy_id=str(vid),
                    source_message_id=message_id,
                )
            else:
                # Вакансия выживает (есть другие источники), но правки удалённого
                # сообщения нужно откатить — иначе вакансия покажет данные из
                # сообщения, которого уже нет.
                await _revert_message_deltas(session, vid, msg_uuid)
                logger.info(
                    "vacancy_source_deleted_kept",
                    vacancy_id=str(vid),
                    live_sources=live_sources,
                )
        await session.commit()


async def _mark_vacancy_deleted(session, vacancy_id) -> None:
    await session.execute(
        text("UPDATE vacancies SET is_deleted = true WHERE id = :vid"),
        {"vid": vacancy_id},
    )


async def _message_created_vacancy(session, vacancy_id, msg_uuid) -> bool:
    """Была ли среди ревизий этого сообщения create-ревизия для вакансии?"""
    found = (
        await session.execute(
            text(
                "SELECT 1 FROM vacancy_revisions "
                "WHERE vacancy_id = :vid AND source_message_id = :mid "
                "AND action = 'create' LIMIT 1"
            ),
            {"vid": vacancy_id, "mid": msg_uuid},
        )
    ).first()
    return found is not None


async def _fields_changed_after(session, vacancy_id, after: datetime) -> set[str]:
    """Имена полей, изменённых живыми ревизиями вакансии позже момента `after`."""
    rows = (
        await session.execute(
            text(
                """
                SELECT vr.new_value
                FROM vacancy_revisions vr
                JOIN chat_messages cm ON cm.id = vr.source_message_id
                WHERE vr.vacancy_id = :vid
                  AND cm.is_deleted = false
                  AND vr.created_at > :after
                  AND vr.action IN ('update', 'close')
                  AND vr.new_value IS NOT NULL
                """
            ),
            {"vid": vacancy_id, "after": after},
        )
    ).scalars().all()

    fields: set[str] = set()
    for new_value in rows:
        try:
            fields.update(json.loads(new_value).keys())
        except (TypeError, ValueError):
            continue
    return fields


async def _revert_message_deltas(
    session, vacancy_id: uuid.UUID, msg_uuid: uuid.UUID
) -> None:
    """Откатить поля, которые менял удалённый источник, к их old_value.

    Поле откатывается только если более поздняя живая ревизия не переписала его
    (иначе победит более свежее значение).
    """
    deleted_revs = (
        await session.execute(
            select(VacancyRevision)
            .where(
                VacancyRevision.vacancy_id == vacancy_id,
                VacancyRevision.source_message_id == msg_uuid,
                VacancyRevision.action.in_(("update", "close")),
                VacancyRevision.old_value.isnot(None),
            )
            .order_by(VacancyRevision.created_at)
        )
    ).scalars().all()
    if not deleted_revs:
        return

    vac = (
        await session.execute(select(Vacancy).where(Vacancy.id == vacancy_id))
    ).scalar_one()

    reverted: list[str] = []
    for rev in deleted_revs:
        superseded = await _fields_changed_after(session, vacancy_id, rev.created_at)
        old_vals = json.loads(rev.old_value)
        new_vals = json.loads(rev.new_value) if rev.new_value else {}
        for field in new_vals:  # поля, которые менял именно этот источник
            if field in superseded or field not in _REVERTABLE_FIELDS:
                continue
            setattr(vac, field, old_vals.get(field))
            reverted.append(field)

    if reverted:
        vac.updated_at = datetime.now(timezone.utc)
        logger.info("vacancy_reverted", vacancy_id=str(vacancy_id), fields=reverted)
