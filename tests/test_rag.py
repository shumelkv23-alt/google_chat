"""Unit-тест сборки RAG-контекста: описание вакансии должно попадать в промпт.

Без БД/сети — мокаем embedding-клиент и поиск (_search).
"""

import asyncio
from unittest.mock import AsyncMock, patch

from app.services import rag

_FAKE_VACANCY = {
    "title": "Python разработчик",
    "status": "open",
    "salary_min": 3000,
    "salary_max": 5000,
    "currency": "USD",
    "owner_name": "Кирилл",
    "team": "Backend",
    "description": "Нужен опыт с FastAPI и PostgreSQL",
    "updated_at": None,
}


def test_build_rag_context_includes_description() -> None:
    async def _run() -> str:
        fake_resp = AsyncMock()
        fake_resp.data = [AsyncMock(embedding=[0.1] * 1536)]
        with patch.object(rag, "_openai") as mock_openai, patch.object(
            rag, "_search", new=AsyncMock(return_value=([], [_FAKE_VACANCY]))
        ):
            mock_openai.embeddings.create = AsyncMock(return_value=fake_resp)
            return await rag.build_rag_context("ищу питониста", None)

    ctx = asyncio.run(_run())
    assert "Python разработчик" in ctx
    assert "FastAPI и PostgreSQL" in ctx  # описание дошло до контекста


def test_format_vacancies_without_description_omits_line() -> None:
    # Без описания строка собирается как раньше — лишнего «Описание:» нет.
    out = rag._format_vacancies([{"title": "QA", "status": "open"}])
    assert out == "- QA [open]"
