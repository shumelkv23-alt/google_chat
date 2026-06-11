"""Тест двух ключевых механизмов batch v2: история [hN] и link_to_index.

Сценарий 1 — follow-up через границу пачек (история [hN], B7):
  пачка 1: «Открыли Kotlin» → processed, вакансия создана;
  пачка 2: «по Kotlin подняли до 500k» — видит Kotlin в истории/открытых,
  обновляет ту же вакансию, НЕ создаёт дубль.

Сценарий 2 — связь внутри одной пачки (link_to_index, B8):
  одна пачка: [0] «Открыли Elixir» + [1] «туда же нужен Phoenix» (follow-up без
  названия) → обе привязаны к одной вакансии через link_to_index.

Запуск:
    python -m scripts.test_history_link
"""

import asyncio
from datetime import datetime, timezone

from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine
from app.logger import setup_logging
from app.schemas.incoming import IncomingMessage
from app.services.batch_processor import flush_batch
from app.services.ingest import persist_message

SPACE = "spaces/AAQAmmGOtCo"


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


async def _vac(like: str) -> tuple[int, int | None]:
    """(сколько вакансий по маске, salary_max самой свежей)."""
    async with AsyncSessionLocal() as s:
        cnt = (
            await s.execute(
                text(
                    "SELECT count(*) FROM vacancies "
                    "WHERE title ILIKE :p AND is_deleted = false"
                ),
                {"p": like},
            )
        ).scalar()
        sal = (
            await s.execute(
                text(
                    "SELECT salary_max FROM vacancies "
                    "WHERE title ILIKE :p AND is_deleted = false "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"p": like},
            )
        ).scalar()
    return cnt, sal


async def _cleanup() -> None:
    async with AsyncSessionLocal() as s:
        await s.execute(
            text(
                "DELETE FROM vacancy_revisions WHERE vacancy_id IN "
                "(SELECT id FROM vacancies WHERE title ILIKE '%kotlin%' "
                "OR title ILIKE '%elixir%')"
            )
        )
        await s.execute(
            text(
                "DELETE FROM vacancies WHERE title ILIKE '%kotlin%' "
                "OR title ILIKE '%elixir%'"
            )
        )
        await s.execute(
            text(
                "DELETE FROM chat_messages_embeddings WHERE message_id IN "
                "(SELECT id FROM chat_messages WHERE message_id LIKE 'seed-hist-%' "
                "OR message_id LIKE 'seed-link-%')"
            )
        )
        await s.execute(
            text(
                "DELETE FROM chat_messages WHERE message_id LIKE 'seed-hist-%' "
                "OR message_id LIKE 'seed-link-%'"
            )
        )
        await s.commit()


async def _run() -> None:
    setup_logging()
    await _cleanup()

    # --- Сценарий 1: история [hN] через границу пачек ---
    print("=== Сценарий 1: история [hN] ===")
    await persist_message(_msg("seed-hist-1", "Открыли Kotlin-разработчика, до 400k"))
    await flush_batch(SPACE)
    cnt, sal = await _vac("%kotlin%")
    print(f"после пачки 1: Kotlin вакансий={cnt}, salary_max={sal}  (ждём 1, 400000)")

    await persist_message(_msg("seed-hist-2", "по Kotlin подняли до 500k"))
    await flush_batch(SPACE)
    cnt, sal = await _vac("%kotlin%")
    print(
        f"после пачки 2: Kotlin вакансий={cnt}, salary_max={sal}  "
        f"(ждём 1 — НЕ дубль, 500000 — обновилась через историю)\n"
    )

    # --- Сценарий 2: link_to_index внутри одной пачки ---
    print("=== Сценарий 2: link_to_index ===")
    await persist_message(
        _msg("seed-link-1", "Открыли Elixir-разработчика в команду Платежи")
    )
    await persist_message(_msg("seed-link-2", "туда же обязателен опыт с Phoenix"))
    await flush_batch(SPACE)
    cnt, _ = await _vac("%elixir%")
    print(f"после пачки: Elixir вакансий={cnt}  (ждём 1 — [1] привязан к [0])")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
