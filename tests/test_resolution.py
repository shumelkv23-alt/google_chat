"""E2E тест entity resolution: create → update → close → 1 вакансия + 3 ревизии.

Запуск (контейнер chatbot-pg должен быть запущен):
    python -m pytest tests/test_resolution.py -v
"""

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, text

from app.db.models import Vacancy, VacancyRevision
from app.db.session import AsyncSessionLocal, engine
from app.llm.extractor import ExtractionResult
from app.llm.resolver import ResolutionResult
from app.schemas.incoming import IncomingMessage
from app.services.entity_resolution import resolve_and_save

_SPACE_ID = "spaces/RESTEST"
_AUTHOR_ID = "users/res-test-author"
_FAKE_VECTOR = [0.1] * 1536


def _msg(n: int, text_: str) -> IncomingMessage:
    return IncomingMessage(
        message_id=f"spaces/RESTEST/messages/res-{n}",
        space_id=_SPACE_ID,
        thread_id=None,
        author_id=_AUTHOR_ID,
        author_name="Test User",
        text=text_,
        created_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


def _mock_openai() -> AsyncMock:
    mock_resp = AsyncMock()
    mock_resp.data = [AsyncMock(embedding=_FAKE_VECTOR)]
    client = AsyncMock()
    client.embeddings.create = AsyncMock(return_value=mock_resp)
    return client


async def _cleanup() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "DELETE FROM vacancy_revisions WHERE vacancy_id IN "
                "(SELECT id FROM vacancies WHERE owner_id = :uid)"
            ),
            {"uid": _AUTHOR_ID},
        )
        await session.execute(
            text("DELETE FROM vacancies WHERE owner_id = :uid"), {"uid": _AUTHOR_ID}
        )
        await session.commit()


async def _run_e2e() -> None:
    await _cleanup()

    # ── Сообщение 1: create ──────────────────────────────────────────────────
    result1 = ExtractionResult(
        action="create",
        entity_ref="Python разработчик",
        fields={"title": "Python разработчик", "salary_max": 300000, "currency": "RUB"},
        confidence=0.92,
    )
    with patch("app.services.entity_resolution._openai", _mock_openai()):
        with patch(
            "app.services.entity_resolution._find_candidates",
            AsyncMock(return_value=[]),
        ):
            await resolve_and_save(_msg(1, "Открыли Python разработчика"), result1)

    async with AsyncSessionLocal() as session:
        vac = (
            await session.execute(
                select(Vacancy).where(Vacancy.owner_id == _AUTHOR_ID)
            )
        ).scalar_one()
        vac_id = vac.id
        assert vac.title == "Python разработчик"
        assert vac.salary_max == 300000
        assert vac.status == "open"

    # ── Сообщение 2: update (salary) ────────────────────────────────────────
    result2 = ExtractionResult(
        action="update",
        entity_ref="питонист",
        fields={"salary_max": 350000},
        confidence=0.88,
    )
    candidate = {
        "id": str(vac_id),
        "title": "Python разработчик",
        "team": None,
        "description": None,
        "status": "open",
        "salary_min": None,
        "salary_max": 300000,
        "currency": "RUB",
        "owner_name": "Test User",
    }
    llm_match = AsyncMock(return_value=ResolutionResult(vacancy_id=str(vac_id), confidence=0.87))
    with patch("app.services.entity_resolution._openai", _mock_openai()):
        with patch(
            "app.services.entity_resolution._find_candidates",
            AsyncMock(return_value=[candidate]),
        ):
            with patch("app.services.entity_resolution.resolve_entity", llm_match):
                await resolve_and_save(_msg(2, "По питонисту подняли до 350k"), result2)

    async with AsyncSessionLocal() as session:
        vac = (await session.execute(select(Vacancy).where(Vacancy.id == vac_id))).scalar_one()
        assert vac.salary_max == 350000

    # ── Сообщение 3: close ──────────────────────────────────────────────────
    result3 = ExtractionResult(
        action="close",
        entity_ref="Python разработчик",
        fields={},
        confidence=0.95,
    )
    llm_close = AsyncMock(return_value=ResolutionResult(vacancy_id=str(vac_id), confidence=0.95))
    with patch("app.services.entity_resolution._openai", _mock_openai()):
        with patch(
            "app.services.entity_resolution._find_candidates",
            AsyncMock(return_value=[candidate]),
        ):
            with patch("app.services.entity_resolution.resolve_entity", llm_close):
                await resolve_and_save(_msg(3, "Закрыли питониста"), result3)

    # ── Финальная проверка ───────────────────────────────────────────────────
    async with AsyncSessionLocal() as session:
        vac = (await session.execute(select(Vacancy).where(Vacancy.id == vac_id))).scalar_one()
        assert vac.status == "closed", f"ожидали closed, получили {vac.status}"

        revs = (
            await session.execute(
                select(VacancyRevision)
                .where(VacancyRevision.vacancy_id == vac_id)
                .order_by(VacancyRevision.created_at)
            )
        ).scalars().all()
        assert len(revs) == 3, f"ожидаем 3 ревизии, получили {len(revs)}"
        assert revs[0].action == "create"
        assert revs[1].action == "update"
        assert revs[2].action == "close"

    await _cleanup()
    await engine.dispose()


async def _run_pending() -> None:
    """Низкая уверенность → revision(pending), вакансия не трогается."""
    await _cleanup()

    # Создаём вакансию напрямую в БД
    async with AsyncSessionLocal() as session:
        vac = Vacancy(
            title="Go разработчик",
            status="open",
            owner_id=_AUTHOR_ID,
            embedding=_FAKE_VECTOR,
            confidence=0.9,
        )
        session.add(vac)
        await session.commit()
        vac_id = vac.id

    result = ExtractionResult(
        action="update",
        entity_ref="го-разраб",
        fields={"salary_max": 400000},
        confidence=0.4,
    )
    candidate = {
        "id": str(vac_id),
        "title": "Go разработчик",
        "team": None,
        "description": None,
        "status": "open",
        "salary_min": None,
        "salary_max": None,
        "currency": "RUB",
        "owner_name": None,
    }
    # LLM возвращает матч, но с низкой уверенностью
    llm_low = AsyncMock(return_value=ResolutionResult(vacancy_id=str(vac_id), confidence=0.35))
    with patch("app.services.entity_resolution._openai", _mock_openai()):
        with patch(
            "app.services.entity_resolution._find_candidates",
            AsyncMock(return_value=[candidate]),
        ):
            with patch("app.services.entity_resolution.resolve_entity", llm_low):
                await resolve_and_save(_msg(10, "Обновили го-разраба?"), result)

    async with AsyncSessionLocal() as session:
        vac = (await session.execute(select(Vacancy).where(Vacancy.id == vac_id))).scalar_one()
        assert vac.salary_max is None, "вакансия не должна была измениться"

        rev = (
            await session.execute(
                select(VacancyRevision).where(VacancyRevision.vacancy_id == vac_id)
            )
        ).scalar_one()
        assert rev.action == "pending"

    await _cleanup()
    await engine.dispose()


def test_resolution_create_update_close() -> None:
    asyncio.run(_run_e2e())


def test_resolution_pending_on_low_confidence() -> None:
    asyncio.run(_run_pending())
