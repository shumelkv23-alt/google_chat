import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.llm.client import chat
from app.logger import logger
from app.services.company_normalizer import normalize_company
from app.services.role_normalizer import normalize_role
from app.services.seniority_normalizer import normalize_seniority
from app.services.skill_normalizer import normalize_skills

_SYSTEM_BASE = """\
Ты — парсер сообщений о вакансиях в корпоративном чате.
Отвечай ТОЛЬКО валидным JSON без markdown-блоков.

Поля action: create | update | close | none
Поля fields: title, salary_min, salary_max, currency, status, owner, team,
description, location, additional_info, company, seniority, role_category, skills.
- description — суть роли: что за позиция, чем заниматься.
- location — где работать: "удалёнка", "Москва, офис", "гибрид, СПб".
- additional_info — полезные детали, не вошедшие в отдельные поля (грейд,
  уровень языка, формат работы, бенефиты). НЕ дублируй сюда description.
- company — компания-работодатель, если названа ("ищем в Авито" → "Авито").
- seniority — грейд, РОВНО ОДНО из: intern, junior, middle, senior, lead.
- role_category — категория роли, РОВНО ОДНО из: backend, frontend, fullstack,
  mobile, data, ml, devops, qa, design, pm, analyst, security, other.
- skills — МАССИВ конкретных технологий из требований ["python","docker"].
  Только инструменты/языки/фреймворки, НЕ общие слова ("разработка").
Если поле не упомянуто — НЕ включай его в fields (не выдумывай).

Пример: "Ищем senior python-разработчика в Авито, бэкенд, нужен postgres и docker, удалёнка"
{"action":"create","entity_ref":"Python-разработчик","confidence":0.9,
 "fields":{"title":"Python-разработчик","company":"Авито","seniority":"senior",
 "role_category":"backend","skills":["python","postgresql","docker"],"location":"удалёнка"}}\
"""

_SYSTEM_FULL = """\
Ты — парсер сообщений о вакансиях в корпоративном чате.
Отвечай ТОЛЬКО валидным JSON без markdown-блоков.

Тебе могут дать:
1. Список открытых вакансий из БД ("Сейчас открыты позиции") — что сейчас открыто
   в фирме. Используй его, чтобы сопоставить упоминание с существующей позицией.
2. Последние сообщения из чата ("Контекст") — окно недавних обсуждений,
   упорядочены по времени: чем ниже, тем свежее. Более свежие сообщения важнее.
3. Новое сообщение — то, что нужно распарсить.

Поля action (hint — финальное решение принимает другой модуль на основе БД):
- create: явное объявление НОВОЙ позиции, которой нет в списке открытых
  ("открыли вакансию X", "ищем Y" — и X/Y не совпадает с открытыми)
- update: уточнение условий или статуса позиции из открытых или контекста
  (зарплата, требования, команда, овнер; "по X подняли до 350k", "зп от 2000")
- close: явное закрытие позиции ("закрыли X", "вышел кандидат на Y")
- none: сообщение не про вакансии

ВАЖНО:
- Если новое сообщение упоминает или относится к одной из открытых вакансий —
  action = update или close (не create).
- Если в сообщении ЕСТЬ данные о позиции (зарплата, команда, требования) —
  action НЕ может быть "none".
- Для короткого follow-up без названия ("зп от 2000", "на удалёнку", "а хотя нет
  3000") определи позицию по Контексту: посмотри предыдущие реплики (особенно
  [самое свежее] и соседние) — если выше обсуждают конкретную вакансию (звучало
  её название), follow-up относится к НЕЙ. Контекст важнее любых других сигналов.
- Только если в Контексте нет явной позиции — бери вакансию, помеченную
  [последняя созданная] в списке открытых (последняя заведённая, к которой
  дописывают условия). Никогда не привязывай к самой похожей по зарплате/смыслу.
- Если в follow-up ЯВНО назван другой объект (другое название позиции/технология)
  — относи к нему.
- Сомневаешься между create и update → ставь update.

Поля entity_ref — короткая идентификационная фраза:
- create: название новой позиции из текущего сообщения
- update/close: title вакансии, к которой относится сообщение. Для безымянного
  follow-up — title позиции, обсуждаемой в Контексте; если её в Контексте нет —
  title вакансии [последняя созданная]. Предпочитай title из списка открытых.

Поля fields: title, salary_min, salary_max, currency, status, owner, team,
description, location, additional_info.
- description — ЧЕМ заниматься, суть роли. Нет сути роли — НЕ выдумывай.
- location — где работать: "удалёнка", "Москва, офис", "гибрид, СПб".
- additional_info — требования и условия: уровень языка, грейд, опыт, формат,
  бенефиты. Пример: "нужен английский B2, есть ДМС и релокация" →
  additional_info = "английский B2; ДМС; релокация" (НЕ в description!).
- company — компания-работодатель, если названа.
- seniority — грейд, РОВНО ОДНО из: intern, junior, middle, senior, lead.
- role_category — РОВНО ОДНО из: backend, frontend, fullstack, mobile, data, ml,
  devops, qa, design, pm, analyst, security, other.
- skills — МАССИВ конкретных технологий ["python","docker"]; не общие слова.
Если поле не упомянуто — НЕ включай его в fields.
confidence: 0.0–1.0 — насколько ты уверен в action и entity_ref.
Если action != "none", confidence должен быть > 0.3.\
"""


# Колонки salary_* — INTEGER, а LLM нередко отдаёт зарплату строкой ("2000",
# "300к", "150 000 руб"). Нормализуем на границе, пока данные не ушли в БД.
_INT_FIELDS = ("salary_min", "salary_max")
_NUMBER_RE = re.compile(r"(\d[\d\s.,]*)\s*(k|к|тыс)?", re.IGNORECASE)


def _coerce_int(value: Any) -> int | None:
    """Привести значение к int. Возвращает None, если число не вытащить."""
    if isinstance(value, bool):  # bool — подкласс int, но это не зарплата
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, str):
        return None
    match = _NUMBER_RE.search(value)
    if match is None:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    if not digits:
        return None
    result = int(digits)
    if match.group(2):  # суффикс тысяч: "300к" → 300000
        result *= 1000
    return result


# Колонка status — под check-constraint (open/closed/on_hold/filled). LLM же
# отдаёт синонимы ("active", "открыта", "on hold"). Без нормализации чужой статус
# роняет вставку вакансии с CheckViolation. Мапим известное, неизвестное —
# выкидываем (тогда сработает дефолт open / статус задаст action close).
_ALLOWED_STATUS = {"open", "closed", "on_hold", "filled"}
_STATUS_SYNONYMS = {
    "active": "open",
    "opened": "open",
    "открыта": "open",
    "открыт": "open",
    "открытая": "open",
    "открыто": "open",
    "close": "closed",
    "закрыта": "closed",
    "закрыт": "closed",
    "закрытая": "closed",
    "закрыто": "closed",
    "on hold": "on_hold",
    "hold": "on_hold",
    "paused": "on_hold",
    "на паузе": "on_hold",
    "приостановлена": "on_hold",
    "заполнена": "filled",
    "заполнен": "filled",
    "укомплектована": "filled",
}


class ExtractionResult(BaseModel):
    action: Literal["create", "update", "close", "none"]
    entity_ref: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0

    @field_validator("entity_ref", mode="before")
    @classmethod
    def _coerce_entity_ref(cls, value: Any) -> str:
        """Для action=none модель законно отдаёт entity_ref: null. Тип str это
        отверг бы → валидный item выкидывался бы как битый (а в batch — застревал
        бы pending и крутил лишние флаши). Любое не-строковое → пустая строка."""
        return value if isinstance(value, str) else ""

    @field_validator("fields", mode="before")
    @classmethod
    def _coerce_fields(cls, value: Any) -> dict:
        """Модель иногда отдаёт fields: null (особенно для action=none). Тип dict
        это отверг бы и ронял весь item — приводим не-словарь к пустому dict."""
        return value if isinstance(value, dict) else {}

    @field_validator("fields", mode="after")
    @classmethod
    def _normalize_salary(cls, fields: dict[str, Any]) -> dict[str, Any]:
        """LLM отдаёт зарплату как угодно (строкой/числом). Приводим salary_* к
        int; что не парсится — выкидываем, чтобы не ронять запись в БД."""
        cleaned = dict(fields)
        for key in _INT_FIELDS:
            if key not in cleaned:
                continue
            number = _coerce_int(cleaned[key])
            if number is None:
                cleaned.pop(key)
            else:
                cleaned[key] = number
        return cleaned

    @field_validator("fields", mode="after")
    @classmethod
    def _normalize_status(cls, fields: dict[str, Any]) -> dict[str, Any]:
        """Привести status к разрешённому набору; неизвестный — убрать."""
        if "status" not in fields:
            return fields
        cleaned = dict(fields)
        raw = cleaned["status"]
        norm = raw.strip().lower() if isinstance(raw, str) else None
        if norm in _ALLOWED_STATUS:
            cleaned["status"] = norm
        elif norm in _STATUS_SYNONYMS:
            cleaned["status"] = _STATUS_SYNONYMS[norm]
        else:
            cleaned.pop("status")
        return cleaned

    @field_validator("fields", mode="after")
    @classmethod
    def _normalize_skills_field(cls, fields: dict[str, Any]) -> dict[str, Any]:
        """skills: канонизировать стек. Пустой результат убираем — иначе update
        с пустым массивом затёр бы уже известный стек (семантика замены)."""
        if "skills" not in fields:
            return fields
        cleaned = dict(fields)
        skills = normalize_skills(cleaned["skills"])
        if skills:
            cleaned["skills"] = skills
        else:
            cleaned.pop("skills")
        return cleaned

    @field_validator("fields", mode="after")
    @classmethod
    def _normalize_categorical(cls, fields: dict[str, Any]) -> dict[str, Any]:
        """seniority/role_category/company → канон; неизвестное/пустое убираем,
        чтобы не писать в БД мусорные значения и не дробить разрезы аналитики."""
        cleaned = dict(fields)
        for key, normalize in (
            ("seniority", normalize_seniority),
            ("role_category", normalize_role),
            ("company", normalize_company),
        ):
            if key not in cleaned:
                continue
            norm = normalize(cleaned[key])
            if norm:
                cleaned[key] = norm
            else:
                cleaned.pop(key)
        return cleaned


# Модель изредка отдаёт пустой/битый ответ — один такой чих не должен ронять
# сообщение в action=none. Пробуем ещё раз перед тем, как сдаться.
_MAX_ATTEMPTS = 2


def _format_open_vacancies(vacancies: list[dict]) -> str:
    lines = ["Сейчас открыты позиции (из БД):"]
    for v in vacancies:
        salary = ""
        lo, hi = v.get("salary_min"), v.get("salary_max")
        if lo or hi:
            cur = v.get("currency") or "RUB"
            salary = f", {lo or '?'}–{hi or '?'} {cur}"
        team = f", команда: {v['team']}" if v.get("team") else ""
        location = f", локация: {v['location']}" if v.get("location") else ""
        status = v.get("status") or "open"
        marker = " [последняя созданная]" if v.get("_latest") else ""
        lines.append(f"- {v['title']} [{status}]{salary}{team}{location}{marker}")
    return "\n".join(lines)


def _format_context_messages(messages: list[dict]) -> str:
    # Сообщения идут хронологически (сверху старые, снизу свежие). Помечаем
    # последнее как самое свежее — это главный антецедент для follow-up'ов.
    lines = ["Контекст (последние сообщения чата, снизу — самые свежие):"]
    last = len(messages) - 1
    for i, m in enumerate(messages):
        marker = " [самое свежее]" if i == last else ""
        lines.append(f"- {m.get('author_name') or 'unknown'}: {m['text']}{marker}")
    return "\n".join(lines)


async def extract_vacancy(
    text: str,
    author_name: str | None,
    created_at: str,
    context_messages: list[dict] | None = None,
    open_vacancies: list[dict] | None = None,
) -> ExtractionResult:
    blocks: list[str] = []
    if open_vacancies:
        blocks.append(_format_open_vacancies(open_vacancies))
    if context_messages:
        blocks.append(_format_context_messages(context_messages))
    blocks.append(
        f'Новое сообщение от {author_name or "unknown"} ({created_at}):\n"{text}"'
    )

    system = _SYSTEM_FULL if (open_vacancies or context_messages) else _SYSTEM_BASE
    user_content = "\n\n".join(blocks)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        raw = await chat(
            messages=messages,
            model=settings.openrouter_model_extract,
            response_format={"type": "json_object"},
        )
        if not raw.strip():
            logger.warning("extractor_empty_response", attempt=attempt)
            continue
        try:
            data = json.loads(raw.strip())
            return ExtractionResult(**data)
        except Exception as exc:
            logger.warning(
                "extractor_parse_error", raw=raw[:500], error=str(exc), attempt=attempt
            )

    return ExtractionResult(action="none", confidence=0.0)
