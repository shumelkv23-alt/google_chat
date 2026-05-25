"""Создать Workspace Events подписку на Чат А.

Подписка живёт 4 часа (includeResource=True).
Запускай вручную или автоматически каждые ~3.5 часа.

Запуск:
    python -m scripts.create_subscription
"""

import json

from googleapiclient.discovery import build

from app.config import settings
from app.services.google_oauth import get_credentials


def main() -> None:
    creds = get_credentials()
    svc = build("workspaceevents", "v1", credentials=creds)

    body = {
        "targetResource": f"//chat.googleapis.com/{settings.chat_a_space_id}",
        "eventTypes": [
            "google.workspace.chat.message.v1.created",
            "google.workspace.chat.message.v1.updated",
            "google.workspace.chat.message.v1.deleted",
        ],
        "notificationEndpoint": {
            "pubsubTopic": settings.google_pubsub_topic,
        },
        "payloadOptions": {
            "includeResource": True,
        },
    }

    print("Создаём подписку...")
    result = svc.subscriptions().create(body=body).execute()
    print("Подписка создана!")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
