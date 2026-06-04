"""Робастность пути приёма событий (parse_we_event + /chat/pubsub-push).

Раньше «грязный» payload (null-поля, кривой createTime) ронял обработчик в 500,
а Pub/Sub при non-2xx бесконечно повторял доставку (poison pill). После фикса:
parse_we_event null-безопасен, а хендлер ловит любую ошибку парсинга и мягко
подтверждает приём (204).
"""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

from app.api import pubsub
from app.schemas.incoming import parse_we_event

_CREATED = "google.workspace.chat.message.v1.created"
_DELETED = "google.workspace.chat.message.v1.deleted"


# --- parse_we_event: null-поля больше не роняют парсер --------------------


def test_parse_skips_null_text_on_created() -> None:
    event = {
        "type": _CREATED,
        "message": {
            "name": "spaces/T/messages/1",
            "text": None,  # явный null, а не отсутствие ключа
            "sender": {"name": "users/u"},
            "space": {"name": "spaces/T"},
        },
    }
    assert parse_we_event(event) is None  # пустой текст → мягкий скип


def test_parse_allows_null_text_on_deleted() -> None:
    event = {
        "type": _DELETED,
        "message": {"name": "spaces/T/messages/1", "text": None},
    }
    msg = parse_we_event(event)
    assert msg is not None
    assert msg.event_type == "deleted"


def test_parse_handles_null_sender_and_space() -> None:
    event = {
        "type": _CREATED,
        "message": {
            "name": "spaces/T/messages/1",
            "text": "hi",
            "sender": None,
            "space": None,
            "thread": None,
        },
    }
    msg = parse_we_event(event)
    assert msg is not None
    assert msg.author_id == ""
    assert msg.space_id == ""
    assert msg.thread_id is None


# --- /chat/pubsub-push: кривой payload подтверждается, а не падает --------


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


def _envelope(message: dict, ce_type: str) -> dict:
    data_b64 = base64.b64encode(json.dumps({"message": message}).encode()).decode()
    return {
        "message": {
            "data": data_b64,
            "messageId": "m-1",
            "attributes": {"ce-type": ce_type},
        }
    }


async def _push(message: dict, ce_type: str, persist: AsyncMock):
    bg = _CapturingBG()
    req = _FakeRequest(_envelope(message, ce_type))
    with patch.object(pubsub.settings, "skip_jwt_validation", True), patch.object(
        pubsub, "persist_message", persist
    ):
        return await pubsub.pubsub_push(req, bg)


def test_malformed_payload_acks_with_204_instead_of_500() -> None:
    # Кривой createTime роняет pydantic-валидацию внутри parse_we_event —
    # хендлер обязан поймать это и вернуть 204, а не упасть в 500.
    bad_message = {
        "name": "spaces/T/messages/1",
        "text": "hi",
        "createTime": "не-дата",
        "sender": {"name": "users/u"},
        "space": {"name": "spaces/T"},
    }
    persist = AsyncMock(return_value=True)
    resp = asyncio.run(_push(bad_message, _CREATED, persist))
    assert resp.status_code == 204
    persist.assert_not_awaited()  # до записи в БД дело не дошло
