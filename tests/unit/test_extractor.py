"""Юнит-тесты нормализации зарплаты в extractor.

_coerce_int приводит к int зарплату, которую LLM отдаёт как попало (строкой,
числом, с суффиксом «к/тыс», с текстом вокруг). ExtractionResult._normalize_salary
применяет это на границе модели: salary_min/salary_max → int, неразборное —
выкидывается, чтобы не уронить запись в БД.

Чистая логика: вход → выход, без БД/сети. Модуль тянет app.config при импорте,
но реальный .env не нужен — conftest подкладывает безопасные заглушки.
"""

import pytest

from app.llm.extractor import ExtractionResult, _coerce_int


# Значения, из которых число вытащить можно: вход → ожидаемый int.
@pytest.mark.parametrize(
    "value, expected",
    [
        # числа как есть
        (2000, 2000),
        (2000.0, 2000),
        (1999.9, 1999),  # float усекается, не округляется
        # строки-числа
        ("2000", 2000),
        ("2,000", 2000),  # запятая-разделитель выкидывается
        ("150 000", 150000),  # пробелы внутри числа
        # суффикс тысяч
        ("300к", 300000),  # кириллическая к
        ("300k", 300000),  # латинская k
        ("300 тыс", 300000),
        # число с текстом вокруг — берётся первое число
        ("зп от 2000", 2000),
        ("150 000 руб", 150000),
    ],
)
def test_coerce_int_extracts_number(value, expected):
    assert _coerce_int(value) == expected


# Значения, из которых число вытащить нельзя → None.
@pytest.mark.parametrize(
    "value",
    [
        True,   # bool — подкласс int, но это не зарплата
        False,
        None,
        "abc",
        "",
        "договорная",
        [],
        {},
    ],
)
def test_coerce_int_returns_none_for_unparseable(value):
    assert _coerce_int(value) is None


def test_normalize_salary_converts_string_to_int():
    # Валидатор модели должен прогнать salary_* через _coerce_int
    result = ExtractionResult(
        action="update", fields={"salary_min": "300к", "title": "Backend"}
    )

    assert result.fields["salary_min"] == 300000
    assert result.fields["title"] == "Backend"  # не-salary поля не трогаем


def test_normalize_salary_drops_unparseable_value():
    result = ExtractionResult(action="update", fields={"salary_max": "договорная"})

    assert "salary_max" not in result.fields


def test_normalize_salary_leaves_non_salary_fields_untouched():
    fields = {"title": "ML Engineer", "team": "Research", "currency": "USD"}

    result = ExtractionResult(action="create", fields=fields)

    assert result.fields == fields


def test_normalize_salary_does_not_mutate_input_dict():
    # Иммутабельность: исходный dict вызывающего не должен меняться
    original = {"salary_min": "300к"}

    ExtractionResult(action="update", fields=original)

    assert original == {"salary_min": "300к"}
