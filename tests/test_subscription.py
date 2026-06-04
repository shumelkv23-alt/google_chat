"""Тесты механизма WE-подписки (app/services/subscription.py).

Этот модуль держит Workspace Events подписку живой (create/renew + фоновый
цикл), но до сих пор не имел покрытия. Сеть не трогаем: googleapiclient
подменяем фейковым сервисом, get_credentials/build не вызываются.
"""

import asyncio
from unittest.mock import patch

import pytest

from app.services import subscription

_TARGET = "//chat.googleapis.com/spaces/AAA"


# --- Фейковый googleapiclient-сервис -------------------------------------


class _FakeExec:
    def __init__(self, result: dict) -> None:
        self._result = result

    def execute(self) -> dict:
        return self._result


class _FakeSubscriptions:
    def __init__(self, pages: list[dict]) -> None:
        # pages[i] ссылается на следующую через nextPageToken="page-<i+1>".
        self._pages = pages
        self.list_tokens: list = []
        self.created: dict | None = None
        self.patched: dict | None = None

    def list(self, **kwargs):
        token = kwargs.get("pageToken")
        self.list_tokens.append(token)
        idx = 0 if token is None else int(token.split("-")[1])
        return _FakeExec(self._pages[idx])

    def create(self, body=None):
        self.created = body
        return _FakeExec({"name": "operations/create-1"})

    def patch(self, name=None, updateMask=None, body=None):
        self.patched = {"name": name, "updateMask": updateMask, "body": body}
        return _FakeExec({"name": "operations/patch-1"})


class _FakeService:
    def __init__(self, pages: list[dict]) -> None:
        self._subs = _FakeSubscriptions(pages)

    def subscriptions(self):
        return self._subs


def _pages(*sub_lists: list) -> list[dict]:
    """Собрать страницы list-ответа со сцепкой через nextPageToken."""
    out: list[dict] = []
    for i, subs in enumerate(sub_lists):
        page: dict = {"subscriptions": subs}
        if i + 1 < len(sub_lists):
            page["nextPageToken"] = f"page-{i + 1}"
        out.append(page)
    return out


def _patched_service(*sub_lists: list) -> _FakeService:
    return _FakeService(_pages(*sub_lists))


# --- ensure_subscription: create vs renew --------------------------------


def test_creates_subscription_when_none_exists() -> None:
    fake = _patched_service([])
    with patch.object(subscription, "_build_service", return_value=fake), patch.object(
        subscription.settings, "chat_a_space_id", "spaces/AAA"
    ), patch.object(
        subscription.settings, "google_pubsub_topic", "projects/p/topics/t"
    ):
        result = subscription.ensure_subscription()

    subs = fake.subscriptions()
    assert subs.created is not None, "должен быть вызван create"
    assert subs.patched is None, "renew не должен вызываться"
    assert subs.created["targetResource"] == _TARGET
    assert subs.created["notificationEndpoint"]["pubsubTopic"] == "projects/p/topics/t"
    assert subs.created["payloadOptions"]["includeResource"] is True
    assert subs.created["ttl"] == "14400s"
    assert subs.created["eventTypes"] == subscription._EVENT_TYPES
    assert result["name"] == "operations/create-1"


def test_renews_subscription_when_one_matches() -> None:
    existing = {"name": "subscriptions/123", "targetResource": _TARGET}
    fake = _patched_service([existing])
    with patch.object(subscription, "_build_service", return_value=fake), patch.object(
        subscription.settings, "chat_a_space_id", "spaces/AAA"
    ):
        result = subscription.ensure_subscription()

    subs = fake.subscriptions()
    assert subs.created is None, "create не должен вызываться при существующей подписке"
    assert subs.patched is not None
    assert subs.patched["name"] == "subscriptions/123"
    assert subs.patched["updateMask"] == "ttl"
    assert subs.patched["body"] == {"ttl": "14400s"}
    assert result["name"] == "operations/patch-1"


def test_create_when_existing_belongs_to_other_space() -> None:
    other = {
        "name": "subscriptions/999",
        "targetResource": "//chat.googleapis.com/spaces/OTHER",
    }
    fake = _patched_service([other])
    with patch.object(subscription, "_build_service", return_value=fake), patch.object(
        subscription.settings, "chat_a_space_id", "spaces/AAA"
    ), patch.object(
        subscription.settings, "google_pubsub_topic", "projects/p/topics/t"
    ):
        subscription.ensure_subscription()

    assert fake.subscriptions().created is not None
    assert fake.subscriptions().patched is None


def test_find_existing_walks_pagination() -> None:
    """Наша подписка на второй странице — _find_existing обязан её найти."""
    page_one_other = {"targetResource": "//chat.googleapis.com/spaces/X"}
    our_sub = {"name": "subscriptions/777", "targetResource": _TARGET}
    fake = _patched_service([page_one_other], [our_sub])
    with patch.object(subscription.settings, "chat_a_space_id", "spaces/AAA"):
        found = subscription._find_existing(fake)
    assert found == our_sub
    assert fake.subscriptions().list_tokens == [None, "page-1"]  # обошёл обе страницы


def test_find_existing_skips_deleted_subscription() -> None:
    """Подписку в состоянии DELETED продлевать нельзя — игнорируем её."""
    dead = {"name": "subscriptions/dead", "targetResource": _TARGET, "state": "DELETED"}
    fake = _patched_service([dead])
    with patch.object(subscription.settings, "chat_a_space_id", "spaces/AAA"):
        assert subscription._find_existing(fake) is None


# --- is_subscription_configured ------------------------------------------


def test_is_configured_true_when_all_present() -> None:
    with patch.object(subscription.settings, "google_refresh_token", "rt"), patch.object(
        subscription.settings, "chat_a_space_id", "spaces/A"
    ), patch.object(subscription.settings, "google_pubsub_topic", "topic"):
        assert subscription.is_subscription_configured() is True


@pytest.mark.parametrize("missing", ["google_refresh_token", "chat_a_space_id", "google_pubsub_topic"])
def test_is_configured_false_when_any_missing(missing: str) -> None:
    values = {
        "google_refresh_token": "rt",
        "chat_a_space_id": "spaces/A",
        "google_pubsub_topic": "topic",
    }
    values[missing] = ""
    with patch.object(subscription.settings, "google_refresh_token", values["google_refresh_token"]), patch.object(
        subscription.settings, "chat_a_space_id", values["chat_a_space_id"]
    ), patch.object(subscription.settings, "google_pubsub_topic", values["google_pubsub_topic"]):
        assert subscription.is_subscription_configured() is False


# --- renewal_loop: выбор интервала сна -----------------------------------


class _StopLoop(Exception):
    """Прерываем бесконечный while True после первой итерации."""


def _run_loop_once(to_thread_side_effect):
    """Гоняет renewal_loop ровно одну итерацию, возвращает использованный delay."""
    delays: list[float] = []

    async def fake_sleep(delay):
        delays.append(delay)
        raise _StopLoop

    async def fake_to_thread(func, *args, **kwargs):
        return to_thread_side_effect()

    with patch.object(subscription.asyncio, "to_thread", fake_to_thread), patch.object(
        subscription.asyncio, "sleep", fake_sleep
    ):
        with pytest.raises(_StopLoop):
            asyncio.run(subscription.renewal_loop())
    return delays


def test_renewal_loop_uses_renew_interval_on_success() -> None:
    delays = _run_loop_once(lambda: {"name": "op"})
    assert delays == [subscription.RENEW_INTERVAL_SECONDS]


def test_renewal_loop_uses_retry_interval_on_failure() -> None:
    def boom():
        raise RuntimeError("API down")

    delays = _run_loop_once(boom)
    assert delays == [subscription.RETRY_INTERVAL_SECONDS]
