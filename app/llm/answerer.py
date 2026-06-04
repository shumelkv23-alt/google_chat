from app.config import settings
from app.llm.client import chat

_COMPLEX_MARKERS = {"почему", "как", "какие", "сравни", "объясни", "расскажи", "подробнее", "зачем"}

_SYSTEM = """Ты — дружелюбный ассистент по вакансиям в корпоративном чате. Общаешься живо и по-человечески, без канцелярщины.

Если вопрос про вакансии — источник истины это блок «Актуальное состояние вакансий» (данные из БД): опирайся в первую очередь на него. Блок «Релевантные сообщения» — лишь вспомогательный контекст; если он противоречит состоянию вакансий из БД, верь БД (сообщение могло устареть). Можешь добавить своё мнение или полезное наблюдение поверх фактов, если это уместно.

Если вопрос общий (например, что сейчас перспективнее, какие технологии в тренде, карьерный совет) — отвечай как эксперт по рынку труда, используй свои знания. Контекст из базы можешь использовать как дополнение.

Если данных по конкретной вакансии нет — скажи честно, но не сухо.
Отвечай по-русски, коротко и по делу."""


def _needs_high_reasoning(query: str) -> bool:
    words = set(query.lower().split())
    return bool(words & _COMPLEX_MARKERS)


async def generate_answer(user_query: str, context: str) -> str:
    effort = "high" if _needs_high_reasoning(user_query) else None
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"{context}\n\nВопрос: {user_query}"},
    ]
    return await chat(
        messages=messages,
        model=settings.openrouter_model_answer,
        reasoning_effort=effort,
    )
