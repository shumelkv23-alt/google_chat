"""Batch-экстрактор: разбор ПАЧКИ сообщений одним большим LLM-контекстом.

В отличие от per-message extractor'а ([extractor.py](extractor.py)), который видит
одно сообщение + узкое окно, сюда подаётся вся пачка сразу. LLM видит связи
между сообщениями (follow-up'ы, цитаты, треды) в одном контексте и сама
распределяет действия по вакансиям — это и есть гипотеза о росте точности.

Контракт v2 (batch_mode_v2.md, этапы 4–6):
- вердикт обязателен по КАЖДОМУ индексу пачки, action="none" — явно (B9:
  «модель забыла» и «модель решила, что не про вакансии» различимы);
- связи, найденные моделью, доходят до БД напрямую через link_to_index
  (сообщение той же пачки) и link_to_vacancy_ref (метка vN вакансии) — B8;
- read-only история [hN] с привязками — контекст через границу пачек (B7);
- при битом JSON ретрай показывает модели её ошибку (самопочинка парсинга).

Вход: список сообщений пачки (хронологически) с уже подготовленной разметкой
тредов/цитат (готовит batch_processor) + открытые вакансии (с метками vN)
+ хвост обработанной истории (с метками hN).
Выход: список BatchItem, провалидированный _validate_coverage (дубли и
индексы вне диапазона отброшены). Недостающие индексы вычисляет процессор.
"""

import json

from pydantic import Field

from app.config import settings
from app.llm.client import chat
from app.llm.extractor import ExtractionResult
from app.logger import logger


class BatchItem(ExtractionResult):
    """Действие по вакансии + привязки к контексту пачки.

    Наследует ExtractionResult (action/entity_ref/fields/confidence) вместе с
    нормализацией salary_*. Добавляет привязку к сообщению пачки и структурные
    ссылки, найденные моделью (см. правила приоритета в _SYSTEM).
    """

    message_index: int = Field(..., description="индекс сообщения в пачке")
    # «Та же сущность, что у сообщения N этой пачки» — резолв через карту
    # index→vacancy без эмбеддинг-поиска.
    link_to_index: int | None = None
    # Метка vN вакансии из списка открытых / истории — прямой lookup.
    link_to_vacancy_ref: str | None = None


_SYSTEM = """\
Ты — парсер сообщений о вакансиях в корпоративном чате. Тебе дают ПАЧКУ
сообщений сразу (по порядку, с индексами), список открытых вакансий из БД
(с метками v1, v2, …) и недавнюю обработанную историю чата (с метками h0, h1, …).
Разбери всю пачку в едином контексте и выдай вердикт по КАЖДОМУ сообщению.

Отвечай ТОЛЬКО валидным JSON-объектом вида:
{"items": [
  {"message_index": <int>, "action": "...", "entity_ref": "...",
   "fields": {...}, "confidence": <float>,
   "link_to_index": <int|null>, "link_to_vacancy_ref": "<vN|null>"}
]}

ОБЯЗАТЕЛЬНО: верни РОВНО ОДИН объект на КАЖДЫЙ индекс из секции «Новые
сообщения», включая сообщения не о вакансиях — для них action = "none".
Пропуск индекса считается ошибкой. Сообщения [hN] из истории — ТОЛЬКО контекст:
items по ним возвращать НЕЛЬЗЯ.

action: create | update | close | none
- create: объявление НОВОЙ позиции, которой нет среди открытых вакансий
- update: уточнение условий/статуса существующей позиции (зарплата, команда,
  требования, овнер; "по X подняли до 350k", "теперь удалёнка")
- close: закрытие позиции ("закрыли X", "вышел кандидат на Y")
- none: сообщение НЕ про вакансии

Привязка (заполняй link_to_index / link_to_vacancy_ref, приоритет по убыванию):
1. цитата [↳ ответ на N] → link_to_index = N
2. цитата [↳ ответ на hN] → link_to_vacancy_ref вакансии из строки [hN]
3. общий тред (одна метка «тред A») → link_to_index первого сообщения треда
4. смысловая связь внутри пачки (follow-up к сообщению выше) → link_to_index
5. упоминание вакансии из списка открытых или истории → link_to_vacancy_ref
6. ничего из перечисленного → оба поля null
ЗАПРЕЩЕНО привязывать по совпадению зарплаты или других чисел — только по
структуре (цитата/тред) и смыслу контекста.

Ещё правила:
- несколько сообщений про одну вакансию → несколько items с разными
  message_index, но одной привязкой (link_to_index на первое из них).
- сверяйся с открытыми вакансиями: если сообщение относится к уже открытой
  позиции — это update/close с link_to_vacancy_ref, НЕ create.
- сомневаешься между create и update → ставь update.

fields: title, salary_min, salary_max, currency, status, owner, team,
description, location, additional_info.
- description — ЧЕМ заниматься, суть роли (напр. «разработка биллинга на Go»).
  Если сути роли в сообщении нет — НЕ выдумывай description.
- location — где работать (удалёнка / город / офис / гибрид).
- additional_info — требования и условия: уровень языка, грейд, опыт, формат,
  бенефиты. Пример: сообщение «нужен английский B2, есть ДМС и релокация» →
  additional_info = "английский B2; ДМС; релокация" (НЕ в description!).
Поле не упомянуто — НЕ включай его (не выдумывай). salary_* — числами.
entity_ref — короткая идентификационная фраза (title вакансии).
confidence: 0.0–1.0. Если action != "none", confidence должен быть > 0.3.\
"""

# Битый/пустой ответ модели — пробуем ещё раз перед тем, как вернуть пустой
# список (вся пачка останется на следующий заход, не потеряется).
_MAX_ATTEMPTS = 2


def _format_open_vacancies(vacancies: list[dict]) -> str:
    """Открытые вакансии с метками vN — по ним LLM возвращает link_to_vacancy_ref."""
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
        lines.append(f"[{v['ref']}] {v['title']} [{status}]{salary}{team}{location}{marker}")
    return "\n".join(lines)


def _format_history(history: list[dict]) -> str:
    """Read-only хвост обработанной истории с привязками к вакансиям (B7)."""
    lines = [
        "Недавняя история (ТОЛЬКО контекст, items по этим сообщениям НЕ возвращать):"
    ]
    for h in history:
        author = h.get("author_name") or "unknown"
        when = h["created_at"].strftime("%d.%m %H:%M") if h.get("created_at") else ""
        binding = ""
        if h.get("vacancy_ref"):
            binding = f" → вакансия [{h['vacancy_ref']}] «{h.get('vacancy_title')}»"
        elif h.get("vacancy_title"):
            binding = f" → вакансия «{h['vacancy_title']}»"
        lines.append(f"[{h['label']}] {author} ({when}){binding}: {h['text']}")
    return "\n".join(lines)


def _format_batch_messages(messages: list[dict]) -> str:
    """Пронумерованный список сообщений пачки с пометками тредов и цитат."""
    lines = ["Новые сообщения (извлекать; в квадратных скобках — индекс):"]
    for m in messages:
        author = m.get("author_name") or "unknown"
        marks: list[str] = []
        if m.get("thread_label"):
            marks.append(f"тред {m['thread_label']}")
        if m.get("reply_to_index") is not None:
            marks.append(f"↳ ответ на [{m['reply_to_index']}]")
        elif m.get("reply_to_history"):
            marks.append(f"↳ ответ на [{m['reply_to_history']}]")
        elif m.get("reply_to_text"):
            marks.append(f'↳ ответ на: "{m["reply_to_text"][:80]}"')
        mark = f" ({'; '.join(marks)})" if marks else ""
        lines.append(f"[{m['message_index']}] {author}{mark}: {m['text']}")
    return "\n".join(lines)


def _validate_coverage(items: list[BatchItem], batch_len: int) -> list[BatchItem]:
    """Отбросить галлюцинации индексов и дубли (первый вердикт побеждает).

    Недостающие индексы НЕ чинит — их вычисляет процессор и оставляет pending
    на повторный флаш (B9: пропуск ≠ осознанный none).
    """
    seen: dict[int, BatchItem] = {}
    for it in items:
        if not (0 <= it.message_index < batch_len):
            logger.warning("batch_llm_index_out_of_range", index=it.message_index)
            continue
        if it.message_index in seen:
            logger.warning("batch_llm_duplicate_index", index=it.message_index)
            continue
        seen[it.message_index] = it
    return [seen[i] for i in sorted(seen)]


async def extract_batch(
    messages: list[dict],
    open_vacancies: list[dict] | None = None,
    history: list[dict] | None = None,
) -> list[BatchItem]:
    """Разобрать пачку сообщений. Возвращает провалидированный список вердиктов.

    messages — список dict с полями message_index, author_name, text и
    подготовленной разметкой: thread_label, reply_to_index | reply_to_history |
    reply_to_text. open_vacancies — с метками ref (vN); history — с метками
    label (hN) и привязками vacancy_ref/vacancy_title.
    Пустой список при невосстановимом сбое — пачка останется pending.
    """
    if not messages:
        return []

    blocks: list[str] = []
    if open_vacancies:
        blocks.append(_format_open_vacancies(open_vacancies))
    if history:
        blocks.append(_format_history(history))
    blocks.append(_format_batch_messages(messages))
    user_content = "\n\n".join(blocks)

    llm_messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_content},
    ]

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        raw = await chat(
            messages=llm_messages,
            model=settings.openrouter_model_extract,
            response_format={"type": "json_object"},
        )
        if not raw.strip():
            logger.warning("batch_extractor_empty_response", attempt=attempt)
            continue
        try:
            data = json.loads(raw.strip())
        except Exception as exc:
            logger.warning(
                "batch_extractor_parse_error",
                raw=raw[:500],
                error=str(exc),
                attempt=attempt,
            )
            # Самопочинка: показываем модели её битый ответ и просим исправить —
            # заметная доля ошибок парсинга чинится со второй попытки.
            llm_messages.append({"role": "assistant", "content": raw})
            llm_messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Ответ не распарсился: {exc}. Верни ТОЛЬКО валидный "
                        "JSON-объект по схеме, без markdown-ограждений и пояснений."
                    ),
                }
            )
            continue

        items_raw = data.get("items", []) if isinstance(data, dict) else []
        results: list[BatchItem] = []
        for item in items_raw:
            try:
                results.append(BatchItem(**item))
            except Exception as exc:
                # Один битый item не должен ронять всю пачку — пропускаем его.
                logger.warning(
                    "batch_extractor_item_invalid", item=item, error=str(exc)
                )
        return _validate_coverage(results, len(messages))

    return []
