"""Интеграция сборки RAG-контекста (build_rag_context) на реальной БД chatbot_test.

Эмбеддинг запроса замокан (conftest подменяет app.services.rag._openai), у
засеянных строк ставим ненулевой вектор, чтобы они проходили фильтр
`embedding IS NOT NULL` и попадали в выборку cosine-поиска. Проверяем не точность
ранжирования (одна-две строки), а что нужные данные попадают в строку контекста и
что приоритеты источников/память диалога собираются корректно.
"""

from datetime import datetime, timezone

import pytest

from app.db.models import ChatMessage, ChatMessageEmbedding, Conversation, Vacancy
from app.db.session import AsyncSessionLocal
from app.services.rag import build_rag_context

pytestmark = pytest.mark.anyio

_NOW = datetime.now(timezone.utc)
# Ненулевой вектор: лишь бы строка прошла `embedding IS NOT NULL`. Точность
# ранжирования на одной-двух строках не проверяем — порядок здесь не важен.
_EMBED = [0.1] * 1536


async def _seed_vacancy(title: str, **kwargs) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            Vacancy(
                title=title,
                status=kwargs.pop("status", "open"),
                skills=kwargs.pop("skills", []),
                embedding=_EMBED,
                created_at=_NOW,
                updated_at=_NOW,
                **kwargs,
            )
        )
        await session.commit()


async def _seed_message(text: str, author_name: str = "Иван") -> None:
    async with AsyncSessionLocal() as session:
        msg = ChatMessage(
            message_id=f"m-{text[:20]}",
            space_id="spaces/TEST",
            author_id="u1",
            author_name=author_name,
            text=text,
            created_at=_NOW,
            source="chat_a",
        )
        session.add(msg)
        await session.flush()  # нужен msg.id для FK эмбеддинга
        session.add(
            ChatMessageEmbedding(message_id=msg.id, embedding=_EMBED, model="test")
        )
        await session.commit()


async def test_build_rag_context_empty_db():
    context = await build_rag_context("любой запрос", None)

    # Обе секции данных пусты, память диалога — дефолтная.
    assert context.count("Нет данных") == 2
    assert "Первый диалог" in context


async def test_build_rag_context_includes_vacancy():
    await _seed_vacancy("Python Backend разработчик", salary_max=300)

    context = await build_rag_context("ищу бэкендера", None)

    assert "Python Backend разработчик" in context


async def test_build_rag_context_includes_message():
    await _seed_message("у нас открылась вакансия на go")

    context = await build_rag_context("что нового", None)

    assert "у нас открылась вакансия на go" in context
    assert "Иван" in context


async def test_build_rag_context_uses_conversation_memory():
    conv = Conversation(
        user_id="u1",
        space_id="spaces/TEST",
        running_summary="обсуждали python-вакансии",
        recent_turns=[{"role": "user", "text": "привет бот"}],
    )

    context = await build_rag_context("продолжим", conv)

    assert "обсуждали python-вакансии" in context
    assert "привет бот" in context


async def test_build_rag_context_vacancy_prioritized_over_message():
    """Вакансии — приоритетный источник: их блок идёт раньше блока сообщений."""
    await _seed_vacancy("Senior DevOps")
    await _seed_message("кто-нибудь видел вакансию devops")

    context = await build_rag_context("devops", None)

    vac_pos = context.index("Актуальное состояние вакансий")
    msg_pos = context.index("Релевантные сообщения")
    assert vac_pos < msg_pos
    assert "Senior DevOps" in context
