"""Тест edit/delete в batch-режиме (пункты 10-11).

Запуск:
    python -m scripts.test_edit
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


async def _state(mid: str):
    async with AsyncSessionLocal() as s:
        return (
            await s.execute(
                text(
                    "SELECT text, is_edited, is_processed, is_deleted "
                    "FROM chat_messages WHERE message_id = :m"
                ),
                {"m": mid},
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

    # 1. EDIT необработанного
    await persist_message(_msg("seed-ed-1", "Открыли вакансию аналитика, 200k"))
    await handle_edit_batch(_msg("seed-ed-1", "Открыли вакансию аналитика, 250k"))
    st = await _state("seed-ed-1")
    print(f"EDIT:   text={st.text!r} is_edited={st.is_edited} is_processed={st.is_processed}")
    print("  ждём: текст=250k, is_edited=True, is_processed=False (ждёт пачку)\n")

    # 2. DELETE необработанного
    await persist_message(_msg("seed-del-1", "Открыли вакансию C++ разработчика, 400k"))
    await handle_delete(_msg("seed-del-1", ""))
    st = await _state("seed-del-1")
    print(f"DELETE: is_deleted={st.is_deleted}")
    print("  ждём: is_deleted=True\n")

    # 3. Прогон пачки (напрямую, без проверки триггеров созревания)
    await flush_batch(SPACE)
    st_e = await _state("seed-ed-1")
    st_d = await _state("seed-del-1")
    print(f"после пачки: аналитик is_processed={st_e.is_processed}, C++ is_processed={st_d.is_processed}")
    print("  ждём: аналитик=True (взят со свежим текстом), C++=False (удалён, в пачку не попал)")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
