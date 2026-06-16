import argparse
import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import text

from app.db.session import AsyncSessionLocal, engine

SPACE = "spaces/LOADTEST"
AUTHOR = "users/load-author"
AUTHOR_NAME = "Load Tester"

_CE = {
    "created": "google.workspace.chat.message.v1.created",
    "updated": "google.workspace.chat.message.v1.updated",
    "deleted": "google.workspace.chat.message.v1.deleted",
}


_ROLES = [
    ("Python-разработчика", "Платформа", 300),
    ("Go-разработчика", "Биллинг", 280),
    ("Frontend на React", "Веб", 250),
    ("DevOps-инженера", "Инфра", 350),
    ("ML-инженера", "Data", 400),
    ("QA-автоматизатора", "Качество", 220),
    ("системного аналитика", "Аналитика", 240),
    ("UX-дизайнера", "Дизайн", 230),
    ("продакт-менеджера", "Продукт", 320),
    ("Android-разработчика", "Мобайл", 290),
    ("iOS-разработчика", "Мобайл", 290),
    ("Data-инженера", "Data", 330),
    ("SRE", "Инфра", 360),
    ("специалиста по безопасности", "Security", 340),
    ("Java-разработчика", "Бэкенд", 310),
    ("Scala-разработчика", "Финтех", 330),
    ("Rust-разработчика", "Платформа", 340),
    ("Kotlin-разработчика", "Мобайл", 300),
    ("C++ разработчика", "Ядро", 380),
    ("PHP-разработчика", "Веб", 200),
    ("Ruby-разработчика", "Веб", 250),
    ("Node.js разработчика", "Веб", 260),
    ("дата-сайентиста", "Data", 350),
    ("технического писателя", "Док", 180),
    ("HR-менеджера", "HR", 190),
]

_JUNK = [
    "всем привет, как выходные?",
    "обед сегодня в 13:00",
    "кто идёт на кофе?",
    "с пятницей, коллеги",
    "напоминаю про созвон в 15:00",
]


def _event(mid, txt, ts, *, ce="created", thread=None, quoted=None) -> dict:
    """Собрать одно отправляемое событие (нормализованный вид для отправщика)."""
    return {
        "mid": mid,
        "text": txt,
        "ts": ts,
        "ce": ce,
        "thread": thread,
        "quoted": quoted,
    }


def build_events() -> list[dict]:
    """Реалистичная смесь ~100 событий с возрастающим временем и связями."""
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    events: list[dict] = []
    n = 0

    def ts() -> datetime:
        nonlocal n
        n += 1
        return base + timedelta(seconds=n * 10)

    # 1) Независимые объявления (25 ролей) → отдельные вакансии.
    announced: list[str] = []
    for i, (role, team, salary) in enumerate(_ROLES):
        mid = f"{SPACE}/messages/ann-{i}"
        events.append(
            _event(mid, f"Открыли вакансию {role}, до {salary}k, команда {team}", ts())
        )
        announced.append(mid)

    # 2) Follow-up'ы в тредах (12 тредов): объявление + follow-up без названия.
    for i in range(12):
        thread = f"{SPACE}/threads/th-{i}"
        role = _ROLES[i][0]
        base_mid = f"{SPACE}/messages/th-base-{i}"
        events.append(
            _event(base_mid, f"Ищем ещё одного {role}, тред для деталей", ts(), thread=thread)
        )
        events.append(
            _event(
                f"{SPACE}/messages/th-fu-{i}",
                "кстати, теперь удалёнка и есть ДМС",
                ts(),
                thread=thread,
            )
        )

    # 3) Цитаты (8): объявление + quoted reply без названия.
    for i in range(8):
        base_mid = f"{SPACE}/messages/q-base-{i}"
        events.append(
            _event(base_mid, f"Открыли позицию {_ROLES[i][0]} срочно", ts())
        )
        events.append(
            _event(
                f"{SPACE}/messages/q-fu-{i}",
                "поднимаем вилку на 50k",
                ts(),
                quoted=base_mid,
            )
        )

    # 4) Правки (8): updated тех же announcement-сообщений (новый текст).
    for i in range(8):
        events.append(
            _event(
                announced[i],
                f"Поправка по {_ROLES[i][0]}: вилку подняли до {_ROLES[i][2] + 50}k",
                ts(),
                ce="updated",
            )
        )

    # 5) Закрытия (7).
    for i in range(7):
        events.append(
            _event(
                f"{SPACE}/messages/close-{i}",
                f"Закрыли вакансию {_ROLES[i][0]} — вышел кандидат",
                ts(),
            )
        )

    # 6) Мусор (5).
    for i, junk in enumerate(_JUNK):
        events.append(_event(f"{SPACE}/messages/junk-{i}", junk, ts()))

    events.sort(key=lambda e: e["ts"])
    return events


def _envelope(ev: dict) -> dict:
    """Обернуть событие в Pub/Sub push-конверт (как присылает Google)."""
    msg = {
        "name": ev["mid"],
        "text": ev["text"],
        "createTime": ev["ts"].isoformat(),
        "space": {"name": SPACE},
        "sender": {"name": AUTHOR, "displayName": AUTHOR_NAME},
    }
    if ev["thread"]:
        msg["thread"] = {"name": ev["thread"]}
    if ev["quoted"]:
        msg["quotedMessageMetadata"] = {"name": ev["quoted"]}
    data_b64 = base64.b64encode(json.dumps({"message": msg}).encode()).decode()
    return {
        "message": {
            "data": data_b64,
            "messageId": ev["mid"],
            "attributes": {"ce-type": _CE[ev["ce"]]},
        }
    }


async def _send_all(url: str, events: list[dict]) -> int:
    sent = 0
    async with httpx.AsyncClient(timeout=10.0) as client:
        for ev in events:
            resp = await client.post(
                f"{url}/chat/pubsub-push", json=_envelope(ev)
            )
            if resp.status_code == 204:
                sent += 1
            else:
                print(f"  ! {ev['mid']} → HTTP {resp.status_code}: {resp.text[:120]}")
    return sent


async def _stats() -> dict:
    async with AsyncSessionLocal() as s:
        row = (
            await s.execute(
                text(
                    """
                    SELECT
                      count(*) FILTER (WHERE process_status='pending' AND is_deleted=false) AS pending,
                      count(*) FILTER (WHERE process_status='processed') AS processed,
                      count(*) FILTER (WHERE process_status='failed') AS failed,
                      count(*) AS total
                    FROM chat_messages WHERE space_id = :sp
                    """
                ),
                {"sp": SPACE},
            )
        ).first()
        vac = (
            await s.execute(
                text(
                    "SELECT count(*) FROM vacancies v "
                    "JOIN vacancy_revisions r ON r.vacancy_id = v.id "
                    "JOIN chat_messages m ON m.id = r.source_message_id "
                    "WHERE m.space_id = :sp AND v.is_deleted = false"
                ),
                {"sp": SPACE},
            )
        ).scalar()
    return {
        "pending": row.pending,
        "processed": row.processed,
        "failed": row.failed,
        "total": row.total,
        "vacancies": vac,
    }


async def _monitor(timeout: int) -> dict:
    """Ждать, пока тикер разберёт очередь (pending→0) либо истечёт таймаут."""
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
    last = None
    while datetime.now(timezone.utc) < deadline:
        st = await _stats()
        if st != last:
            print(
                f"  pending={st['pending']:>3} processed={st['processed']:>3} "
                f"failed={st['failed']:>2} vacancies={st['vacancies']:>3}"
            )
            last = st
        if st["pending"] == 0 and st["total"] > 0:
            return st
        await asyncio.sleep(5)
    return await _stats()


async def _run(url: str, timeout: int) -> None:
    events = build_events()
    created = sum(1 for e in events if e["ce"] == "created")
    updated = sum(1 for e in events if e["ce"] == "updated")
    print(f"Сгенерировано {len(events)} событий (created={created}, updated={updated})")

    print(f"\nОтправка на {url}/chat/pubsub-push ...")
    sent = await _send_all(url, events)
    print(f"Отправлено успешно (HTTP 204): {sent}/{len(events)}")

    print(f"\nМониторинг разбора (таймаут {timeout}s, тикер в фоне):")
    final = await _monitor(timeout)

    print("\n=== ИТОГ ===")
    print(f"  сообщений в БД : {final['total']}")
    print(f"  processed      : {final['processed']}")
    print(f"  pending        : {final['pending']}  (ждём 0)")
    print(f"  failed (яд)    : {final['failed']}  (ждём 0)")
    print(f"  вакансий       : {final['vacancies']}")
    verdict = "✅ всё разобрано" if final["pending"] == 0 else "⚠️ остались pending"
    print(f"  вердикт        : {verdict}")
    await engine.dispose()


async def _clean() -> None:
    """Удалить все данные spaces/LOADTEST из БД."""
    # Один DO-блок: всё внутри PostgreSQL, без биндинга UUID-массивов через asyncpg.
    # Порядок важен: сначала все ревизии вакансий (FK), потом сами вакансии,
    # потом сообщения (эмбеддинги каскадятся).
    sql = text(
        """
        DO $$
        DECLARE
            vac_ids UUID[];
        BEGIN
            SELECT ARRAY_AGG(DISTINCT vr.vacancy_id)
            INTO vac_ids
            FROM vacancy_revisions vr
            JOIN chat_messages m ON m.id = vr.source_message_id
            WHERE m.space_id = 'spaces/LOADTEST';

            IF vac_ids IS NOT NULL THEN
                DELETE FROM vacancy_revisions WHERE vacancy_id = ANY(vac_ids);
                UPDATE vacancies SET last_message_id = NULL WHERE id = ANY(vac_ids);
                DELETE FROM vacancies WHERE id = ANY(vac_ids);
            END IF;

            DELETE FROM chat_messages WHERE space_id = 'spaces/LOADTEST';

            -- Подчистить осиротевшие вакансии (без ревизий) от предыдущих запусков.
            DELETE FROM vacancies
            WHERE id NOT IN (SELECT DISTINCT vacancy_id FROM vacancy_revisions);
        END $$;
        """
    )
    async with AsyncSessionLocal() as s:
        async with s.begin():
            await s.execute(sql)

    # Проверяем что всё чисто
    async with AsyncSessionLocal() as s:
        msgs = (await s.execute(
            text("SELECT count(*) FROM chat_messages WHERE space_id = 'spaces/LOADTEST'")
        )).scalar()
        vacs = (await s.execute(
            text("SELECT count(*) FROM vacancies WHERE is_deleted = false")
        )).scalar()
    print(f"После очистки: сообщений LOADTEST={msgs}, вакансий в БД={vacs}")
    await engine.dispose()


async def _diagnose() -> None:
    """Полный расклад по ревизиям и сообщениям в LOADTEST-спейсе."""
    async with AsyncSessionLocal() as s:
        # 1. Ревизии по типу действия
        rows = (
            await s.execute(
                text(
                    """
                    SELECT vr.action, count(*) AS cnt
                    FROM vacancy_revisions vr
                    JOIN chat_messages m ON m.id = vr.source_message_id
                    WHERE m.space_id = :sp
                    GROUP BY vr.action
                    ORDER BY cnt DESC
                    """
                ),
                {"sp": SPACE},
            )
        ).fetchall()
        print("\n=== Ревизии по action ===")
        for r in rows:
            print(f"  {r.action:<10} {r.cnt}")

        # 2. Каждая ревизия с текстом источника
        rows = (
            await s.execute(
                text(
                    """
                    SELECT
                      v.title,
                      vr.action,
                      m.message_id,
                      left(m.text, 70) AS msg_text,
                      vr.changed_field
                    FROM vacancy_revisions vr
                    JOIN vacancies v ON v.id = vr.vacancy_id
                    JOIN chat_messages m ON m.id = vr.source_message_id
                    WHERE m.space_id = :sp
                    ORDER BY vr.created_at
                    """
                ),
                {"sp": SPACE},
            )
        ).fetchall()
        print(f"\n=== Все {len(rows)} ревизий ===")
        for r in rows:
            changed = f" [{r.changed_field}]" if r.changed_field else ""
            print(f"  [{r.action:<7}]{changed} {r.title!r:35} ← {r.msg_text!r}")

        # 3. Сообщения без ревизий (не попали ни в одну вакансию)
        rows = (
            await s.execute(
                text(
                    """
                    SELECT m.message_id, left(m.text, 70) AS text, m.process_status
                    FROM chat_messages m
                    WHERE m.space_id = :sp
                      AND NOT EXISTS (
                          SELECT 1 FROM vacancy_revisions vr
                          WHERE vr.source_message_id = m.id
                      )
                    ORDER BY m.created_at
                    """
                ),
                {"sp": SPACE},
            )
        ).fetchall()
        print(f"\n=== Сообщения без ревизий: {len(rows)} ===")
        for r in rows:
            print(f"  [{r.process_status}] {r.message_id:35} {r.text!r}")

    await engine.dispose()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--diagnose", action="store_true", help="показать расклад по БД без запуска теста")
    p.add_argument("--clean", action="store_true", help="удалить данные spaces/LOADTEST из БД")
    args = p.parse_args()
    if args.diagnose:
        asyncio.run(_diagnose())
    elif args.clean:
        asyncio.run(_clean())
    else:
        asyncio.run(_run(args.url, args.timeout))
