from app.config import settings
from app.llm.client import chat

_SYSTEM = (
    "Ты классификатор сообщений корпоративного чата. "
    "Определи, содержит ли сообщение информацию о вакансии — "
    "открытие, закрытие, изменение условий, поиск кандидата. "
    "Ответь ровно одним словом: yes или no."
)

_SYSTEM_WITH_CONTEXT = (
    "Ты классификатор сообщений корпоративного чата. "
    "Тебе дают контекст (предыдущие сообщения) и новое сообщение. "
    "Определи, является ли новое сообщение частью обсуждения вакансии — "
    "уточнение условий, изменение зарплаты, закрытие и т.п. "
    "Ответь ровно одним словом: yes или no."
)


async def is_vacancy_message(text: str, context_messages: list[dict] | None = None) -> bool:
    if context_messages:
        context_block = "Контекст:\n" + "".join(
            f"- {m.get('author_name') or 'unknown'}: {m['text']}\n"
            for m in context_messages
        )
        user_content = f"{context_block}\nНовое сообщение:\n{text}"
        system = _SYSTEM_WITH_CONTEXT
    else:
        user_content = text
        system = _SYSTEM

    result = await chat(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        model=settings.openrouter_model_prefilter,
    )
    return result.strip().lower().startswith("yes")
