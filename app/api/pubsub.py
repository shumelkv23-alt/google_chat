import base64
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from google.auth.transport import requests as grequests
from google.oauth2 import id_token

from app.config import settings
from app.logger import logger

router = APIRouter()

_google_request = grequests.Request()


def _verify_pubsub_jwt(authorization: str) -> None:
    """Проверяет JWT от Pub/Sub push-подписки."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization[len("Bearer "):]
    audience = f"{settings.app_base_url}/chat/pubsub-push"
    id_token.verify_oauth2_token(token, _google_request, audience=audience)


@router.post("/chat/pubsub-push")
async def pubsub_push(request: Request) -> Response:
    if not settings.skip_jwt_validation:
        try:
            _verify_pubsub_jwt(request.headers.get("Authorization", ""))
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("pubsub_jwt_invalid", error=str(e))
            raise HTTPException(status_code=401, detail="Invalid JWT")

    try:
        envelope = await request.json()
    except Exception as e:
        logger.error("pubsub_parse_error", error=str(e))
        return Response(status_code=204)

    msg = envelope.get("message", {})
    data_b64 = msg.get("data", "")
    message_id = msg.get("messageId", "")

    logger.info("pubsub_request", message_id=message_id, has_data=bool(data_b64))

    if data_b64:
        try:
            data = json.loads(base64.b64decode(data_b64))
            logger.info("pubsub_event_received", message_id=message_id, data=data)
        except Exception as e:
            logger.error("pubsub_decode_error", error=str(e))

    return Response(status_code=204)
