"""Нагрузочный e2e-тест batch-режима через HTTP-приём (уровень B).

Шлёт ~100 фейковых Pub/Sub-push на /chat/pubsub-push (как делает реальный
Pub/Sub), затем мониторит БД, пока фоновый тикер не разберёт всё (pending → 0).

Состав — реалистичная смесь: независимые объявления, follow-up'ы в тредах,
правки (updated), цитаты, закрытия и мусор. Проверяет весь путь: HTTP-приём →
persist → маршрутизация → тикер → флаш → mark, плюс batch-фичи (связывание,
история [hN], бисекция при необходимости).

Предусловия:
  - сервер поднят локально в batch-режиме (PROCESSING_MODE=batch),
    напр.: PROCESSING_MODE=batch BATCH_SIZE=20 uvicorn app.main:app --port 8000
  - skip_jwt_validation=True (иначе push отклонит JWT-проверка)

Запуск:
    python -m scripts.load_test
    python -m scripts.load_test --url http://localhost:8000 --timeout 600
"""

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

# Роли для независимых объявлений — каждое создаёт отдельную вакансию.
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


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--timeout", type=int, default=600)
    args = p.parse_args()
    asyncio.run(_run(args.url, args.timeout))
