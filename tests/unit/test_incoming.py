"""Юнит-тесты парсера входящих событий Google Chat → IncomingMessage.

parse_we_event — чистая функция (dict → IncomingMessage | None), без БД и сети,
поэтому тесты быстрые и детерминированные. Проверяем happy-path и все ветки,
где событие отбрасывается или поля подставляются по умолчанию.
"""

from datetime import datetime

from app.schemas.incoming import parse_we_event


def _make_event(**overrides) -> dict:
    """Собрать валидное created-событие; overrides точечно меняют message-поля.

    Хелпер, чтобы каждый тест не таскал полный dict — меняем только то,
    что относится к проверяемому случаю (DRY + читаемость).
    """
    message = {
        "name": "spaces/AAA/messages/MSG1",
        "text": "ищем питониста в команду бэкенда",
        "createTime": "2026-06-10T12:00:00Z",
        "space": {"name": "spaces/AAA"},
        "thread": {"name": "spaces/AAA/threads/T1"},
        "sender": {"name": "users/123", "displayName": "Кирилл"},
    }
    message.update(overrides)
    return {"type": "google.workspace.chat.message.v1.created", "message": message}


def test_parses_valid_created_event_into_all_fields():
    # Arrange
    event = _make_event()

    # Act
    msg = parse_we_event(event)

    # Assert — happy path: все поля смаплены из вложенного dict
    assert msg is not None
    assert msg.message_id == "spaces/AAA/messages/MSG1"
    assert msg.space_id == "spaces/AAA"
    assert msg.thread_id == "spaces/AAA/threads/T1"
    assert msg.author_id == "users/123"
    assert msg.author_name == "Кирилл"
    assert msg.text == "ищем питониста в команду бэкенда"
    assert msg.event_type == "created"
    assert msg.quoted_message_id is None


def test_returns_none_for_unknown_event_type():
    # Тип не из _TYPE_MAP → событие нам не интересно
    event = _make_event()
    event["type"] = "google.workspace.chat.reaction.v1.created"

    assert parse_we_event(event) is None


def test_returns_none_when_message_id_missing():
    # Без message.name делать нечего — нечем идентифицировать сообщение
    event = _make_event()
    del event["message"]["name"]

    assert parse_we_event(event) is None


def test_returns_none_for_empty_text_on_created():
    # Пустой текст у created/updated пропускаем
    event = _make_event(text="   ")  # только пробелы → после strip пусто

    assert parse_we_event(event) is None


def test_returns_none_when_text_is_null_on_created():
    # Google может прислать text: null — `or ""` должен это поймать
    event = _make_event(text=None)

    assert parse_we_event(event) is None


def test_keeps_deleted_event_without_text():
    # У deleted текста может не быть — такое событие НЕ отбрасываем
    event = _make_event(text=None)
    event["type"] = "google.workspace.chat.message.v1.deleted"

    msg = parse_we_event(event)

    assert msg is not None
    assert msg.event_type == "deleted"
    assert msg.text == ""


def test_extracts_quoted_message_id():
    # quotedMessageMetadata.name → quoted_message_id (сигнал привязки follow-up)
    event = _make_event(
        quotedMessageMetadata={"name": "spaces/AAA/messages/QUOTED"}
    )

    msg = parse_we_event(event)

    assert msg is not None
    assert msg.quoted_message_id == "spaces/AAA/messages/QUOTED"


def test_falls_back_to_now_when_create_time_missing():
    # Без createTime created_at подставляется текущим временем (тип datetime)
    event = _make_event()
    del event["message"]["createTime"]

    msg = parse_we_event(event)

    assert msg is not None
    assert isinstance(msg.created_at, datetime)


def test_defaults_for_missing_nested_blocks():
    # Нет space/thread/sender → безопасные дефолты, без падения
    event = _make_event()
    for key in ("space", "thread", "sender"):
        del event["message"][key]

    msg = parse_we_event(event)

    assert msg is not None
    assert msg.space_id == ""
    assert msg.thread_id is None
    assert msg.author_id == ""
    assert msg.author_name is None
