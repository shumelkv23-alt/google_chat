"""Роутинг /chat/interaction: резюме (файл/префикс) vs обычный запрос.

БД и тяжёлые шаги мокаем — проверяем только, какой обработчик вызван и что ушло
в ответ. detect_resume_text НЕ мокаем (нужен реальный разбор префикса).
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from app.api import interactions


class _FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.headers: dict = {}

    async def json(self) -> dict:
        return self._payload


class _BG:
    def add_task(self, *args, **kwargs) -> None:
        pass


def _event(text: str = "", attachment: list | None = None) -> dict:
    message: dict = {"text": text}
    if attachment is not None:
        message["attachment"] = attachment
    return {
        "type": "MESSAGE",
        "user": {"name": "users/1"},
        "space": {"name": "spaces/x"},
        "message": message,
    }


def _run(event: dict, *, attach: tuple = (None, None)):
    """Вызвать эндпоинт с замоканными зависимостями. Возвращает (body, match_mock, query_mock).

    attach — то, что вернёт extract_text_from_attachments: (text, hint).
    """
    match_mock = AsyncMock(return_value=({"text": "MATCH"}, "MATCH"))
    query_mock = AsyncMock(return_value=({"text": "QUERY"}, "QUERY"))
    with patch.object(interactions.settings, "skip_jwt_validation", True), patch.object(
        interactions, "_get_or_create_conversation", new=AsyncMock(return_value=object())
    ), patch.object(interactions, "_append_turns", new=AsyncMock()), patch.object(
        interactions,
        "extract_text_from_attachments",
        new=AsyncMock(return_value=attach),
    ), patch.object(interactions, "match_resume", new=match_mock), patch.object(
        interactions, "handle_query", new=query_mock
    ):
        resp = asyncio.run(interactions.chat_interaction(_FakeRequest(event), _BG()))
    return json.loads(resp.body), match_mock, query_mock


def test_file_attachment_routes_to_match_resume() -> None:
    event = _event(text="", attachment=[{"contentType": "application/pdf"}])
    body, match_mock, query_mock = _run(event, attach=("резюме из файла", None))
    assert body == {"text": "MATCH"}
    match_mock.assert_awaited_once()
    query_mock.assert_not_awaited()


def test_text_prefix_routes_to_match_resume() -> None:
    event = _event(text="Резюме: Python, 5 лет, FastAPI")
    body, match_mock, query_mock = _run(event)
    assert body == {"text": "MATCH"}
    match_mock.assert_awaited_once()
    # в match_resume ушёл текст после префикса
    assert match_mock.await_args.args[0] == "Python, 5 лет, FastAPI"
    query_mock.assert_not_awaited()


def test_plain_query_routes_to_handle_query() -> None:
    event = _event(text="сколько вакансий открыто")
    body, match_mock, query_mock = _run(event)
    assert body == {"text": "QUERY"}
    query_mock.assert_awaited_once()
    match_mock.assert_not_awaited()


def test_attachment_hint_is_shown_to_user() -> None:
    # Файл скачался/распарсился неудачно — юзеру уходит конкретная подсказка.
    event = _event(text="", attachment=[{"contentType": "application/pdf"}])
    hint = "Похоже, это скан или картинка — текст не извлекается."
    body, match_mock, query_mock = _run(event, attach=(None, hint))
    assert body == {"text": hint}
    match_mock.assert_not_awaited()
    query_mock.assert_not_awaited()


def test_unreadable_unsupported_attachment_gives_generic_hint() -> None:
    # Неподдерживаемый тип → extract вернул (None, None) → общий ответ.
    event = _event(text="", attachment=[{"contentType": "image/png"}])
    body, match_mock, query_mock = _run(event, attach=(None, None))
    assert "Не смог прочитать вложение" in body["text"]
    match_mock.assert_not_awaited()
    query_mock.assert_not_awaited()


def test_empty_message_returns_nothing() -> None:
    event = _event(text="")
    body, match_mock, query_mock = _run(event)
    assert body == {}
    match_mock.assert_not_awaited()
    query_mock.assert_not_awaited()
