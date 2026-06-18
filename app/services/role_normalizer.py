"""Канонизация категории специальности.

Чистый модуль без I/O (как posting_heuristic). Сводит роль к фиксированному
набору категорий — это ось «каких спецов ищут» в кластерном анализе.
"""

# Канонический набор категорий специальностей.
ALLOWED: tuple[str, ...] = (
    "backend",
    "frontend",
    "fullstack",
    "mobile",
    "data",
    "ml",
    "devops",
    "qa",
    "design",
    "pm",
    "analyst",
    "security",
    "other",
)

_SYNONYMS: dict[str, str] = {
    "бэкенд": "backend",
    "бекенд": "backend",
    "back-end": "backend",
    "бэк": "backend",
    "серверная разработка": "backend",
    "фронтенд": "frontend",
    "фронт": "frontend",
    "front-end": "frontend",
    "вёрстка": "frontend",
    "верстка": "frontend",
    "фуллстек": "fullstack",
    "full-stack": "fullstack",
    "full stack": "fullstack",
    "мобайл": "mobile",
    "мобильный": "mobile",
    "android": "mobile",
    "ios": "mobile",
    "дата": "data",
    "data engineer": "data",
    "дата-инженер": "data",
    "инженер данных": "data",
    "млинженер": "ml",
    "ml engineer": "ml",
    "machine learning": "ml",
    "мл": "ml",
    "data scientist": "ml",
    "датасайентист": "ml",
    "девопс": "devops",
    "sre": "devops",
    "тестировщик": "qa",
    "тестирование": "qa",
    "тестер": "qa",
    "qa engineer": "qa",
    "дизайнер": "design",
    "ux": "design",
    "ui": "design",
    "ux/ui": "design",
    "проджект": "pm",
    "продакт": "pm",
    "project manager": "pm",
    "product manager": "pm",
    "менеджер": "pm",
    "аналитик": "analyst",
    "системный аналитик": "analyst",
    "бизнес-аналитик": "analyst",
    "безопасность": "security",
    "иб": "security",
    "appsec": "security",
}


def normalize_role(raw: object) -> str | None:
    """Привести роль к ALLOWED. None — неизвестное/пустое (не выдумываем категорию)."""
    if not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    if key in ALLOWED:
        return key
    return _SYNONYMS.get(key)
