"""Канонизация названия компании-работодателя.

Чистый модуль без I/O (как posting_heuristic). Полная дедупликация компаний —
отдельная большая задача; здесь минимум: срезать юр.форму и кавычки, схлопнуть
известные алиасы. Этого хватает, чтобы разрез «зп по компаниям» не двоился на
«ООО Яндекс» / «Яндекс» / «yandex».
"""

import re

# Юридические формы в начале названия — срезаем.
_LEGAL_FORMS = ("ооо", "оао", "зао", "пао", "ао", "ип", "llc", "inc", "ltd", "gmbh", "corp")
_LEGAL_PREFIX = re.compile(rf"^({'|'.join(_LEGAL_FORMS)})\.?\s+", re.IGNORECASE)
# Голая юр.форма без названия — не компания, а мусор.
_LEGAL_ONLY = frozenset(_LEGAL_FORMS)

# Нормализованный (lower) вариант → каноническое отображаемое имя.
_ALIASES: dict[str, str] = {
    "яндекс": "Yandex",
    "yandex": "Yandex",
    "сбер": "Sber",
    "сбербанк": "Sber",
    "sber": "Sber",
    "тинькофф": "T-Bank",
    "тбанк": "T-Bank",
    "tinkoff": "T-Bank",
    "вк": "VK",
    "vk": "VK",
    "озон": "Ozon",
    "ozon": "Ozon",
    "авито": "Avito",
    "avito": "Avito",
}


def normalize_company(raw: object) -> str | None:
    """Очистить и схлопнуть имя компании. None — пусто/мусор."""
    if not isinstance(raw, str):
        return None
    name = _LEGAL_PREFIX.sub("", raw.strip())
    name = name.strip().strip('"«»“”').strip()
    if not name or name.lower() in _LEGAL_ONLY:
        return None
    return _ALIASES.get(name.lower(), name)
