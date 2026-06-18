"""Юнит-тесты канонизации названия компании. Чистый модуль — без БД/LLM."""

import pytest

from app.services.company_normalizer import normalize_company


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ООО Яндекс", "Yandex"),
        ("яндекс", "Yandex"),
        ("Yandex", "Yandex"),
        ('ООО "Сбербанк"', "Sber"),
        ("Тинькофф", "T-Bank"),
        ("  VK  ", "VK"),
        ("«Авито»", "Avito"),
        ("LLC Acme", "Acme"),  # незнакомая — чистим юр.форму, имя сохраняем
        ("Рога и Копыта", "Рога и Копыта"),
    ],
)
def test_normalize_company_canonicalizes(raw, expected):
    assert normalize_company(raw) == expected


@pytest.mark.parametrize("bad", ["", "   ", '""', "ООО ", None, 123])
def test_normalize_company_empty_is_none(bad):
    assert normalize_company(bad) is None
