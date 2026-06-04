"""Управление Workspace Events подпиской на Чат А.

У подписки с includeResource=True максимальный TTL — 4 часа, «вечной»
подписки не бывает. Поэтому её приходится продлевать: продление — это
`subscriptions.patch` с updateMask=ttl, который сдвигает expireTime на now+ttl.

Обычно продление идёт автоматически фоновым циклом FastAPI-приложения
(`renewal_loop`, см. app/main.py). Создать/продлить вручную можно через
`python -m scripts.create_subscription`.
"""

import asyncio

from googleapiclient.discovery import build

from app.config import settings
from app.logger import logger
from app.services.google_oauth import get_credentials

# includeResource=True ограничивает TTL четырьмя часами.
SUBSCRIPTION_TTL_SECONDS = 4 * 60 * 60  # 14400
# Продлеваем заранее — за 30 минут до истечения TTL.
RENEW_INTERVAL_SECONDS = SUBSCRIPTION_TTL_SECONDS - 30 * 60  # 12600 (~3.5ч)
# Если продление упало — повторяем чаще, не ждём полный цикл.
RETRY_INTERVAL_SECONDS = 5 * 60

_EVENT_TYPES = [
    "google.workspace.chat.message.v1.created",
    "google.workspace.chat.message.v1.updated",
    "google.workspace.chat.message.v1.deleted",
]


def _target_resource() -> str:
    return f"//chat.googleapis.com/{settings.chat_a_space_id}"


def _build_service():
    return build("workspaceevents", "v1", credentials=get_credentials())


def _find_existing(svc) -> dict | None:
    """Найти существующую (живую) подписку для нашего пространства, либо None.

    Обходит все страницы: иначе подписка со второй страницы будет «не найдена»
    и мы создадим дубликат. Удаляемые (DELETED) подписки пропускаем — продлить
    их нельзя, нужна новая.
    """
    target = _target_resource()
    page_token: str | None = None
    while True:
        resp = (
            svc.subscriptions()
            .list(filter=f'event_types:"{_EVENT_TYPES[0]}"', pageToken=page_token)
            .execute()
        )
        for sub in resp.get("subscriptions", []):
            if sub.get("targetResource") == target and sub.get("state") != "DELETED":
                return sub
        page_token = resp.get("nextPageToken")
        if not page_token:
            return None


def _create(svc) -> dict:
    body = {
        "targetResource": _target_resource(),
        "eventTypes": _EVENT_TYPES,
        "notificationEndpoint": {"pubsubTopic": settings.google_pubsub_topic},
        "payloadOptions": {"includeResource": True},
        "ttl": f"{SUBSCRIPTION_TTL_SECONDS}s",
    }
    return svc.subscriptions().create(body=body).execute()


def _renew(svc, name: str) -> dict:
    return (
        svc.subscriptions()
        .patch(name=name, updateMask="ttl", body={"ttl": f"{SUBSCRIPTION_TTL_SECONDS}s"})
        .execute()
    )


def ensure_subscription() -> dict:
    """Создать подписку, если её нет; иначе продлить TTL.

    Возвращает сырой ответ API (Operation для create/patch).
    """
    svc = _build_service()
    existing = _find_existing(svc)

    if existing is None:
        logger.info("subscription_creating", target=_target_resource())
        result = _create(svc)
        logger.info("subscription_created", operation=result.get("name"))
        return result

    name = existing["name"]
    logger.info("subscription_renewing", name=name)
    result = _renew(svc, name)
    logger.info("subscription_renewed", name=name)
    return result


def is_subscription_configured() -> bool:
    """Заданы ли настройки, без которых подписку не создать."""
    return bool(
        settings.google_refresh_token
        and settings.chat_a_space_id
        and settings.google_pubsub_topic
    )


async def renewal_loop() -> None:
    """Фоновый цикл: при старте создаёт/продлевает подписку, затем продлевает
    каждые ~3.5 часа. Блокирующий googleapiclient выносим в поток."""
    while True:
        try:
            await asyncio.to_thread(ensure_subscription)
            delay = RENEW_INTERVAL_SECONDS
        except Exception as e:  # продление не должно ронять приложение
            logger.error("subscription_renewal_failed", error=str(e))
            delay = RETRY_INTERVAL_SECONDS
        await asyncio.sleep(delay)
