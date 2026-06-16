import asyncio

from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine
from app.logger import setup_logging
from app.services.batch_processor import flush_due_batches


async def _counts() -> tuple[int, int]:
    async with AsyncSessionLocal() as s:
        unproc = (
            await s.execute(
                text(
                    "SELECT count(*) FROM chat_messages "
                    "WHERE is_processed = false AND is_deleted = false"
                )
            )
        ).scalar()
        vac = (
            await s.execute(
                text("SELECT count(*) FROM vacancies WHERE is_deleted = false")
            )
        ).scalar()
    return unproc, vac


async def _show_vacancies() -> None:
    async with AsyncSessionLocal() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT title, status, salary_min, salary_max, team "
                    "FROM vacancies WHERE is_deleted = false "
                    "ORDER BY created_at DESC LIMIT 20"
                )
            )
        ).fetchall()
    print("\nВакансии (до 20 последних):")
    for r in rows:
        print(f"  - {r.title} [{r.status}] {r.salary_min}-{r.salary_max} | команда: {r.team}")


async def _run() -> None:
    setup_logging()  # чтобы видеть логи batch_due / batch_flushed
    before_unproc, before_vac = await _counts()
    print(f"ДО:    необработанных={before_unproc}, вакансий={before_vac}\n")

    await flush_due_batches()

    after_unproc, after_vac = await _counts()
    print(f"\nПОСЛЕ: необработанных={after_unproc}, вакансий={after_vac}")
    print(
        f"разобрано сообщений: {before_unproc - after_unproc}, "
        f"новых вакансий: {after_vac - before_vac}"
    )
    await _show_vacancies()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
