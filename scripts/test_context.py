"""Тест видимости контекста: follow-up к УЖЕ обработанной пачке.

Берёт существующие открытые вакансии (напр. из нагрузочного прогона) и шлёт
к ним новые follow-up разными каналами привязки, проверяя, виден ли контекст
через границу пачек:

  1. reply в тред открытой вакансии (без названия)  → find_anchor (тред), онлайн
  2. цитата на обработанное объявление              → find_anchor (цитата), онлайн
  3. упоминание названия без треда/цитаты            → пачка: история [hN] / open [vN]
  4. голый follow-up без названия/структуры          → граничный (контекст/последняя)

Для каждого follow-up печатает, к какой вакансии он привязался (по новой
ревизии), создал ли дубль или промахнулся.

Опоры ищутся в БД динамически — скрипт переживает пересев данных.

Запуск:
    python -m scripts.test_context
"""

import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine
from app.logger import setup_logging
from app.schemas.incoming import IncomingMessage
from app.services.batch_processor import flush_batch, route_created_batch
from app.services.ingest import persist_message


def _msg(mid, txt, space, thread=None, quoted=None) -> IncomingMessage:
    return IncomingMessage(
        message_id=mid,
        space_id=space,
        thread_id=thread,
        author_id="users/ctx-author",
        author_name="Context Tester",
        text=txt,
        created_at=datetime.now(timezone.utc),
        quoted_message_id=quoted,
    )


async def _find_anchors() -> dict | None:
    """Найти опоры: открытую вакансию с тредом и две открытые без треда."""
    async with AsyncSessionLocal() as s:
        with_thread = (
            await s.execute(
                text(
                    """
                    SELECT v.id::text vid, v.title, m.space_id, m.thread_id
                    FROM vacancies v
                    JOIN vacancy_revisions r ON r.vacancy_id = v.id
                    JOIN chat_messages m ON m.id = r.source_message_id
                    WHERE v.status='open' AND v.is_deleted=false
                      AND m.thread_id IS NOT NULL
                    ORDER BY m.created_at DESC LIMIT 1
                    """
                )
            )
        ).first()
        no_thread = (
            await s.execute(
                text(
                    """
                    SELECT v.id::text vid, v.title, m.space_id, m.message_id
                    FROM vacancies v
                    JOIN vacancy_revisions r ON r.vacancy_id = v.id
                    JOIN chat_messages m ON m.id = r.source_message_id
                    WHERE v.status='open' AND v.is_deleted=false
                      AND m.thread_id IS NULL AND r.action='create'
                    ORDER BY m.created_at DESC LIMIT 2
                    """
                )
            )
        ).fetchall()
    if not with_thread or len(no_thread) < 2:
        return None
    return {
        "thread": dict(with_thread._mapping),
        "quote": dict(no_thread[0]._mapping),
        "mention": dict(no_thread[1]._mapping),
    }


async def _binding(follow_mid: str) -> str:
    """К какой вакансии привязался follow-up (по его ревизии)."""
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    """
                    SELECT v.title, v.status, r.action
                    FROM vacancy_revisions r
                    JOIN vacancies v ON v.id = r.vacancy_id
                    JOIN chat_messages m ON m.id = r.source_message_id
                    WHERE m.message_id = :mid
                    ORDER BY r.created_at DESC LIMIT 1
                    """
                ),
                {"mid": follow_mid},
            )
        ).first()
    if row is None:
        return "— нет ревизии (не привязался ни к чему)"
    return f"{row.action} → «{row.title}» [{row.status}]"


async def _run() -> None:
    setup_logging()
    anchors = await _find_anchors()
    if anchors is None:
        print("Нет подходящих открытых вакансий — сначала прогони scripts.load_test")
        await engine.dispose()
        return

    th = anchors["thread"]
    q = anchors["quote"]
    mention = anchors["mention"]
    space = th["space_id"]
    sfx = uuid.uuid4().hex[:6]  # уникальный суффикс, чтобы не конфликтовать с прошлым прогоном

    print("Опоры (открытые вакансии из обработанной пачки):")
    print(f"  тред     : «{th['title']}» (thread={th['thread_id']})")
    print(f"  цитата   : «{q['title']}» (msg={q['message_id']})")
    print(f"  упоминан.: «{mention['title']}»\n")

    # Собираем follow-up'ы. mid уникален (суффикс), чтобы повторный запуск не упирался в дубли.
    fu = {
        "thread": _msg(
            f"{space}/messages/ctx-th-{sfx}",
            "коллеги, по этой позиции ещё нужен опыт код-ревью",
            space,
            thread=th["thread_id"],
        ),
        "quote": _msg(
            f"{space}/messages/ctx-q-{sfx}",
            "по ней вилку подняли до 999k",
            space,
            quoted=q["message_id"],
        ),
        "mention": _msg(
            f"{space}/messages/ctx-m-{sfx}",
            f"по позиции «{mention['title']}» теперь полная удалёнка",
            space,
        ),
        "bare": _msg(
            f"{space}/messages/ctx-bare-{sfx}",
            "а ещё добавили годовой бонус по ней",
            space,
        ),
    }

    # Реальный поток created: persist → route (онлайн при структурной привязке),
    # затем flush добивает то, что осталось ждать пачку (упоминание/голый).
    for m in fu.values():
        await persist_message(m)
        await route_created_batch(m)
    await flush_batch(space)

    print("Куда привязались follow-up'ы:")
    print(f"  1. reply в тред     : {await _binding(fu['thread'].message_id)}")
    print(f"     ожидали → «{th['title']}»")
    print(f"  2. цитата           : {await _binding(fu['quote'].message_id)}")
    print(f"     ожидали → «{q['title']}»")
    print(f"  3. упоминание (hN/vN): {await _binding(fu['mention'].message_id)}")
    print(f"     ожидали → «{mention['title']}»")
    print(f"  4. голый follow-up  : {await _binding(fu['bare'].message_id)}")
    print("     (граничный — привязка по контексту/последней; промах допустим)")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
