"""Пре-фильтр: быстрая проверка, содержит ли сообщение информацию о вакансии."""

from app.config import settings
from app.llm.client import chat

_SYSTEM = (
    "Ты классификатор сообщений корпоративного чата. "
    "Определи, содержит ли сообщение информацию о вакансии — "
    "открытие, закрытие, изменение условий, поиск кандидата. "
    "Ответь ровно одним словом: yes или no."
)


async def is_vacancy_message(text: str) -> bool:
    result = await chat(
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": text},
        ],
        model=settings.openrouter_model_prefilter,
    )
    return result.strip().lower().startswith("yes")
