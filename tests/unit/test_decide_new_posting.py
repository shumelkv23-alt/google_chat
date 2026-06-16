

from unittest.mock import AsyncMock

import pytest

from app.llm.posting_classifier import PostingVerdict
from app.services.entity_resolution import _POSTING_CONFIDENCE_MIN, _decide_new_posting

pytestmark = pytest.mark.anyio

# Тексты с известным вердиктом regex — чтобы видеть, когда сработал он, а когда LLM.
_HIRING_TEXT = "ищем питониста"       # regex → True
_NON_HIRING_TEXT = "подняли зарплату"  # regex → False


def _verdict_mock(new_posting: bool, confidence: float) -> AsyncMock:
    return AsyncMock(
        return_value=PostingVerdict(new_posting=new_posting, confidence=confidence)
    )


async def test_trusts_confident_llm_over_regex_true(monkeypatch):
    # Текст regex'ом НЕ ловится, но уверенный LLM говорит «новый найм» → True
    monkeypatch.setattr(
        "app.services.entity_resolution.classify_new_posting",
        _verdict_mock(new_posting=True, confidence=0.9),
    )

    assert await _decide_new_posting(_NON_HIRING_TEXT, None) is True


async def test_trusts_confident_llm_over_regex_false(monkeypatch):
    # Текст regex ловит как найм, но уверенный LLM говорит «правка» → False
    monkeypatch.setattr(
        "app.services.entity_resolution.classify_new_posting",
        _verdict_mock(new_posting=False, confidence=0.9),
    )

    assert await _decide_new_posting(_HIRING_TEXT, None) is False


async def test_falls_back_to_regex_when_llm_unsure_hiring(monkeypatch):
    # LLM не уверен (0.3 < порога) → решает regex; текст похож на найм → True
    monkeypatch.setattr(
        "app.services.entity_resolution.classify_new_posting",
        _verdict_mock(new_posting=False, confidence=0.3),
    )

    assert await _decide_new_posting(_HIRING_TEXT, None) is True


async def test_falls_back_to_regex_when_llm_unsure_non_hiring(monkeypatch):
    # LLM не уверен → решает regex; текст не про найм → False
    monkeypatch.setattr(
        "app.services.entity_resolution.classify_new_posting",
        _verdict_mock(new_posting=True, confidence=0.3),
    )

    assert await _decide_new_posting(_NON_HIRING_TEXT, None) is False


async def test_confidence_exactly_at_threshold_trusts_llm(monkeypatch):
    # Граница: confidence == порог → доверяем LLM (условие >=), а не regex
    monkeypatch.setattr(
        "app.services.entity_resolution.classify_new_posting",
        _verdict_mock(new_posting=True, confidence=_POSTING_CONFIDENCE_MIN),
    )

    assert await _decide_new_posting(_NON_HIRING_TEXT, None) is True
