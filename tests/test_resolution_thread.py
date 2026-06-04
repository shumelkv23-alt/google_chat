"""Thread-match: reply в тред вакансии привязывается к ней, а не к последней
созданной / самой похожей по зарплате (баг B2).

Живая БД (chatbot-pg). Сети нет: при thread-match resolve_and_save не считает
эмбеддинг и не зовёт LLM-резолвер. Строки помечены 'res-thr-' / 'RESTEST-'.
"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.db.models import ChatMessage, Vacancy, VacancyRevision
from app.db.session import AsyncSessionLocal, engine
from app.llm.extractor import ExtractionResult
from app.schemas.incoming import IncomingMessage
from app.services.entity_resolution import _find_thread_vacancy, resolve_and_save

_THREAD = "spaces/RES/threads/dataeng"


def _incoming(
    message_id: str,
    thread_id: str | None,
    text_val: str,
    quoted_message_id: str | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        space_id="spaces/RES",
        thread_id=thread_id,
        author_id="users/res",
        author_name="Res",
        text=text_val,
        created_at=datetime.now(timezone.utc),
        event_type="created",
        quoted_message_id=quoted_message_id,
    )


async def _add_message(session, message_id: str, thread_id: str | None, text_val: str):
    m = ChatMessage(
        message_id=message_id,
        space_id="spaces/RES",
        thread_id=thread_id,
        author_id="users/res",
        author_name="Res",
        text=text_val,
        created_at=datetime.now(timezone.utc),
        source="chat_a",
    )
    session.add(m)
    await session.flush()
    return m.id


async def _add_vacancy(session, title: str, salary_min: int | None = None):
    v = Vacancy(title=title, status="open", salary_min=salary_min)
    session.add(v)
    await session.flush()
    return v.id


async def _cleanup() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "DELETE FROM vacancy_revisions WHERE vacancy_id IN "
                "(SELECT id FROM vacancies WHERE title LIKE 'RESTEST-%')"
            )
        )
        await session.execute(text("DELETE FROM vacancies WHERE title LIKE 'RESTEST-%'"))
        await session.execute(text("DELETE FROM chat_messages WHERE message_id LIKE 'res-thr-%'"))
        await session.commit()


def test_reply_in_thread_updates_thread_vacancy_not_latest() -> None:
    """Reply про зп в треде дата-инженера апдейтит ЕГО, хотя extractor ошибочно
    выдал entity_ref='C++' и C++ — последняя созданная вакансия."""
    async def _run() -> None:
        await _cleanup()
        async with AsyncSessionLocal() as session:
            # Тред дата-инженера: create-источник A.
            ua = await _add_message(session, "res-thr-a", _THREAD, "ищем дата инженера")
            vid_data = await _add_vacancy(session, "RESTEST-dataeng")
            session.add(
                VacancyRevision(
                    vacancy_id=vid_data, action="create", source_message_id=ua, confidence=0.9
                )
            )
            # C++ заведена позже (последняя созданная), в другом треде.
            ub = await _add_message(session, "res-thr-b", "spaces/RES/threads/cpp", "ищем C++")
            vid_cpp = await _add_vacancy(session, "RESTEST-cpp")
            session.add(
                VacancyRevision(
                    vacancy_id=vid_cpp, action="create", source_message_id=ub, confidence=0.9
                )
            )
            # Reply в тред дата-инженера: только зарплата, без названия.
            await _add_message(session, "res-thr-c", _THREAD, "зп 2000")
            await session.commit()

        # Extractor ошибся: привязал к C++. Thread-match должен это перебить.
        result = ExtractionResult(
            action="update",
            entity_ref="разработчик на ЯП С++",
            fields={"salary_min": 2000},
            confidence=0.9,
        )
        await resolve_and_save(_incoming("res-thr-c", _THREAD, "зп 2000"), result)

        async with AsyncSessionLocal() as session:
            data = (
                await session.execute(select(Vacancy).where(Vacancy.id == vid_data))
            ).scalar_one()
            cpp = (
                await session.execute(select(Vacancy).where(Vacancy.id == vid_cpp))
            ).scalar_one()
            assert data.salary_min == 2000  # апдейт лёг на дата-инженера
            assert cpp.salary_min is None  # C++ не тронут

            msg_c = (
                await session.execute(
                    select(ChatMessage.id).where(ChatMessage.message_id == "res-thr-c")
                )
            ).scalar_one()
            rev = (
                await session.execute(
                    select(VacancyRevision).where(
                        VacancyRevision.source_message_id == msg_c
                    )
                )
            ).scalar_one()
            assert rev.vacancy_id == vid_data  # ревизия привязана к дата-инженеру

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())


def test_quoted_reply_updates_quoted_vacancy_not_similar() -> None:
    """Quoted reply «поднимаем до 2000» на сообщение «зп 1500» (привязано к МЛ
    Инженеру) апдейтит МЛ Инженера, хотя extractor ошибся на Ruby (та же зп 2000),
    а сообщение сидит в своём изолированном треде (thread_id == message_id)."""
    async def _run() -> None:
        await _cleanup()
        async with AsyncSessionLocal() as session:
            # МЛ Инженер + сообщение-источник «зп 1500» (изолированный тред).
            sal = await _add_message(
                session, "res-thr-sal", "res-thr-sal", "зп 1500 долларов"
            )
            vid_ml = await _add_vacancy(session, "RESTEST-mleng", salary_min=1500)
            session.add(
                VacancyRevision(
                    vacancy_id=vid_ml, action="update", source_message_id=sal, confidence=0.9
                )
            )
            # Ruby — похожа по зарплате, не должна получить апдейт (отличимое 1234).
            rb = await _add_message(session, "res-thr-rb", "res-thr-rb", "ищем руби")
            vid_rb = await _add_vacancy(session, "RESTEST-ruby", salary_min=1234)
            session.add(
                VacancyRevision(
                    vacancy_id=vid_rb, action="create", source_message_id=rb, confidence=0.9
                )
            )
            # Сам quoted reply — в своём треде, цитирует «зп 1500».
            await _add_message(session, "res-thr-q", "res-thr-q", "поднимаем до 2000 долларов")
            await session.commit()

        # Extractor ошибся на Ruby; quoted-match должен перебить.
        result = ExtractionResult(
            action="update",
            entity_ref="разработчик на ЯП руби",
            fields={"salary_min": 2000, "salary_max": 2000},
            confidence=0.6,
        )
        await resolve_and_save(
            _incoming("res-thr-q", "res-thr-q", "поднимаем до 2000 долларов",
                      quoted_message_id="res-thr-sal"),
            result,
        )

        async with AsyncSessionLocal() as session:
            ml = (
                await session.execute(select(Vacancy).where(Vacancy.id == vid_ml))
            ).scalar_one()
            rb_vac = (
                await session.execute(select(Vacancy).where(Vacancy.id == vid_rb))
            ).scalar_one()
            assert ml.salary_min == 2000  # апдейт лёг на МЛ Инженера (цитата)
            assert rb_vac.salary_min == 1234  # Ruby не тронут

            msg_q = (
                await session.execute(
                    select(ChatMessage.id).where(ChatMessage.message_id == "res-thr-q")
                )
            ).scalar_one()
            rev = (
                await session.execute(
                    select(VacancyRevision).where(
                        VacancyRevision.source_message_id == msg_q
                    )
                )
            ).scalar_one()
            assert rev.vacancy_id == vid_ml  # ревизия привязана к МЛ Инженеру

        await _cleanup()
        await engine.dispose()

    asyncio.run(_run())


def test_find_thread_vacancy_none_for_unknown_thread() -> None:
    """Тред без вакансии → None (упадём в обычный путь по эмбеддингу)."""
    async def _run() -> None:
        await _cleanup()
        result = await _find_thread_vacancy("spaces/RES/threads/nonexistent")
        assert result is None
        await engine.dispose()

    asyncio.run(_run())
