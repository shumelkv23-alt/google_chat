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


# Статус из LLM → разрешённый набор (open/closed/on_hold/filled). Чужой статус
# иначе ронял бы вставку вакансии с CheckViolation.
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("open", "open"),
        ("OPEN", "open"),  # регистр не важен
        ("active", "open"),  # синоним → open (ловит найденный баг)
        ("открыта", "open"),
        ("closed", "closed"),
        ("закрыта", "closed"),
        ("on hold", "on_hold"),
        ("заполнена", "filled"),
    ],
)
def test_normalize_status_maps_to_allowed(raw, expected):
    result = ExtractionResult(action="create", fields={"status": raw})

    assert result.fields["status"] == expected


@pytest.mark.parametrize("raw", ["в работе", "frozen", 42, None])
def test_normalize_status_drops_unknown(raw):
    # Неизвестный/нестроковый статус выкидывается → сработает дефолт open.
    result = ExtractionResult(action="create", fields={"status": raw, "title": "X"})

    assert "status" not in result.fields
    assert result.fields["title"] == "X"  # остальные поля не трогаем


@pytest.mark.parametrize("raw, expected", [(None, ""), (123, ""), ("Backend", "Backend")])
def test_entity_ref_coerces_non_string_to_empty(raw, expected):
    # action=none часто приходит с entity_ref: null — это не должно ронять разбор.
    result = ExtractionResult(action="none", entity_ref=raw, confidence=0.0)

    assert result.entity_ref == expected


@pytest.mark.parametrize("raw", [None, "не словарь", 42])
def test_fields_coerces_non_dict_to_empty(raw):
    # Модель иногда отдаёт fields: null — item не должен падать на валидации.
    result = ExtractionResult(action="none", fields=raw, confidence=0.0)

    assert result.fields == {}


# --- Обогащающие поля: skills / seniority / role_category / company ---------
# LLM отдаёт их грязно (синонимы, дубли, регистр). Валидаторы канонизируют их на
# границе модели — иначе аналитика трендов/кластеров дробится на синонимы.


def test_normalize_skills_canonicalizes_and_dedups():
    result = ExtractionResult(
        action="create",
        fields={"skills": ["Питон", "py", "PYTHON", "Docker"]},
    )

    assert result.fields["skills"] == ["python", "docker"]


def test_normalize_skills_drops_when_empty_after_cleaning():
    # Пустой стек не пишем — иначе update затёр бы уже известные технологии.
    result = ExtractionResult(
        action="update", fields={"skills": ["разработка", ""], "title": "X"}
    )

    assert "skills" not in result.fields
    assert result.fields["title"] == "X"


@pytest.mark.parametrize(
    "field, raw, expected",
    [
        ("seniority", "джуниор", "junior"),
        ("seniority", "Senior", "senior"),
        ("role_category", "бэкенд", "backend"),
        ("role_category", "machine learning", "ml"),
        ("company", "ООО Яндекс", "Yandex"),
    ],
)
def test_normalize_categorical_canonicalizes(field, raw, expected):
    result = ExtractionResult(action="create", fields={field: raw})

    assert result.fields[field] == expected


@pytest.mark.parametrize(
    "field, raw",
    [
        ("seniority", "архитектор"),
        ("role_category", "непонятная роль"),
        ("company", "   "),
    ],
)
def test_normalize_categorical_drops_unknown(field, raw):
    # Неизвестное/пустое выкидываем — не пишем мусор в БД и не дробим разрезы.
    result = ExtractionResult(action="create", fields={field: raw, "title": "X"})

    assert field not in result.fields
    assert result.fields["title"] == "X"
