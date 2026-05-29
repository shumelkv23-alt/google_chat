"""Свёртка старых реплик диалога в running_summary (этап 7).

На вход — текущая выжимка + реплики, вытесняемые из окна recent_turns.
На выходе — обновлённый текст выжимки.
"""

from app.config import settings
from app.llm.client import chat

_SYSTEM = (
    "Ты ведёшь сжатую долговременную память диалога между пользователем "
    "и ботом-ассистентом по вакансиям. Тебе дают текущую выжимку и старые реплики, "
    "которые вытесняются из окна. Обнови выжимку: добавь новые факты, интересы "
    "пользователя и важный контекст из реплик; не дублируй то, что уже есть. "
    "Пиши кратко и по делу, на русском. "
    "Верни только обновлённый текст выжимки — без пояснений и преамбул."
)


def _format_turns(turns: list[dict]) -> str:
    lines = []
    for t in turns:
        role = "Пользователь" if t.get("role") == "user" else "Ассистент"
        lines.append(f"{role}: {t.get('text', '')}")
    return "\n".join(lines)


async def summarize_conversation(
    existing_summary: str | None, old_turns: list[dict]
) -> str:
    """Свернуть old_turns поверх existing_summary. Reasoning OFF."""
    user_content = (
        f"Текущая выжимка:\n{existing_summary or 'пусто'}\n\n"
        f"Старые реплики для добавления:\n{_format_turns(old_turns)}"
    )
    result = await chat(
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_content},
        ],
        model=settings.openrouter_model_summarize,
    )
    return result.strip()
