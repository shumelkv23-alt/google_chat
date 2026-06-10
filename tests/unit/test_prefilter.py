"""Юнит-тесты pre-filter is_vacancy_message с подменой LLM-вызова.

is_vacancy_message зовёт LLM (chat) и трактует ответ. Сам LLM нам не нужен —
подменяем chat на AsyncMock и проверяем НАШУ логику: трактовку ответа и сборку
запроса. Это всё ещё честный юнит-тест: наружу (в сеть) ничего не уходит.

Тонкость: патчим chat там, где он ИСПОЛЬЗУЕТСЯ (app.llm.prefilter.chat), а не там,
где определён (app.llm.client.chat). prefilter сделал `from ... import chat` —
имя уже связано в его модуле, и подменять надо именно это связанное имя.
"""

from unittest.mock import AsyncMock

import pytest

from app.llm.prefilter import _SYSTEM, _SYSTEM_WITH_CONTEXT, is_vacancy_message


pytestmark = pytest.mark.anyio


async def test_returns_true_when_llm_says_yes(monkeypatch):
    mock_chat = AsyncMock(return_value="yes")
    monkeypatch.setattr("app.llm.prefilter.chat", mock_chat)

    assert await is_vacancy_message("ищем питониста") is True
    mock_chat.assert_awaited_once()


async def test_returns_false_when_llm_says_no(monkeypatch):
    monkeypatch.setattr("app.llm.prefilter.chat", AsyncMock(return_value="no"))

    assert await is_vacancy_message("всем привет, как дела") is False


async def test_answer_is_case_and_whitespace_insensitive(monkeypatch):

    monkeypatch.setattr("app.llm.prefilter.chat", AsyncMock(return_value="  YES\n"))

    assert await is_vacancy_message("нужен бэкендер") is True


async def test_uses_plain_system_prompt_without_context(monkeypatch):
    
    mock_chat = AsyncMock(return_value="yes")
    monkeypatch.setattr("app.llm.prefilter.chat", mock_chat)

    await is_vacancy_message("ищем DevOps")

    messages = mock_chat.await_args.kwargs["messages"]
    assert messages[0]["content"] == _SYSTEM
    assert messages[1]["content"] == "ищем DevOps"


async def test_uses_context_prompt_and_includes_context(monkeypatch):
    # С контекстом переключается на _SYSTEM_WITH_CONTEXT, а контекст вшивается в user
    mock_chat = AsyncMock(return_value="yes")
    monkeypatch.setattr("app.llm.prefilter.chat", mock_chat)
    context = [{"author_name": "Аня", "text": "по бэкенд-вакансии"}]

    await is_vacancy_message("теперь удалёнка", context)

    messages = mock_chat.await_args.kwargs["messages"]
    assert messages[0]["content"] == _SYSTEM_WITH_CONTEXT
    user_content = messages[1]["content"]
    assert "Контекст:" in user_content
    assert "Аня: по бэкенд-вакансии" in user_content
    assert "теперь удалёнка" in user_content
