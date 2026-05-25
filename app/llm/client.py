"""Обёртка над OpenAI SDK с base_url на OpenRouter."""

from openai import AsyncOpenAI

from app.config import settings

_client = AsyncOpenAI(
    api_key=settings.openrouter_api_key,
    base_url="https://openrouter.ai/api/v1",
)


async def chat(
    messages: list[dict],
    model: str,
    response_format: dict | None = None,
) -> str:
    kwargs: dict = {}
    if response_format:
        kwargs["response_format"] = response_format

    response = await _client.chat.completions.create(
        model=model,
        messages=messages,
        **kwargs,
    )
    return response.choices[0].message.content or ""
