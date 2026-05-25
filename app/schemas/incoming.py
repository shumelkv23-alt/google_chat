"""Нормализованное представление входящего WE API события."""

from datetime import datetime

from pydantic import BaseModel


class IncomingMessage(BaseModel):
    message_id: str
    space_id: str
    thread_id: str | None
    author_id: str
    author_name: str | None
    text: str
    created_at: datetime
    source: str = "chat_a"


def parse_we_event(data: dict) -> IncomingMessage | None:
    """WE API event dict → IncomingMessage. None если событие не нужно сохранять."""
    if data.get("type") != "google.workspace.chat.message.v1.created":
        return None

    msg = data.get("message", {})
    text = msg.get("text", "").strip()
    if not text:
        return None

    return IncomingMessage(
        message_id=msg["name"],
        space_id=msg.get("space", {}).get("name", ""),
        thread_id=msg.get("thread", {}).get("name"),
        author_id=msg.get("sender", {}).get("name", ""),
        author_name=msg.get("sender", {}).get("displayName"),
        text=text,
        created_at=msg["createTime"],
    )
