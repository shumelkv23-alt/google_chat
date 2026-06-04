"""Тесты этапа 8: edit/delete сообщений + soft-delete вакансий + дедуп ревизий.

Живая БД (chatbot-pg) + моки OpenAI/LLM. Все строки помечены маркерами
('edel-' / 'EDELTEST-') и подчищаются за собой.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, text

from app.db.models import (
    ChatMessage,
    ChatMessageEmbedding,
    Vacancy,
    VacancyRevision,
)
from app.db.session import AsyncSessionLocal, engine
from app.schemas.incoming import IncomingMessage
from app.services import entity_resolution as er
from app.services.analytics import group_count
from app.services.edits import handle_delete, handle_edit

_TEAM = "EDELTEST-TEAM"


def _incoming(message_id: str, event_type: str, text_val: str = "x") -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        space_id="spaces/EDEL",
        thread_id=None,
        author_id="users/edel",
        author_name="Edel",
        text=text_val,
        created_at=datetime.now(timezone.utc),
        event_type=event_type,
    )


async def _add_message(session, message_id: str, text_val: str = "txt", is_deleted: bool = False):
    m = ChatMessage(
        message_id=message_id,
        space_id="spaces/EDEL",
        thread_id=None,
        author_id="users/edel",
        author_name="Edel",
        text=text_val,
        created_at=datetime.now(timezone.utc),
        source="chat_a",
        is_deleted=is_deleted,
    )
    session.add(m)
    await session.flush()
    return m.id


async def _add_vacancy(
    session,
    title: str,
    team: str | None = None,
    is_deleted: bool = False,
    salary_min: int | None = None,
):
    v = Vacancy(
        title=title, status="open", team=team, is_deleted=is_deleted, salary_min=salary_min
    )
    session.add(v)
    await session.flush()
    return v.id


async def _add_revision(
    session,
    vacancy_id,
    action: str,
    source_message_id,
    *,
    old_value: str | None = None,
    new_value: str | None = None,
    created_at: datetime | None = None,
) -> None:
    rev = VacancyRevision(
        vacancy_id=vacancy_id,
        action=action,
        source_message_id=source_message_id,
        confidence=0.9,
        old_value=old_value,
        new_value=new_value,
    )
    if created_at is not None:
        rev.created_at = created_at
    session.add(rev)


async def _cleanup() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "DELETE FROM vacancy_revisions WHERE source_message_id IN "
                "(SELECT id FROM chat_messages WHERE message_id LIKE 'edel-%')"
            )
        )
        await session.execute(
            text(
                "DELETE FROM vacancy_revisions WHERE vacancy_id IN "
                "(SELECT id FROM vacancies WHERE title LIKE 'EDELTEST-%')"
            )
        )
        await session.execute(text("DELETE FROM vacancies WHERE title LIKE 'EDELTEST-%'"))
        await session.execute(text("DELETE FROM chat_messages WHERE message_id LIKE 'edel-%'"))
        await session.commit()


# --- edit ---------------------------------------------------------------------


def test_handle_edit_updates_text_and_embedding() -> None:
    async def _run() -> None:
        await _cleanup()
        mid = "edel-edit-1"
        async with AsyncSessionLocal() as session:
            msg_uuid = await _add_message(session, mid, text_val="старый текст")
            session.add(
                ChatMessageEmbedding(
                    message_id=msg_uuid,
                    embedding=[0.1] * 1536,
                    model="text-embedding-3-small",
                )
            )
            await session.commit()

        new_embed = AsyncMock()
        new_embed.data = [AsyncMock(embedding=[0.2] * 1536)]
        with patch("app.services.edits._openai") as mock_openai, patch(
            "app.services.edits.run_extraction", new=AsyncMock()
        ) as mock_extract:
            mock_openai.embeddings.create = AsyncMock(return_value=new_embed)
            await handle_edit(_incoming(mid, "updated", text_val="новый текст"))

        mock_extract.assert_awaited_once()
        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    select(ChatMessage).where(ChatMessage.message_id == mid)
                )
            ).scalar_one()
            assert row.text == "новый текст"
            assert row.is_edited is True
            emb = (
                await session.execute(
                    select(ChatMessageEmbedding).where(
                        ChatMessageEmbedding.message_id == row.id
                    )
                )
            ).scalar_one()
            assert abs(emb.embedding[0] - 0.2) < 1e-6  # вектор пересчитан

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())


def test_handle_edit_unknown_message_noop() -> None:
    async def _run() -> None:
        await _cleanup()
        with patch("app.services.edits._openai") as mock_openai, patch(
            "app.services.edits.run_extraction", new=AsyncMock()
        ) as mock_extract:
            mock_openai.embeddings.create = AsyncMock()
            await handle_edit(_incoming("edel-nope", "updated", text_val="x"))
        # Неизвестное сообщение → ни embedding, ни extraction не трогаем.
        mock_openai.embeddings.create.assert_not_called()
        mock_extract.assert_not_awaited()
        await engine.dispose()

    asyncio.run(_run())


# --- delete -------------------------------------------------------------------


def test_handle_delete_soft_deletes_single_source_vacancy() -> None:
    async def _run() -> None:
        await _cleanup()
        mid = "edel-del-1"
        async with AsyncSessionLocal() as session:
            msg_uuid = await _add_message(session, mid)
            vid = await _add_vacancy(session, "EDELTEST-single")
            await _add_revision(session, vid, "create", msg_uuid)
            await session.commit()

        await handle_delete(_incoming(mid, "deleted"))

        async with AsyncSessionLocal() as session:
            cm = (
                await session.execute(
                    select(ChatMessage).where(ChatMessage.message_id == mid)
                )
            ).scalar_one()
            assert cm.is_deleted is True
            vac = (
                await session.execute(select(Vacancy).where(Vacancy.id == vid))
            ).scalar_one()
            assert vac.is_deleted is True  # единственный источник удалён → вакансия тоже

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())


def test_handle_delete_keeps_multi_source_vacancy() -> None:
    """Удаляем update-источник при живом create → вакансия остаётся жива."""
    async def _run() -> None:
        await _cleanup()
        mid_a, mid_b = "edel-del-a", "edel-del-b"
        async with AsyncSessionLocal() as session:
            ua = await _add_message(session, mid_a)
            ub = await _add_message(session, mid_b)
            vid = await _add_vacancy(session, "EDELTEST-multi")
            await _add_revision(session, vid, "create", ua)
            await _add_revision(session, vid, "update", ub)
            await session.commit()

        await handle_delete(_incoming(mid_b, "deleted"))  # удаляем update-источник

        async with AsyncSessionLocal() as session:
            cm = (
                await session.execute(
                    select(ChatMessage).where(ChatMessage.message_id == mid_b)
                )
            ).scalar_one()
            assert cm.is_deleted is True
            vac = (
                await session.execute(select(Vacancy).where(Vacancy.id == vid))
            ).scalar_one()
            assert vac.is_deleted is False  # create-источник (A) жив → вакансия жива

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())


def test_handle_delete_create_source_deletes_vacancy() -> None:
    """Удаляем create-источник при живом update → вакансия удаляется целиком."""
    async def _run() -> None:
        await _cleanup()
        mid_a, mid_b = "edel-cre-a", "edel-cre-b"
        async with AsyncSessionLocal() as session:
            ua = await _add_message(session, mid_a)
            ub = await _add_message(session, mid_b)
            vid = await _add_vacancy(session, "EDELTEST-createdel")
            await _add_revision(session, vid, "create", ua)
            await _add_revision(session, vid, "update", ub)
            await session.commit()

        await handle_delete(_incoming(mid_a, "deleted"))  # удаляем create-источник

        async with AsyncSessionLocal() as session:
            vac = (
                await session.execute(select(Vacancy).where(Vacancy.id == vid))
            ).scalar_one()
            assert vac.is_deleted is True  # «родитель» удалён → вакансия тоже

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())


def test_handle_delete_reverts_update_field() -> None:
    """Удаляем сообщение, поднявшее зарплату → поле откатывается к old_value."""
    async def _run() -> None:
        await _cleanup()
        mid_a, mid_b = "edel-rev-a", "edel-rev-b"
        async with AsyncSessionLocal() as session:
            ua = await _add_message(session, mid_a)
            ub = await _add_message(session, mid_b)
            vid = await _add_vacancy(session, "EDELTEST-revert", salary_min=5000)
            await _add_revision(
                session, vid, "create", ua, new_value='{"salary_min": 3000}'
            )
            await _add_revision(
                session,
                vid,
                "update",
                ub,
                old_value='{"salary_min": 3000}',
                new_value='{"salary_min": 5000}',
            )
            await session.commit()

        await handle_delete(_incoming(mid_b, "deleted"))

        async with AsyncSessionLocal() as session:
            vac = (
                await session.execute(select(Vacancy).where(Vacancy.id == vid))
            ).scalar_one()
            assert vac.is_deleted is False  # живой источник A → вакансия жива
            assert vac.salary_min == 3000  # правка B откатилась к old_value

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())


def test_handle_delete_keeps_superseded_field() -> None:
    """Удаляем среднее сообщение; более позднее живое уже перезаписало поле →
    откатывать НЕ нужно, побеждает свежее значение."""
    async def _run() -> None:
        await _cleanup()
        mid_a, mid_b, mid_c = "edel-sup-a", "edel-sup-b", "edel-sup-c"
        t0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        async with AsyncSessionLocal() as session:
            ua = await _add_message(session, mid_a)
            ub = await _add_message(session, mid_b)
            uc = await _add_message(session, mid_c)
            vid = await _add_vacancy(session, "EDELTEST-superseded", salary_min=7000)
            await _add_revision(
                session, vid, "create", ua, new_value='{"salary_min": 3000}', created_at=t0
            )
            await _add_revision(
                session,
                vid,
                "update",
                ub,
                old_value='{"salary_min": 3000}',
                new_value='{"salary_min": 5000}',
                created_at=t0 + timedelta(minutes=1),
            )
            await _add_revision(
                session,
                vid,
                "update",
                uc,
                old_value='{"salary_min": 5000}',
                new_value='{"salary_min": 7000}',
                created_at=t0 + timedelta(minutes=2),
            )
            await session.commit()

        await handle_delete(_incoming(mid_b, "deleted"))  # удаляем среднее (B)

        async with AsyncSessionLocal() as session:
            vac = (
                await session.execute(select(Vacancy).where(Vacancy.id == vid))
            ).scalar_one()
            assert vac.salary_min == 7000  # C новее B → значение C сохраняется

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())


# --- идемпотентность ревизий --------------------------------------------------


def test_revision_dedup_by_source_and_action() -> None:
    async def _run() -> None:
        await _cleanup()
        mid = "edel-dup-1"
        async with AsyncSessionLocal() as session:
            msg_uuid = await _add_message(session, mid)
            vid = await _add_vacancy(session, "EDELTEST-dup")
            await session.commit()

        async with AsyncSessionLocal() as session:
            for _ in range(2):
                await er._add_revision(
                    session,
                    vacancy_id=vid,
                    action="create",
                    changed_field=None,
                    old_value=None,
                    new_value="{}",
                    source_message_id=msg_uuid,
                    confidence=0.9,
                )
            await session.commit()

        async with AsyncSessionLocal() as session:
            cnt = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM vacancy_revisions "
                        "WHERE source_message_id = :mid AND action = 'create'"
                    ),
                    {"mid": msg_uuid},
                )
            ).scalar_one()
            assert cnt == 1  # второй INSERT отсёкся on_conflict_do_nothing

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())


# --- аналитика не видит удалённые вакансии ------------------------------------


def test_analytics_excludes_deleted_vacancy() -> None:
    async def _run() -> None:
        await _cleanup()
        async with AsyncSessionLocal() as session:
            await _add_vacancy(session, "EDELTEST-live", team=_TEAM, is_deleted=False)
            await _add_vacancy(session, "EDELTEST-dead", team=_TEAM, is_deleted=True)
            await session.commit()

        rows = await group_count(group_by="team", status="any")
        counts = {label: cnt for label, cnt in rows}
        assert counts.get(_TEAM) == 1  # удалённая вакансия не посчитана

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())
