"""Этап 8: роутинг /chat/pubsub-push по типу события (created/updated/deleted).

Лёгкий unit: фейковый Request + перехват background_tasks, JWT отключён патчем.
В сеть/БД не ходим — проверяем только, какой таск поставлен в очередь.
"""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

from app.api import pubsub


_MESSAGE = {
    "name": "spaces/T/messages/route-1",
    "createTime": "2024-01-01T12:00:00Z",
    "sender": {"name": "users/u", "displayName": "U"},
    "text": "Ищем python разработчика",
    "thread": {"name": "spaces/T/threads/th"},
    "space": {"name": "spaces/T"},
}


def _envelope(ce_type: str) -> dict:
    # Тип события — в атрибуте CloudEvents `ce-type`, тело data содержит ресурс.
    data_b64 = base64.b64encode(json.dumps({"message": _MESSAGE}).encode()).decode()
    return {
        "message": {
            "data": data_b64,
            "messageId": "route-msg-1",
            "attributes": {"ce-type": ce_type},
        }
    }


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


async def _route(ce_type: str, *, is_new: bool = True) -> list:
    bg = _CapturingBG()
    req = _FakeRequest(_envelope(ce_type))
    # persist_message теперь вызывается синхронно — мокаем, чтобы не ходить в БД.
    with patch.object(pubsub.settings, "skip_jwt_validation", True), patch.object(
        pubsub, "persist_message", AsyncMock(return_value=is_new)
    ):
        await pubsub.pubsub_push(req, bg)
    return [func for func, _ in bg.calls]


def test_created_routes_to_embed_and_extraction() -> None:
    funcs = asyncio.run(_route("google.workspace.chat.message.v1.created"))
    assert funcs == [pubsub.embed_message, pubsub.run_extraction]


def test_created_duplicate_embeds_but_skips_extraction() -> None:
    # Дубликат (Pub/Sub at-least-once): вектор догоняем, но LLM-extraction не жжём.
    funcs = asyncio.run(
        _route("google.workspace.chat.message.v1.created", is_new=False)
    )
    assert funcs == [pubsub.embed_message]


def test_updated_routes_to_handle_edit() -> None:
    funcs = asyncio.run(_route("google.workspace.chat.message.v1.updated"))
    assert funcs == [pubsub.handle_edit]


def test_deleted_routes_to_handle_delete() -> None:
    funcs = asyncio.run(_route("google.workspace.chat.message.v1.deleted"))
    assert funcs == [pubsub.handle_delete]


def test_unknown_type_routes_nothing() -> None:
    funcs = asyncio.run(_route("google.workspace.chat.reaction.v1.created"))
    assert funcs == []
