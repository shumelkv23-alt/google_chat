"""Юнит-тесты канонизации технологий.

Чистый модуль (только dict/str) — без БД/LLM/.env. Без этой канонизации тренды
по skills развалятся на синонимы, поэтому контракт фиксируем жёстко.
"""

import pytest

from app.services.skill_normalizer import normalize_skill, normalize_skills


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Питон", "python"),
        ("py", "python"),
        ("PYTHON", "python"),
        ("python3", "python"),
        ("  Docker ", "docker"),
        ("postgres", "postgresql"),
        ("k8s", "kubernetes"),
        ("react.js", "react"),
        ("node", "nodejs"),
        ("Go", "go"),
        ("неизвестная-тулза", "неизвестная-тулза"),  # незнакомое — как есть (lower)
    ],
)
def test_normalize_skill_canonicalizes(raw, expected):
    assert normalize_skill(raw) == expected


@pytest.mark.parametrize("junk", ["", "  ", "программирование", "разработка", "IT"])
def test_normalize_skill_drops_junk(junk):
    assert normalize_skill(junk) is None


def test_normalize_skills_dedups_preserving_order():
    raw = ["Питон", "py", "PYTHON", "Docker", "docker"]
    assert normalize_skills(raw) == ["python", "docker"]


def test_normalize_skills_filters_junk_and_nonstrings():
    raw = ["python", "разработка", "", None, 123, "Docker"]
    assert normalize_skills(raw) == ["python", "docker"]


@pytest.mark.parametrize("bad", [None, "python", 42, {"a": 1}])
def test_normalize_skills_nonlist_returns_empty(bad):
    assert normalize_skills(bad) == []
