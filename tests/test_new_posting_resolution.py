"""Новый найм на уже открытую роль создаёт ОТДЕЛЬНУЮ вакансию, а не апдейтит.

Сценарий бага: «системный админ / Microsoft» уже открыт; приходит новое
объявление «ищем … системного администратора …». Резолвер уверенно матчит
старую запись, но это другой найм — должна появиться вторая вакансия.

Живая БД (chatbot-pg). Сеть (_embed/_find_candidates/resolve_entity) замокана,
поэтому тест детерминирован. Строки помечены 'RESTEST-' / 'res-np-'.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select, text

from app.db.models import ChatMessage, Vacancy, VacancyRevision
from app.db.session import AsyncSessionLocal, engine
from app.llm.extractor import ExtractionResult
from app.llm.posting_classifier import PostingVerdict
from app.llm.resolver import ResolutionResult
from app.schemas.incoming import IncomingMessage
from app.services import entity_resolution
from app.services.entity_resolution import resolve_and_save

_TITLE = "RESTEST-sysadmin"
_NEW_THREAD = "spaces/RES/threads/np-new"  # свежий тред без вакансии → нет anchor


def _incoming(message_id: str, text_val: str) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        space_id="spaces/RES",
        thread_id=_NEW_THREAD,
        author_id="users/res",
        author_name="Res",
        text=text_val,
        created_at=datetime.now(timezone.utc),
        event_type="created",
    )


async def _add_message(session, message_id: str, text_val: str):
    m = ChatMessage(
        message_id=message_id,
        space_id="spaces/RES",
        thread_id=_NEW_THREAD,
        author_id="users/res",
        author_name="Res",
        text=text_val,
        created_at=datetime.now(timezone.utc),
        source="chat_a",
    )
    session.add(m)
    await session.flush()
    return m.id


async def _cleanup() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "DELETE FROM vacancy_revisions WHERE vacancy_id IN "
                "(SELECT id FROM vacancies WHERE title LIKE 'RESTEST-%')"
            )
        )
        await session.execute(text("DELETE FROM vacancies WHERE title LIKE 'RESTEST-%'"))
        await session.execute(text("DELETE FROM chat_messages WHERE message_id LIKE 'res-np-%'"))
        await session.commit()


def test_new_posting_creates_separate_vacancy_not_update() -> None:
    """Явное объявление о найме на уже открытую роль → вторая вакансия, старая цела."""
    async def _run() -> None:
        await _cleanup()
        async with AsyncSessionLocal() as session:
            # Старый «системный админ» уже открыт.
            existing = Vacancy(title=_TITLE, status="open", team="Microsoft")
            session.add(existing)
            await session.flush()
            existing_id = existing.id
            # Новое объявление (в своём свежем треде, без привязки к вакансии).
            await _add_message(session, "res-np-1", "ищем в команду Microsoft системного администратора")
            await session.commit()

        result = ExtractionResult(
            action="update",  # extractor ошибочно прибил create → update
            entity_ref=_TITLE,
            fields={"team": "Microsoft", "description": "администрирование сетей"},
            confidence=0.9,
        )

        # Сеть мокаем: резолвер уверенно матчит старую запись, классификатор
        # подтверждает, что это новое объявление.
        fake_embed = AsyncMock(return_value=[0.0] * 1536)
        fake_candidates = AsyncMock(
            return_value=[{"id": str(existing_id), "title": _TITLE, "status": "open"}]
        )
        fake_resolve = AsyncMock(
            return_value=ResolutionResult(vacancy_id=str(existing_id), confidence=0.9)
        )
        fake_classify = AsyncMock(
            return_value=PostingVerdict(new_posting=True, confidence=0.9)
        )
        with patch.object(entity_resolution, "_embed", fake_embed), patch.object(
            entity_resolution, "_find_candidates", fake_candidates
        ), patch.object(entity_resolution, "resolve_entity", fake_resolve), patch.object(
            entity_resolution, "classify_new_posting", fake_classify
        ):
            await resolve_and_save(_incoming("res-np-1", "ищем системного администратора"), result)

        async with AsyncSessionLocal() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(Vacancy)
                    .where(Vacancy.title == _TITLE, Vacancy.is_deleted == False)  # noqa: E712
                )
            ).scalar_one()
            assert count == 2  # появилась вторая вакансия — отдельный найм

            old = (
                await session.execute(select(Vacancy).where(Vacancy.id == existing_id))
            ).scalar_one()
            assert old.description is None  # старую не тронули

            msg_id = (
                await session.execute(
                    select(ChatMessage.id).where(ChatMessage.message_id == "res-np-1")
                )
            ).scalar_one()
            rev = (
                await session.execute(
                    select(VacancyRevision).where(VacancyRevision.source_message_id == msg_id)
                )
            ).scalar_one()
            assert rev.action == "create"  # ревизия — создание, не апдейт
            assert rev.vacancy_id != existing_id  # привязана к новой вакансии

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())


def test_new_posting_inherits_team_from_duplicate() -> None:
    """update-режим extractor роняет team → новый найм берёт компанию из дубля."""
    async def _run() -> None:
        await _cleanup()
        async with AsyncSessionLocal() as session:
            existing = Vacancy(title=_TITLE, status="open", team="Apple")
            session.add(existing)
            await session.flush()
            existing_id = existing.id
            await _add_message(session, "res-np-2", "Хотим нанять ещё одного системного администратора")
            await session.commit()

        # extractor пометил update → в fields только условия, без title/team.
        result = ExtractionResult(
            action="update",
            entity_ref=_TITLE,
            fields={"salary_min": 1000, "salary_max": 1000, "currency": "USD"},
            confidence=0.9,
        )

        fake_embed = AsyncMock(return_value=[0.0] * 1536)
        fake_candidates = AsyncMock(
            return_value=[
                {"id": str(existing_id), "title": _TITLE, "status": "open", "team": "Apple"}
            ]
        )
        fake_resolve = AsyncMock(
            return_value=ResolutionResult(vacancy_id=str(existing_id), confidence=0.9)
        )
        fake_classify = AsyncMock(
            return_value=PostingVerdict(new_posting=True, confidence=0.9)
        )
        with patch.object(entity_resolution, "_embed", fake_embed), patch.object(
            entity_resolution, "_find_candidates", fake_candidates
        ), patch.object(entity_resolution, "resolve_entity", fake_resolve), patch.object(
            entity_resolution, "classify_new_posting", fake_classify
        ):
            await resolve_and_save(
                _incoming("res-np-2", "Хотим нанять ещё одного системного администратора"),
                result,
            )

        async with AsyncSessionLocal() as session:
            new_vac = (
                await session.execute(
                    select(Vacancy).where(
                        Vacancy.title == _TITLE,
                        Vacancy.id != existing_id,
                        Vacancy.is_deleted == False,  # noqa: E712
                    )
                )
            ).scalar_one()
            assert new_vac.team == "Apple"     # team унаследован из дубля
            assert new_vac.salary_min == 1000  # условия — из сообщения

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())
