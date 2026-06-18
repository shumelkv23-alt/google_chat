"""Канонизация технологий из сырого LLM-вывода.

Чистый модуль без I/O — изолирован ради юнит-тестов (как posting_heuristic).
Без канонизации тренды по skills развалятся: «python / питон / py / python3» не
схлопнутся в один ключ, и топ технологий превратится в кашу из синонимов.
"""

# Алиас → канонический ключ. Ключи только в нижнем регистре.
_ALIASES: dict[str, str] = {
    # языки
    "питон": "python",
    "py": "python",
    "python3": "python",
    "джава": "java",
    "голанг": "go",
    "golang": "go",
    "js": "javascript",
    "джаваскрипт": "javascript",
    "ts": "typescript",
    "тайпскрипт": "typescript",
    "c#": "csharp",
    "си шарп": "csharp",
    "сишарп": "csharp",
    "c++": "cpp",
    "си++": "cpp",
    "плюсы": "cpp",
    "пхп": "php",
    "котлин": "kotlin",
    "раст": "rust",
    "руби": "ruby",
    # фреймворки / библиотеки
    "реакт": "react",
    "react.js": "react",
    "reactjs": "react",
    "вью": "vue",
    "vue.js": "vue",
    "vuejs": "vue",
    "node": "nodejs",
    "node.js": "nodejs",
    "джанго": "django",
    "фласк": "flask",
    "фастапи": "fastapi",
    "спринг": "spring",
    # БД / инфра
    "постгрес": "postgresql",
    "postgres": "postgresql",
    "psql": "postgresql",
    "pg": "postgresql",
    "мускул": "mysql",
    "монго": "mongodb",
    "mongo": "mongodb",
    "редис": "redis",
    "кликхаус": "clickhouse",
    "эластик": "elasticsearch",
    "докер": "docker",
    "кубер": "kubernetes",
    "кубернетес": "kubernetes",
    "к8с": "kubernetes",
    "k8s": "kubernetes",
}

# Слишком общие слова — не технологии. Выкидываем, чтобы не засоряли топ.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "программирование",
        "разработка",
        "кодинг",
        "coding",
        "development",
        "софт",
        "it",
        "ит",
        "computer science",
        "технологии",
    }
)


def normalize_skill(raw: str) -> str | None:
    """Привести одну технологию к каноническому ключу. None — мусор/стоп-слово."""
    if not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    if not key or key in _STOPWORDS:
        return None
    return _ALIASES.get(key, key)


def normalize_skills(raw: object) -> list[str]:
    """Список технологий → канонизированный, дедуплицированный (порядок сохранён).

    Принимает что угодно (LLM иногда отдаёт строку или null) — не-список даёт [].
    """
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        norm = normalize_skill(item)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out
