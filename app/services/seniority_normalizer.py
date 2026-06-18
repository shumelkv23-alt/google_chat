"""Канонизация грейда вакансии.

Чистый модуль без I/O (как posting_heuristic). Приводит грейд к фиксированному
набору, чтобы кластеры «спец × зп × грейд» не дробились на синонимы
(«джун / junior / младший» — один уровень).
"""

# Канонический набор уровней (по возрастанию).
ALLOWED: tuple[str, ...] = ("intern", "junior", "middle", "senior", "lead")

_SYNONYMS: dict[str, str] = {
    "стажёр": "intern",
    "стажер": "intern",
    "trainee": "intern",
    "интерн": "intern",
    "джун": "junior",
    "джуниор": "junior",
    "младший": "junior",
    "jr": "junior",
    "мидл": "middle",
    "миддл": "middle",
    "mid": "middle",
    "средний": "middle",
    "сеньор": "senior",
    "синьор": "senior",
    "старший": "senior",
    "sr": "senior",
    "лид": "lead",
    "тимлид": "lead",
    "teamlead": "lead",
    "team lead": "lead",
    "ведущий": "lead",
    "head": "lead",
}


def normalize_seniority(raw: object) -> str | None:
    """Привести грейд к ALLOWED. None — неизвестное/пустое (не выдумываем уровень)."""
    if not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    if key in ALLOWED:
        return key
    return _SYNONYMS.get(key)
