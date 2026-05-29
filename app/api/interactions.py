from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from google.auth.transport import requests as grequests
from google.oauth2 import id_token
from sqlalchemy import select

from app.config import settings
from app.db.models import Conversation
from app.db.session import AsyncSessionLocal
from app.logger import logger
from app.services.interaction_handler import handle_query
from app.services.memory import MAX_TURNS, compact_conversation

router = APIRouter()

_google_request = grequests.Request()


def _verify_chat_jwt(authorization: str) -> None:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization[len("Bearer "):]
    id_token.verify_oauth2_token(
        token,
        _google_request,
        audience=settings.chat_app_audience,
    )


@router.post("/chat/interaction")
async def chat_interaction(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    if not settings.skip_jwt_validation:
        try:
            _verify_chat_jwt(request.headers.get("Authorization", ""))
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("interaction_jwt_invalid", error=str(e))
            raise HTTPException(status_code=401, detail="Invalid JWT")

    try:
        event = await request.json()
    except Exception:
        return JSONResponse(content={})

    event_type = event.get("type")

    if event_type == "ADDED_TO_SPACE":
        user_name = event.get("user", {}).get("displayName", "")
        return JSONResponse(
            content={"text": f"Привет, {user_name}! Я бот-ассистент по вакансиям. Спрашивай про открытые позиции."}
        )

    if event_type != "MESSAGE":
        return JSONResponse(content={})

    user_id = event.get("user", {}).get("name", "")
    space_id = event.get("space", {}).get("name", "")
    user_query = event.get("message", {}).get("text", "").strip()

    if not user_query:
        return JSONResponse(content={})

    logger.info(
        "interaction_message",
        user_id=user_id,
        space_id=space_id,
        query_len=len(user_query),
    )

    conversation = await _get_or_create_conversation(user_id, space_id)
    payload, turn_text = await handle_query(user_query, conversation)
    await _append_turns(background_tasks, user_id, space_id, user_query, turn_text)

    return JSONResponse(content=payload)


async def _get_or_create_conversation(user_id: str, space_id: str) -> Conversation:
    async with AsyncSessionLocal() as session:
        conv = (
            await session.execute(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.space_id == space_id,
                )
            )
        ).scalar_one_or_none()
        if conv is not None:
            return conv
        conv = Conversation(user_id=user_id, space_id=space_id)
        session.add(conv)
        await session.commit()
        await session.refresh(conv)
        return conv


async def _append_turns(
    background_tasks: BackgroundTasks,
    user_id: str,
    space_id: str,
    user_text: str,
    bot_text: str,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    async with AsyncSessionLocal() as session:
        conv = (
            await session.execute(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.space_id == space_id,
                )
            )
        ).scalar_one()
        turns = list(conv.recent_turns or [])
        turns.append({"role": "user", "text": user_text, "ts": now_iso})
        turns.append({"role": "assistant", "text": bot_text, "ts": now_iso})
        # Окно не обрезаем здесь — свёртка вытеснит старое в running_summary (см. memory.py).
        conv.recent_turns = turns
        conv.turns_count = (conv.turns_count or 0) + 1
        conv.updated_at = datetime.now(timezone.utc)
        await session.commit()

    # Накопилось достаточно реплик → фоновая свёртка старых в summary.
    if len(turns) >= MAX_TURNS:
        background_tasks.add_task(compact_conversation, user_id, space_id)
