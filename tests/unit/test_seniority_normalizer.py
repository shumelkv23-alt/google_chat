"""Юнит-тесты канонизации грейда. Чистый модуль — без БД/LLM/.env."""

import pytest

from app.services.seniority_normalizer import ALLOWED, normalize_seniority


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("джун", "junior"),
        ("Junior", "junior"),
        ("младший", "junior"),
        ("мидл", "middle"),
        ("MIDDLE", "middle"),
        ("сеньор", "senior"),
        ("синьор", "senior"),
        ("Senior ", "senior"),
        ("тимлид", "lead"),
        ("стажёр", "intern"),
    ],
)
def test_normalize_seniority_canonicalizes(raw, expected):
    assert normalize_seniority(raw) == expected


def test_canonical_values_pass_through():
    for value in ALLOWED:
        assert normalize_seniority(value) == value


@pytest.mark.parametrize("bad", ["непонятно", "", None, 42, "архитектор"])
def test_normalize_seniority_unknown_is_none(bad):
    assert normalize_seniority(bad) is None
