"""Прогон датасета через настоящий пайплайн в выбранном режиме и на выбранной
модели. Реальная БД (chatbot_test), реальные эмбеддинги и LLM.

ВАЖНО: DATABASE_URL должен указывать на тестовую БД ДО импорта этого модуля —
он тянет app.db.session. Это обеспечивает eval/run_eval.py.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import app.llm.batch_extractor as _be
import app.llm.client as _client
import app.llm.extractor as _ex
import app.llm.posting_classifier as _pc
import app.llm.prefilter as _pf
import app.llm.resolver as _rs
import app.services.batch_processor as _bp
import app.services.edits as _ed_svc
import app.services.entity_resolution as _er
import app.services.extraction as _ex_svc
import app.services.ingest as _ig
from app.config import settings
from app.db.session import AsyncSessionLocal
from app.schemas.incoming import IncomingMessage
from app.services.batch_processor import flush_batch, route_created_batch
from app.services.edits import handle_delete, handle_edit, handle_edit_batch
from app.services.extraction import run_extraction
from app.services.ingest import persist_message

from eval.dataset import EvalMessage

SPACE = "spaces/EVAL"
_BASE = datetime(2026, 6, 16, 12, 0, tzinfo=timezone.utc)

_TABLES = (
    "vacancy_revisions",
    "chat_messages_embeddings",
    "vacancies",
    "chat_messages",
    "conversations",
)


# --------------------------------------------------------------------------
# Счётчик LLM-вызовов: оборачиваем настоящий chat во всех use-site модулях.
# --------------------------------------------------------------------------

_real_chat = _client.chat
_calls = {"n": 0}
_CHAT_MODULES = (_pf, _ex, _be, _rs, _pc)


async def _counting_chat(*args, **kwargs):
    _calls["n"] += 1
    return await _real_chat(*args, **kwargs)


def install_call_counter() -> None:
    for module in _CHAT_MODULES:
        module.chat = _counting_chat


# Эмбеддинги (OpenAI) на длинном прогоне ловят транзиентные ConnectTimeout —
# именно они убили pro в прошлой матрице. Оборачиваем ретраем с backoff, чтобы
# мерить качество модели, а не везение сети. На прод не влияет (eval-only).
_EMBED_MODULES = (_er, _ig, _ex_svc, _bp, _ed_svc)


def _with_retry(real, attempts: int, base_delay: float):
    async def wrapper(*args, **kwargs):
        last: Exception | None = None
        for i in range(attempts):
            try:
                return await real(*args, **kwargs)
            except Exception as exc:  # сетевые/таймаут — пробуем ещё
                last = exc
                await asyncio.sleep(base_delay * (i + 1))
        raise last  # noqa: вернёт последнюю ошибку, если все попытки легли
    return wrapper


def install_embedding_retry(attempts: int = 4, base_delay: float = 2.0) -> None:
    for module in _EMBED_MODULES:
        real = module._openai.embeddings.create
        module._openai.embeddings.create = _with_retry(real, attempts, base_delay)


def reset_calls() -> None:
    _calls["n"] = 0


def get_calls() -> int:
    return _calls["n"]


# --------------------------------------------------------------------------
# Конфиг модели и сброс БД
# --------------------------------------------------------------------------


def set_model(model: str) -> None:
    """Модели точности — extract/resolve/prefilter. answer/summarize вне пути."""
    settings.openrouter_model_extract = model
    settings.openrouter_model_resolve = model
    settings.openrouter_model_prefilter = model


async def reset_db() -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
        )
        await session.commit()


async def _pending() -> int:
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                text(
                    "SELECT count(*) FROM chat_messages WHERE space_id = :sp "
                    "AND process_status = 'pending' AND is_deleted = false"
                ),
                {"sp": SPACE},
            )
        ).scalar()


# --------------------------------------------------------------------------
# Replay
# --------------------------------------------------------------------------


def _incoming(msg: EvalMessage, created_at: datetime) -> IncomingMessage:
    return IncomingMessage(
        message_id=msg.message_id,
        space_id=SPACE,
        thread_id=msg.thread_id,
        author_id="users/eval",
        author_name=msg.author,
        text=msg.text,
        created_at=created_at,
        event_type=msg.event_type,
        quoted_message_id=msg.quoted_message_id,
    )


async def replay(messages: list[EvalMessage], mode: str) -> None:
    """Прогнать сообщения через пайплайн, повторяя роутинг pubsub для режима."""
    for i, msg in enumerate(messages):
        incoming = _incoming(msg, _BASE + timedelta(seconds=i * 30))
        if msg.event_type == "created":
            await persist_message(incoming)
            if mode == "batch":
                await route_created_batch(incoming)  # онлайн-ветка или дефер в пачку
            else:
                await run_extraction(incoming)
        elif msg.event_type == "updated":
            await (handle_edit_batch if mode == "batch" else handle_edit)(incoming)
        elif msg.event_type == "deleted":
            await handle_delete(incoming)

    if mode == "batch":
        await _drain()


async def _drain() -> None:
    """Догнать накопленные пачки без оглядки на таймеры созревания."""
    prev = -1
    while True:
        pending = await _pending()
        if pending == 0 or pending == prev:
            break
        prev = pending
        await flush_batch(SPACE)
