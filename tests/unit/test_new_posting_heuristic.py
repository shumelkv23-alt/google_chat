"""Юнит-тесты regex-эвристики is_new_posting (детектор объявления о найме).

Эвристика намеренно грубая и широкая: её задача — дать быстрый детерминированный
сигнал «похоже на найм», а точное решение принимает связка с LLM
(_decide_new_posting в entity_resolution). Поэтому тестируем контракт «ловит явные
маркеры найма / не реагирует на правки условий», а не отдельные слова в regex.

Функция живёт в чистом модуле posting_heuristic (зависит только от re), поэтому
тест не тянет БД/LLM и не требует .env — настоящий изолированный юнит.
"""

import pytest

from app.services.posting_heuristic import is_new_posting



HIRING_MESSAGES = [
    "ищем питониста в команду бэкенда",
    "нужен senior Python разработчик",
    "требуется DevOps-инженер",
    "набираем аналитиков в новый отдел",
    "приглашаем фронтендера в команду",
    "зовём senior Go разработчика",
    "хотим нанять тимлида",
    "открыли вакансию на позицию ML-инженера",
    "новая вакансия: продакт-менеджер",
    "We're hiring a backend engineer",
    "looking for a product designer",
    "join our team as a QA engineer",
]


NON_HIRING_MESSAGES = [
    "подняли зарплату до 400k",
    "теперь полностью удалёнка",
    "вышел кандидат на оффер",
    "спасибо за апдейт по проекту",
    "во сколько завтра созвон?",
]


@pytest.mark.parametrize("text", HIRING_MESSAGES)
def test_detects_hiring_announcements(text):
    assert is_new_posting(text) is True


@pytest.mark.parametrize("text", NON_HIRING_MESSAGES)
def test_ignores_non_hiring_messages(text):
    assert is_new_posting(text) is False


def test_is_case_insensitive():
    # regex скомпилирован с IGNORECASE — регистр не должен влиять
    assert is_new_posting("ИЩЕМ ПИТОНИСТА") is True
    assert is_new_posting("HIRING backend dev") is True


def test_empty_text_is_not_a_posting():
    # Граничный случай: пустая строка не должна считаться объявлением
    assert is_new_posting("") is False
