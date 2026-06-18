"""Юнит-тесты канонизации категории специальности. Чистый модуль — без БД/LLM."""

import pytest

from app.services.role_normalizer import ALLOWED, normalize_role


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("бэкенд", "backend"),
        ("back-end", "backend"),
        ("Frontend", "frontend"),
        ("фронт", "frontend"),
        ("фуллстек", "fullstack"),
        ("android", "mobile"),
        ("data engineer", "data"),
        ("machine learning", "ml"),
        ("девопс", "devops"),
        ("тестировщик", "qa"),
        ("ux/ui", "design"),
        ("product manager", "pm"),
        ("системный аналитик", "analyst"),
        ("безопасность", "security"),
    ],
)
def test_normalize_role_canonicalizes(raw, expected):
    assert normalize_role(raw) == expected


def test_canonical_values_pass_through():
    for value in ALLOWED:
        assert normalize_role(value) == value


@pytest.mark.parametrize("bad", ["непонятная роль", "", None, 7])
def test_normalize_role_unknown_is_none(bad):
    assert normalize_role(bad) is None
