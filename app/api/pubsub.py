import base64
import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import Response
from google.auth.transport import requests as grequests
from google.oauth2 import id_token

from app.config import settings
from app.logger import logger
from app.schemas.incoming import parse_we_event
from app.services.edits import handle_delete, handle_edit
from app.services.extraction import run_extraction
from app.services.ingest import ingest_message

router = APIRouter()

_google_request = grequests.Request()


def _verify_pubsub_jwt(authorization: str) -> None:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization[len("Bearer "):]
    audience = f"{settings.app_base_url}/chat/pubsub-push"
    id_token.verify_oauth2_token(token, _google_request, audience=audience)


@router.post("/chat/pubsub-push")
async def pubsub_push(request: Request, background_tasks: BackgroundTasks) -> Response:
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

    if not data_b64:
        return Response(status_code=204)

    try:
        data = json.loads(base64.b64decode(data_b64))
    except Exception as e:
        logger.error("pubsub_decode_error", message_id=message_id, error=str(e))
        return Response(status_code=204)

    incoming = parse_we_event(data)
    if incoming is None:
        logger.info("pubsub_event_skipped", message_id=message_id, type=data.get("type"))
        return Response(status_code=204)

    logger.info(
        "pubsub_event_received",
        message_id=message_id,
        type=incoming.event_type,
        text_chars=len(incoming.text),
    )

    if incoming.event_type == "created":
        background_tasks.add_task(ingest_message, incoming)
        background_tasks.add_task(run_extraction, incoming)
    elif incoming.event_type == "updated":
        background_tasks.add_task(handle_edit, incoming)
    elif incoming.event_type == "deleted":
        background_tasks.add_task(handle_delete, incoming)

    return Response(status_code=204)
