"""Память диалога: свёртка старых реплик в running_summary (этап 7).

Вызывается из FastAPI BackgroundTasks после ответа бота, когда recent_turns
переросли окно. Сворачивает всё, кроме последних KEEP_LAST реплик, в running_summary.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import Conversation
from app.db.session import AsyncSessionLocal
from app.llm.summarizer import summarize_conversation
from app.logger import logger

# Порог свёртки: когда recent_turns достигают MAX_TURNS реплик — сворачиваем.
MAX_TURNS = 12
# Сколько последних реплик оставляем в окне после свёртки.
KEEP_LAST = 6


async def compact_conversation(user_id: str, space_id: str) -> None:
    """Свернуть старые реплики диалога в running_summary. Фоновый таск.

    Ошибки логируются и проглатываются — это фоновая операция вне запроса,
    падать наружу нечему.
    """
    try:
        async with AsyncSessionLocal() as session:
            conv = (
                await session.execute(
                    select(Conversation).where(
                        Conversation.user_id == user_id,
                        Conversation.space_id == space_id,
                    )
                )
            ).scalar_one_or_none()
            if conv is None:
                return

            turns = list(conv.recent_turns or [])
            if len(turns) <= KEEP_LAST:
                return  # нечего сворачивать

            old, keep = turns[:-KEEP_LAST], turns[-KEEP_LAST:]
            new_summary = await summarize_conversation(conv.running_summary, old)

            conv.running_summary = new_summary
            conv.recent_turns = keep
            conv.summary_updated_at = datetime.now(timezone.utc)
            await session.commit()

            logger.info(
                "conversation_compacted",
                user_id=user_id,
                space_id=space_id,
                folded=len(old),
                kept=len(keep),
            )
    except Exception as exc:
        logger.error(
            "conversation_compact_failed", user_id=user_id, error=str(exc)
        )
