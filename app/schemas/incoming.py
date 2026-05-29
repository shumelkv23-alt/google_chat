"""Нормализованное представление входящего WE API события."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

EventType = Literal["created", "updated", "deleted"]

# WE API message event type → нормализованный event_type.
_TYPE_MAP: dict[str, EventType] = {
    "google.workspace.chat.message.v1.created": "created",
    "google.workspace.chat.message.v1.updated": "updated",
    "google.workspace.chat.message.v1.deleted": "deleted",
}


class IncomingMessage(BaseModel):
    message_id: str
    space_id: str
    thread_id: str | None
    author_id: str
    author_name: str | None
    text: str
    created_at: datetime
    source: str = "chat_a"
    event_type: EventType = "created"


def parse_we_event(data: dict) -> IncomingMessage | None:
    """WE API event dict → IncomingMessage. None если событие не нужно обрабатывать."""
    event_type = _TYPE_MAP.get(data.get("type", ""))
    if event_type is None:
        return None  # неизвестный/ненужный тип события

    msg = data.get("message", {})
    message_id = msg.get("name")
    if not message_id:
        return None  # без идентификатора делать нечего

    text = msg.get("text", "").strip()
    # Пустой текст пропускаем только для created/updated; у deleted текста может не быть.
    if not text and event_type != "deleted":
        return None

    # У deleted-события createTime может отсутствовать — нам важен только message_id.
    created_raw = msg.get("createTime")
    created_at = created_raw if created_raw else datetime.now(timezone.utc)

    return IncomingMessage(
        message_id=message_id,
        space_id=msg.get("space", {}).get("name", ""),
        thread_id=msg.get("thread", {}).get("name"),
        author_id=msg.get("sender", {}).get("name", ""),
        author_name=msg.get("sender", {}).get("displayName"),
        text=text,
        created_at=created_at,
        event_type=event_type,
    )
