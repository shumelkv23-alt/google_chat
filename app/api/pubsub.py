import base64
import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import Response
from google.auth.transport import requests as grequests
from google.oauth2 import id_token

from app.config import settings
from app.logger import logger
from app.schemas.incoming import parse_we_event
from app.services.batch_processor import route_created_batch
from app.services.edits import handle_delete, handle_edit, handle_edit_batch
from app.services.extraction import run_extraction
from app.services.ingest import embed_message, persist_message

router = APIRouter()

_google_request = grequests.Request()


def _verify_pubsub_jwt(authorization: str) -> None:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization[len("Bearer "):]
    audience = f"{settings.app_base_url.rstrip('/')}/chat/pubsub-push"
    claims = id_token.verify_oauth2_token(token, _google_request, audience=audience)
    # Если настроен ожидаемый отправитель — токен должен быть подписан именно им.
    expected_email = settings.pubsub_oidc_email
    if expected_email and claims.get("email") != expected_email:
        raise HTTPException(status_code=401, detail="Untrusted token issuer")


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
    attributes = msg.get("attributes", {})

    if not data_b64:
        return Response(status_code=204)

    try:
        data = json.loads(base64.b64decode(data_b64))
    except Exception as e:
        logger.error("pubsub_decode_error", message_id=message_id, error=str(e))
        return Response(status_code=204)

    # Тип события WE API приходит в атрибуте CloudEvents `ce-type`
    # (Pub/Sub message.attributes), а не в теле data.
    event_type_raw = attributes.get("ce-type", "")
    try:
        incoming = parse_we_event({**data, "type": event_type_raw})
    except Exception as e:
        # Кривой payload не должен ронять хендлер в 500 — иначе Pub/Sub зациклит
        # редоставку (poison pill). Логируем и подтверждаем (ack) приёмом.
        logger.error(
            "pubsub_parse_error",
            message_id=message_id,
            type=event_type_raw,
            error=str(e),
        )
        return Response(status_code=204)
    if incoming is None:
        logger.info(
            "pubsub_event_skipped",
            message_id=message_id,
            type=event_type_raw,
            attribute_keys=list(attributes.keys()),
        )
        return Response(status_code=204)

    logger.info(
        "pubsub_event_received",
        message_id=message_id,
        type=incoming.event_type,
        text_chars=len(incoming.text),
        quoted=incoming.quoted_message_id,
    )

    if incoming.event_type == "created":
        # Запись в БД — синхронно, ДО ack: at-least-once гарантирует, что при
        # падении воркера Pub/Sub переотправит. Тяжёлое уходит в фон.
        is_new = await persist_message(incoming)
        if settings.processing_mode == "batch":
            # Batch: сообщение копится и ждёт пачку. Онлайн обрабатываем только
            # при структурной привязке к существующей вакансии — это решает
            # route_created_batch (см. fork.md).
            if is_new:
                background_tasks.add_task(route_created_batch, incoming)
        else:
            # Per-message: embedding идемпотентен и идёт даже на дубликатах —
            # чтобы догнать вектор, если он не успел создаться ранее.
            background_tasks.add_task(embed_message, incoming)
            if is_new:
                background_tasks.add_task(run_extraction, incoming)
    elif incoming.event_type == "updated":
        handler = (
            handle_edit_batch
            if settings.processing_mode == "batch"
            else handle_edit
        )
        background_tasks.add_task(handler, incoming)
    elif incoming.event_type == "deleted":
        background_tasks.add_task(handle_delete, incoming)

    return Response(status_code=204)
