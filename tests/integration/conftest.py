"""Интеграционные тесты: реальная БД chatbot_test + моки LLM/эмбеддингов.

Гоняются ОТДЕЛЬНО от юнитов:

    pytest tests/integration

Нужна поднятая тестовая БД (Postgres+pgvector). Строка берётся из переменной
TEST_DATABASE_URL, иначе — дефолт на локальный docker (chatbot_test).

ПОЧЕМУ ОТДЕЛЬНЫЙ ПРОГОН. app.db.session.engine создаётся на уровне импорта из
settings.database_url. Чтобы приложение смотрело в тестовую БД, DATABASE_URL
надо выставить ДО первого импорта app.db.session. В прогоне только
tests/integration корневой conftest и этот conftest исполняются раньше любого
импорта app — здесь и переставляем переменную. Если запустить весь сьют разом,
юнит-модули импортируют app.db.session с фейковой строкой раньше — поэтому
интеграцию держим отдельной командой.
"""

import hashlib
import os
import struct
from unittest.mock import AsyncMock

_TEST_DB = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:password@localhost:5432/chatbot_test",
)
# Жёсткий перехват: корневой conftest уже мог выставить фейковую DATABASE_URL
# через setdefault. Ставим тестовую строку ДО импорта app.* (ниже по файлу).
os.environ["DATABASE_URL"] = _TEST_DB

# pytest-anyio гоняет каждый тест в своём event loop, а пул соединений asyncpg
# привязан к loop, в котором соединение открылось. Переиспользование пулом в
# другом loop → «another operation is in progress». NullPool открывает свежее
# соединение на каждый запрос и сразу закрывает — кросс-loop переиспользования
# нет. Подменяем фабрику ДО импорта app.db.session, чтобы его
# `from ... import create_async_engine` подхватил обёртку. На прод-код не влияет:
# патч живёт только в процессе интеграционных тестов.
import sqlalchemy.ext.asyncio as _sa_async
from sqlalchemy.pool import NullPool

_orig_create_async_engine = _sa_async.create_async_engine


def _create_async_engine_nullpool(*args, **kwargs):
    kwargs["poolclass"] = NullPool
    return _orig_create_async_engine(*args, **kwargs)


_sa_async.create_async_engine = _create_async_engine_nullpool

import pytest
from sqlalchemy import text

# app импортируем ПОСЛЕ установки DATABASE_URL и патча пула — движок свяжется с
# тестовой БД и без пулинга.
from app.db.session import AsyncSessionLocal, engine

# Порядок не важен (TRUNCATE ... CASCADE), но перечисляем явно — список таблиц,
# которые чистим между тестами.
_TABLES = (
    "vacancy_revisions",
    "chat_messages_embeddings",
    "vacancies",
    "chat_messages",
    "conversations",
)


def _is_test_db(url) -> bool:
    """Предохранитель: имя базы должно содержать 'test'. Иначе TRUNCATE снёс бы
    рабочие данные. chatbot_test → ок; chatbot → блок; фейковый 'test' → ок,
    но не подключится (получим явную ошибку коннекта, а не порчу данных)."""
    return "test" in (url.database or "")


@pytest.fixture(scope="session", autouse=True)
def _guard_test_db():
    if not _is_test_db(engine.url):
        pytest.exit(
            f"Интеграционные тесты делают TRUNCATE, а движок смотрит в "
            f"'{engine.url.database}' — это не тестовая БД. Запусти с "
            f"TEST_DATABASE_URL на chatbot_test.",
            returncode=1,
        )
    yield


@pytest.fixture(autouse=True)
async def _clean_tables():
    """Чистим все таблицы ПЕРЕД каждым тестом — изоляция и детерминизм.
    Перед, а не после: тест всегда стартует с чистого листа, даже если
    предыдущий упал на середине и оставил мусор."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
        )
        await session.commit()
    yield


# --------------------------------------------------------------------------
# Моки внешних сервисов: эмбеддинги (OpenAI) + LLM (OpenRouter).
# В .env живые ключи — ни один интеграционный тест не должен ходить в сеть.
# Поэтому оба фикстуры autouse: сеть заглушена по умолчанию, а тест при нужде
# переопределяет поведение через ручки.
# --------------------------------------------------------------------------

_EMBED_DIM = 1536


def fake_embedding(textual: str) -> list[float]:
    """Детерминированный единичный вектор из текста: одинаковый текст → тот же
    вектор (cosine = 1), разный → почти ортогонально (cosine ≈ 0). Хватает,
    чтобы эмбеддинг-резолв в тестах вёл себя предсказуемо без реального OpenAI."""
    out: list[float] = []
    digest = hashlib.sha256((textual or "").encode("utf-8")).digest()
    while len(out) < _EMBED_DIM:
        digest = hashlib.sha256(digest).digest()
        for off in range(0, len(digest), 4):
            if len(out) >= _EMBED_DIM:
                break
            (raw,) = struct.unpack("<I", digest[off : off + 4])
            out.append(raw / 2**32 - 0.5)
    norm = sum(v * v for v in out) ** 0.5 or 1.0
    return [v / norm for v in out]


class _FakeEmbeddingItem:
    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class _FakeEmbeddingResponse:
    def __init__(self, data: list[_FakeEmbeddingItem]) -> None:
        self.data = data


async def _fake_embeddings_create(*, input, model=None, **_kw):
    """Зеркалит контракт openai: input str | list[str] → response.data[i].embedding."""
    items = [input] if isinstance(input, str) else list(input)
    return _FakeEmbeddingResponse([_FakeEmbeddingItem(fake_embedding(t)) for t in items])


# Каждый модуль создаёт свой AsyncOpenAI() — патчим .embeddings.create у каждого.
_EMBED_OPENAI_MODULES = (
    "app.services.entity_resolution",
    "app.services.ingest",
    "app.services.extraction",
    "app.services.batch_processor",
    "app.services.edits",
)


@pytest.fixture(autouse=True)
def mock_embeddings(monkeypatch):
    for module in _EMBED_OPENAI_MODULES:
        monkeypatch.setattr(
            f"{module}._openai.embeddings.create", _fake_embeddings_create
        )


class _LLM:
    """Пучок AsyncMock'ов на каждый use-site chat(). Тест ставит ответ через
    .return_value / .side_effect соответствующего мока."""

    def __init__(self, **mocks: AsyncMock) -> None:
        self.__dict__.update(mocks)


@pytest.fixture(autouse=True)
def llm(monkeypatch) -> _LLM:
    # Дефолты безопасные: prefilter говорит «не вакансия», экстракторы — пусто.
    mocks = {
        "prefilter": AsyncMock(return_value="no"),
        "extractor": AsyncMock(return_value='{"action": "none", "confidence": 0.0}'),
        "batch": AsyncMock(return_value='{"items": []}'),
        "resolver": AsyncMock(return_value='{"vacancy_id": null, "confidence": 0.0}'),
        "posting": AsyncMock(return_value='{"new_posting": false, "confidence": 0.0}'),
    }
    monkeypatch.setattr("app.llm.prefilter.chat", mocks["prefilter"])
    monkeypatch.setattr("app.llm.extractor.chat", mocks["extractor"])
    monkeypatch.setattr("app.llm.batch_extractor.chat", mocks["batch"])
    monkeypatch.setattr("app.llm.resolver.chat", mocks["resolver"])
    monkeypatch.setattr("app.llm.posting_classifier.chat", mocks["posting"])
    return _LLM(**mocks)
