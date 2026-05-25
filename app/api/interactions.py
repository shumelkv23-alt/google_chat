from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from google.auth.transport import requests as grequests
from google.oauth2 import id_token

from app.config import settings
from app.logger import logger

router = APIRouter()

_google_request = grequests.Request()


def _verify_chat_jwt(authorization: str) -> None:
    """Проверяет JWT от Google Chat App."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization[len("Bearer "):]
    id_token.verify_oauth2_token(
        token,
        _google_request,
        audience=settings.chat_app_audience,
    )


@router.post("/chat/interaction")
async def chat_interaction(request: Request) -> JSONResponse:
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
    user = event.get("user", {}).get("displayName", "")
    message = event.get("message", {})
    question = message.get("text", "")

    logger.info("interaction_received", event_type=event_type, user=user, question=question)

    if event_type == "ADDED_TO_SPACE":
        return JSONResponse(content={"text": f"Привет, {user}! Я бот-ассистент."})

    if event_type == "MESSAGE":
        return JSONResponse(content={"text": f"Привет, {user}! Ты написал: {question}"})

    return JSONResponse(content={})
