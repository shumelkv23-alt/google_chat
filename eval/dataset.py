"""Усложнённый размеченный датасет для сравнения моделей и режимов.

25 сообщений, 12 ожидаемых вакансий, 3 мусорных. Ловушки специально путают
модель:

- 4 близкие «питон/ML/данные» роли (Backend Python, Python ML-инженер, Python
  дата-инженер, Data Scientist) — НЕ должны схлопываться;
- 2 «аналитика» (данных vs системный) — закрыть надо именно системного;
- почти-дубль: «ещё один бэкендер в другую команду» = НОВЫЙ найм, не апдейт P1;
- одна роль обновляется и по треду, и по цитате (структурные якоря);
- именованные follow-up'ы, которые надо привязать к ПРАВИЛЬНОЙ из похожих
  (ML≠Backend≠дата-инженер≠DS; фронт; дата-инженер);
- мусор вперемешку.

Эталон строится по ИСТОЧНИКАМ (какие message_id ведут к какой вакансии) — это
устойчивее названия, которое модель переврёт. Скоринг см. eval/scorer.py.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvalMessage:
    message_id: str
    text: str
    thread_id: str | None = None
    quoted_message_id: str | None = None
    event_type: str = "created"
    author: str = "Иван"


@dataclass(frozen=True)
class ExpectedVacancy:
    key: str
    status: str  # open | closed
    sources: tuple[str, ...]  # message_id, которые должны вести к этой вакансии
    # salary_min/salary_max (== int); location_contains/additional_contains
    # (все подстроки должны встретиться, lower).
    fields: dict = field(default_factory=dict)


MESSAGES: list[EvalMessage] = [
    EvalMessage("m01", "Открыли вакансию Backend-разработчик (Python), вилка 200–300k", thread_id="TP1"),
    EvalMessage("m02", "Ищем Python ML-инженера, удалёнка"),
    EvalMessage("m03", "Открываем позицию аналитика данных", thread_id="TA1"),
    EvalMessage("m04", "по бэкенду подняли верх до 350", thread_id="TP1"),  # тред → P1
    EvalMessage("m05", "Нужен DevOps инженер, дежурства, гибрид"),
    EvalMessage("m06", "Открыли Python дата-инженера в команду аналитики, от 250k"),
    EvalMessage("m07", "Ищем системного аналитика (не путать с аналитиком данных), офис Москва"),
    EvalMessage("m08", "сюда нужен SQL и Tableau", thread_id="TA1"),  # тред → A1
    EvalMessage("m09", "поднимаем вилку до 380", quoted_message_id="m01"),  # цитата → P1
    EvalMessage("m10", "по девопсу добавьте: дежурства 24/7, грейд middle+"),
    EvalMessage("m11", "Открыли Go-разработчика. Английский B2, ДМС, релокация, senior"),
    EvalMessage("m12", "по ML-инженеру на питоне подняли до 320"),  # именованный → P2
    EvalMessage("m13", "Ищем Frontend-разработчика (React)", thread_id="TF"),
    EvalMessage("m14", "вилка до 280k", thread_id="TF"),  # тред → F1
    EvalMessage("m15", "закрыли системного аналитика, нашли человека"),  # close → A2
    EvalMessage("m16", "Всем привет, кто идёт на обед?"),  # мусор
    EvalMessage("m17", "Открываем Data Scientist (не ML-инженер), нужен опыт NLP"),
    EvalMessage("m18", "по аналитику данных подняли до 240k"),  # именованный → A1
    EvalMessage("m19", "Напоминаю про дейли в 11:00"),  # мусор
    EvalMessage("m20", "Нужен ещё один Backend на Python, отдельная команда биллинга, до 330k"),  # новый найм ≠ P1
    EvalMessage("m21", "Кто-нибудь видел мой зарядник?"),  # мусор
    EvalMessage("m22", "Открыли QA-инженера (ручное + авто тестирование)"),
    EvalMessage("m23", "по фронту React добавьте: нужен TypeScript и опыт с Next.js"),  # именованный → F1
    EvalMessage("m24", "Ищем проджект-менеджера в продуктовую команду"),
    EvalMessage("m25", "по дата-инженеру на питоне уточнили: Spark и Airflow обязательны"),  # именованный → P3
]


# fields: salary_min/max (==int), location_contains/additional_contains (подстроки,
# lower), seniority/role_category/company (точное совпадение, lower), skills (set →
# F1). Размечены только однозначно вытекающие из текста значения — спорные опущены.
EXPECTED: list[ExpectedVacancy] = [
    ExpectedVacancy("backend_py", "open", ("m01", "m04", "m09"),
                    {"salary_min": 200000, "salary_max": 380000,
                     "role_category": "backend", "skills": ["python"]}),
    ExpectedVacancy("python_ml", "open", ("m02", "m12"),
                    {"salary_max": 320000, "location_contains": ["удал"],
                     "role_category": "ml", "skills": ["python"]}),
    ExpectedVacancy("python_dataeng", "open", ("m06", "m25"),
                    {"salary_min": 250000, "additional_contains": ["spark", "airflow"],
                     "role_category": "data", "skills": ["python"]}),
    ExpectedVacancy("analyst_data", "open", ("m03", "m08", "m18"),
                    {"salary_max": 240000, "additional_contains": ["sql", "tableau"],
                     "role_category": "analyst"}),
    ExpectedVacancy("sys_analyst", "closed", ("m07", "m15"),
                    {"location_contains": ["моск"], "role_category": "analyst"}),
    ExpectedVacancy("devops", "open", ("m05", "m10"),
                    {"additional_contains": ["дежур"], "role_category": "devops"}),
    ExpectedVacancy("go", "open", ("m11",),
                    {"additional_contains": ["b2", "дмс", "релока"],
                     "seniority": "senior", "skills": ["go"]}),
    ExpectedVacancy("frontend", "open", ("m13", "m14", "m23"),
                    {"salary_max": 280000, "additional_contains": ["typescript", "next"],
                     "role_category": "frontend", "skills": ["react", "typescript"]}),
    ExpectedVacancy("data_scientist", "open", ("m17",),
                    {"additional_contains": ["nlp"], "role_category": "ml"}),
    ExpectedVacancy("backend_py2", "open", ("m20",),
                    {"salary_max": 330000, "role_category": "backend",
                     "skills": ["python"]}),
    ExpectedVacancy("qa", "open", ("m22",), {"role_category": "qa"}),
    ExpectedVacancy("pm", "open", ("m24",), {"role_category": "pm"}),
]

# Мусор: не должен быть источником ни одной вакансии.
EXPECTED_NONE: tuple[str, ...] = ("m16", "m19", "m21")
