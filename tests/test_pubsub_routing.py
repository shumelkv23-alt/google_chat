"""Этап 8: роутинг /chat/pubsub-push по типу события (created/updated/deleted).

Лёгкий unit: фейковый Request + перехват background_tasks, JWT отключён патчем.
В сеть/БД не ходим — проверяем только, какой таск поставлен в очередь.
"""

import asyncio
import base64
import json
from unittest.mock import patch

from app.api import pubsub


def _event(suffix: str) -> dict:
    return {
        "type": f"google.workspace.chat.message.v1.{suffix}",
        "message": {
            "name": "spaces/T/messages/route-1",
            "createTime": "2024-01-01T12:00:00Z",
            "sender": {"name": "users/u", "displayName": "U"},
            "text": "Ищем python разработчика",
            "thread": {"name": "spaces/T/threads/th"},
            "space": {"name": "spaces/T"},
        },
    }


def _envelope(event: dict) -> dict:
    data_b64 = base64.b64encode(json.dumps(event).encode()).decode()
    return {"message": {"data": data_b64, "messageId": "route-msg-1"}}


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.headers: dict = {}

    async def json(self) -> dict:
        return self._payload


class _CapturingBG:
    def __init__(self) -> None:
        self.calls: list = []

    def add_task(self, func, *args) -> None:
        self.calls.append((func, args))


async def _route(event: dict) -> list:
    bg = _CapturingBG()
    req = _FakeRequest(_envelope(event))
    with patch.object(pubsub.settings, "skip_jwt_validation", True):
        await pubsub.pubsub_push(req, bg)
    return [func for func, _ in bg.calls]


def test_created_routes_to_ingest_and_extraction() -> None:
    funcs = asyncio.run(_route(_event("created")))
    assert funcs == [pubsub.ingest_message, pubsub.run_extraction]


def test_updated_routes_to_handle_edit() -> None:
    funcs = asyncio.run(_route(_event("updated")))
    assert funcs == [pubsub.handle_edit]


def test_deleted_routes_to_handle_delete() -> None:
    funcs = asyncio.run(_route(_event("deleted")))
    assert funcs == [pubsub.handle_delete]


def test_unknown_type_routes_nothing() -> None:
    funcs = asyncio.run(_route(_event("reactionAdded")))
    assert funcs == []
