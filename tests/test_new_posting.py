"""Юнит-тесты детектора «новое объявление о найме».

- _is_new_posting — быстрая regex-эвристика.
- _decide_new_posting — связка regex + LLM (LLM решает при уверенности, иначе regex).
- classify_new_posting — парсинг ответа LLM с безопасным фолбэком.

Сигнал решает, создавать ли отдельную вакансию на ту же роль (другой найм)
или дописывать условия в существующую. Сети/БД не нужно.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.llm import posting_classifier
from app.llm.posting_classifier import PostingVerdict, classify_new_posting
from app.services import entity_resolution
from app.services.entity_resolution import _decide_new_posting, _is_new_posting


@pytest.mark.parametrize(
    "text",
    [
        "ищем в команду Microsoft системного администратора на полную ставку",
        "Ищу питониста на проект",
        "нужен системный администратор",
        "нужна QA на удалёнку",
        "требуется DevOps",
        "требуются специалисты по тестированию",
        "набираем команду бэкендеров",
        "набирается отдел поддержки",
        "разыскивается senior Go разработчик",
        "приглашаем дизайнера в продукт",
        "зовём в команду аналитика",
        "Хотим нанять UI/UX дизайнера уровня джуниор на ставку 1000 долларов в Apple",
        "хотим нанять в команду Apple продакт менеджера",
        "нанимаем ещё одного бэкендера",
        "наймём аналитика в команду данных",
        "рассматриваем кандидатов на роль тимлида",
        "открыли вакансию: Java разработчик",
        "открыта позиция продакт-менеджера",
        "новая вакансия — UI/UX дизайнер",
        "новую роль архитектора закрываем наймом",
        "есть вакансия в команде данных",
        "появилась вакансия фронтендера",
        "We are hiring a backend engineer",
        "looking for a product designer",
        "open position: DevOps",
        "join our team as data scientist",
    ],
)
def test_detects_new_posting(text: str) -> None:
    assert _is_new_posting(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "по системному админу подняли зп до 400k",
        "зп от 2000",
        "теперь удалёнка",
        "закрыли Java",
        "вышел кандидат на DevOps",
        "нужно поднять зарплату",  # «нужно» (сделать) — не «нужен» (человек)
        "а хотя нет 3000",
    ],
)
def test_ignores_updates_and_followups(text: str) -> None:
    assert _is_new_posting(text) is False


# --- связка regex + LLM (_decide_new_posting) ---


def _decide(text: str, verdict: PostingVerdict) -> bool:
    fake = AsyncMock(return_value=verdict)
    with patch.object(entity_resolution, "classify_new_posting", fake):
        return asyncio.run(_decide_new_posting(text, None))


def test_llm_confident_overrides_regex_miss() -> None:
    # Текст без regex-маркеров, но LLM уверенно говорит «новый найм» → True.
    assert _decide("в наш отдел требуется ещё одна пара рук",
                   PostingVerdict(new_posting=True, confidence=0.9)) is True


def test_llm_confident_overrides_regex_false_positive() -> None:
    # Regex срабатывает на «ищем», но LLM понимает, что это правка → False.
    assert _is_new_posting("по этой вакансии теперь ищем сеньора") is True
    assert _decide("по этой вакансии теперь ищем сеньора",
                   PostingVerdict(new_posting=False, confidence=0.9)) is False


def test_falls_back_to_regex_when_llm_unsure() -> None:
    low = PostingVerdict(new_posting=False, confidence=0.2)
    assert _decide("ищем системного администратора", low) is True   # regex hit
    assert _decide("подняли зп до 400k", low) is False              # regex miss


# --- парсинг ответа классификатора (classify_new_posting) ---


def test_classify_parses_valid_json() -> None:
    fake_chat = AsyncMock(return_value='{"new_posting": true, "confidence": 0.8}')

    async def _run():
        with patch.object(posting_classifier, "chat", new=fake_chat):
            return await classify_new_posting("ищем го", existing=None)

    verdict = asyncio.run(_run())
    assert verdict.new_posting is True
    assert verdict.confidence == 0.8


def test_classify_falls_back_on_garbage() -> None:
    # Битый ответ модели не должен ронять резолюцию — confidence 0.0 (решит regex).
    fake_chat = AsyncMock(return_value="не json")

    async def _run():
        with patch.object(posting_classifier, "chat", new=fake_chat):
            return await classify_new_posting("шум", existing=None)

    verdict = asyncio.run(_run())
    assert verdict.confidence == 0.0
    assert verdict.new_posting is False
