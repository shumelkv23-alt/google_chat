"""Batch-экстрактор: разбор ПАЧКИ сообщений одним большим LLM-контекстом.

В отличие от per-message extractor'а ([extractor.py](extractor.py)), который видит
одно сообщение + узкое окно, сюда подаётся вся пачка сразу. LLM видит связи
между сообщениями (follow-up'ы, цитаты, треды) в одном контексте и сама
распределяет действия по вакансиям — это и есть гипотеза о росте точности.

Вход: список сообщений пачки (хронологически) с уже подготовленной разметкой
тредов/цитат (готовит batch_processor) + открытые вакансии из БД.
Выход: список BatchItem — по одному на каждое действие, с message_index
сообщения-источника. Сообщения не про вакансии в вывод не попадают.
"""

import json

from pydantic import Field

from app.config import settings
from app.llm.client import chat
from app.llm.extractor import ExtractionResult, _format_open_vacancies
from app.logger import logger


class BatchItem(ExtractionResult):
    """Действие по вакансии + индекс сообщения-источника в пачке.

    Наследует ExtractionResult (action/entity_ref/fields/confidence) вместе с
    нормализацией salary_* — добавляет лишь привязку к сообщению пачки.
    """

    message_index: int = Field(..., description="индекс сообщения в пачке")


_SYSTEM = """\
Ты — парсер сообщений о вакансиях в корпоративном чате. Тебе дают ПАЧКУ
сообщений сразу (по порядку, с индексами) и список открытых вакансий из БД.
Разбери всю пачку в едином контексте и выдай список действий по вакансиям.

Отвечай ТОЛЬКО валидным JSON-объектом вида:
{"items": [
  {"message_index": <int>, "action": "...", "entity_ref": "...",
   "fields": {...}, "confidence": <float>}
]}

action: create | update | close | none
- create: объявление НОВОЙ позиции, которой нет среди открытых вакансий
- update: уточнение условий/статуса существующей позиции (зарплата, команда,
  требования, овнер; "по X подняли до 350k", "теперь удалёнка")
- close: закрытие позиции ("закрыли X", "вышел кандидат на Y")
- none: сообщение НЕ про вакансии — такие просто НЕ включай в items

ГЛАВНОЕ: ты видишь всю пачку разом — пользуйся этим:
- follow-up без названия ("подняли до 350k", "теперь 2000", "на удалёнку")
  относи к вакансии, обсуждаемой ВЫШЕ в пачке. Сигналы привязки по силе:
  цитата [↳ ответ на N] > один тред > смысл/соседство. Никогда не привязывай
  к самой похожей по зарплате — только по контексту пачки.
- несколько сообщений про одну вакансию → несколько items с разными
  message_index, но одинаковым entity_ref.
- сверяйся с открытыми вакансиями: если сообщение относится к уже открытой
  позиции — это update/close, НЕ create.
- сомневаешься между create и update → ставь update.

fields: title, salary_min, salary_max, currency, status, owner, team, description.
Поле не упомянуто — НЕ включай его (не выдумывай). salary_* — числами.
entity_ref — короткая идентификационная фраза (title вакансии).
message_index — индекс сообщения из пачки, к которому относится действие.
confidence: 0.0–1.0. Если action != "none", confidence должен быть > 0.3.\
"""

# Битый/пустой ответ модели — пробуем ещё раз перед тем, как вернуть пустой
# список (вся пачка останется на следующий заход, не потеряется).
_MAX_ATTEMPTS = 2


def _format_batch_messages(messages: list[dict]) -> str:
    """Пронумерованный список сообщений пачки с пометками тредов и цитат."""
    lines = ["Сообщения пачки (по порядку; в квадратных скобках — индекс):"]
    for m in messages:
        author = m.get("author_name") or "unknown"
        marks: list[str] = []
        if m.get("thread_label"):
            marks.append(f"тред {m['thread_label']}")
        if m.get("reply_to_index") is not None:
            marks.append(f"↳ ответ на [{m['reply_to_index']}]")
        elif m.get("reply_to_text"):
            marks.append(f'↳ ответ на: "{m["reply_to_text"][:80]}"')
        mark = f" ({'; '.join(marks)})" if marks else ""
        lines.append(f"[{m['message_index']}] {author}{mark}: {m['text']}")
    return "\n".join(lines)


async def extract_batch(
    messages: list[dict],
    open_vacancies: list[dict] | None = None,
) -> list[BatchItem]:
    """Разобрать пачку сообщений. Возвращает список действий по вакансиям.

    messages — список dict с полями message_index, author_name, text и
    подготовленной разметкой: thread_label, reply_to_index | reply_to_text.
    """
    if not messages:
        return []

    blocks: list[str] = []
    if open_vacancies:
        blocks.append(_format_open_vacancies(open_vacancies))
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
        return results

    return []
