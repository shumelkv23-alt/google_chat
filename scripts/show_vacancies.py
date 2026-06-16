

import asyncio

from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine


async def _run() -> None:
    async with AsyncSessionLocal() as s:
        vacs = (
            await s.execute(
                text(
                    "SELECT id, title, status, salary_min, salary_max, team, is_deleted "
                    "FROM vacancies ORDER BY created_at DESC LIMIT 30"
                )
            )
        ).fetchall()

        for v in vacs:
            flag = " (УДАЛЕНА)" if v.is_deleted else ""
            print(
                f"\n[{v.title}] статус={v.status} "
                f"зп={v.salary_min}-{v.salary_max} команда={v.team}{flag}"
            )
            revs = (
                await s.execute(
                    text(
                        "SELECT action, changed_field, new_value, created_at "
                        "FROM vacancy_revisions WHERE vacancy_id = :vid "
                        "ORDER BY created_at"
                    ),
                    {"vid": v.id},
                )
            ).fetchall()
            for r in revs:
                print(f"    {r.action:8} | поля: {r.changed_field} | {r.new_value}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
