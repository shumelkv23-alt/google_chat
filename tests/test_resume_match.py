"""Тесты подбора вакансий под резюме (resume_match + llm/resume_matcher).

БД и LLM мокаем — проверяем детект префикса, форматирование, ранжирование и
устойчивость к сбоям/галлюцинациям модели.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from app.llm.resume_matcher import MatchRanking, VacancyMatch, rank_matches
from app.services import resume_match

_CANDIDATES = [
    {
        "id": "v1",
        "title": "Python разработчик",
        "status": "open",
        "salary_min": 200000,
        "salary_max": 300000,
        "currency": "RUB",
        "team": "Backend",
        "owner_name": "Кирилл",
        "description": "FastAPI, PostgreSQL",
    },
    {
        "id": "v2",
        "title": "Go разработчик",
        "status": "open",
        "salary_min": None,
        "salary_max": None,
        "currency": "RUB",
        "team": "Platform",
        "owner_name": None,
        "description": "Go, k8s",
    },
]


# --- детект префикса -------------------------------------------------------


def test_detect_resume_text_with_prefix() -> None:
    assert resume_match.detect_resume_text("Резюме: Python, FastAPI") == "Python, FastAPI"


def test_detect_resume_text_without_prefix() -> None:
    assert resume_match.detect_resume_text("сколько вакансий открыто") is None


def test_detect_resume_text_prefix_only_is_none() -> None:
    assert resume_match.detect_resume_text("резюме") is None


# --- match_resume ----------------------------------------------------------


def test_match_resume_formats_ranking() -> None:
    ranking = MatchRanking(
        matches=[
            VacancyMatch(vacancy_id="v1", score=88, reason="Сильный матч по FastAPI."),
            VacancyMatch(vacancy_id="v2", score=20, reason="Нет опыта Go."),
        ]
    )
    with patch.object(
        resume_match, "_search_candidates", new=AsyncMock(return_value=_CANDIDATES)
    ), patch.object(resume_match, "rank_matches", new=AsyncMock(return_value=ranking)):
        payload, turn = asyncio.run(resume_match.match_resume("Python dev, 5 лет"))

    txt = payload["text"]
    assert "1. Python разработчик — 88% совпадение" in txt
    assert "200000–300000 RUB, команда Backend" in txt
    assert "Почему: Сильный матч по FastAPI." in txt
    assert turn == txt  # turn_text == текст ответа


def test_match_resume_no_candidates() -> None:
    with patch.object(
        resume_match, "_search_candidates", new=AsyncMock(return_value=[])
    ):
        payload, _ = asyncio.run(resume_match.match_resume("Python dev"))
    assert "не нашёл" in payload["text"]


def test_match_resume_handles_search_error() -> None:
    with patch.object(
        resume_match,
        "_search_candidates",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        payload, _ = asyncio.run(resume_match.match_resume("Python dev"))
    assert "не получилось обработать" in payload["text"]


def test_match_resume_drops_hallucinated_ids() -> None:
    # LLM вернул несуществующий vacancy_id — в ответ он попасть не должен.
    ranking = MatchRanking(
        matches=[VacancyMatch(vacancy_id="ghost", score=99, reason="x")]
    )
    with patch.object(
        resume_match, "_search_candidates", new=AsyncMock(return_value=_CANDIDATES)
    ), patch.object(resume_match, "rank_matches", new=AsyncMock(return_value=ranking)):
        payload, _ = asyncio.run(resume_match.match_resume("Python dev"))
    assert "ничего толком не подходит" in payload["text"]


# --- llm/resume_matcher: парсинг ответа модели ----------------------------


def test_rank_matches_parses_json() -> None:
    raw = '{"matches":[{"vacancy_id":"v1","score":80,"reason":"ok"}]}'
    with patch("app.llm.resume_matcher.chat", new=AsyncMock(return_value=raw)):
        ranking = asyncio.run(rank_matches("резюме", _CANDIDATES))
    assert len(ranking.matches) == 1
    assert ranking.matches[0].vacancy_id == "v1"
    assert ranking.matches[0].score == 80


def test_rank_matches_bad_json_returns_empty() -> None:
    with patch("app.llm.resume_matcher.chat", new=AsyncMock(return_value="не json")):
        ranking = asyncio.run(rank_matches("резюме", _CANDIDATES))
    assert ranking.matches == []
