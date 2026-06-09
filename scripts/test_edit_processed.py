"""Тест edit/delete УЖЕ обработанных сообщений (которые стали вакансиями).

Запуск:
    python -m scripts.test_edit_processed
"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine
from app.logger import setup_logging
from app.schemas.incoming import IncomingMessage
from app.services.batch_processor import flush_batch
from app.services.edits import handle_delete, handle_edit_batch
from app.services.ingest import persist_message

SPACE = "spaces/AAQAmmGOtCo"


async def _msg_state(mid: str):
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(
                text(
                    "SELECT is_processed, is_deleted "
                    "FROM chat_messages WHERE message_id = :m"
                ),
                {"m": mid},
            )
        ).first()


async def _vac(title_like: str):
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(
                text(
                    "SELECT title, status, salary_min, salary_max, is_deleted "
                    "FROM vacancies WHERE title ILIKE :t "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"t": f"%{title_like}%"},
            )
        ).first()


def _msg(mid: str, txt: str) -> IncomingMessage:
    return IncomingMessage(
        message_id=mid,
        space_id=SPACE,
        thread_id=None,
        author_id="users/seed-author",
        author_name="Seed Author",
        text=txt,
        created_at=datetime.now(timezone.utc),
    )


async def _run() -> None:
    setup_logging()

    # подготовка: создаём 2 вакансии и обрабатываем пачкой
    await persist_message(_msg("seed-proc-1", "Открыли вакансию QA-инженера, 180k"))
    await persist_message(_msg("seed-proc-2", "Открыли вакансию Kotlin-разработчика, 290k"))
    await flush_batch(SPACE)
    print("подготовка:")
    print("  QA:    ", await _vac("QA"))
    print("  Kotlin:", await _vac("Kotlin"), "\n")

    # 1. EDIT обработанного → полный handle_edit
    await handle_edit_batch(_msg("seed-proc-1", "Открыли вакансию QA-инженера, 220k"))
    print("EDIT обработанного (QA 180k -> 220k):")
    print("  QA:", await _vac("QA"))
    print("  ждём: salary_max=220000\n")

    # 2. DELETE обработанного → soft-delete сообщения + вакансии
    await handle_delete(_msg("seed-proc-2", ""))
    print("DELETE обработанного (Kotlin):")
    print("  msg:   ", await _msg_state("seed-proc-2"))
    print("  Kotlin:", await _vac("Kotlin"))
    print("  ждём: msg is_deleted=True, Kotlin is_deleted=True")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
