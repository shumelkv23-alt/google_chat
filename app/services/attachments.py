import asyncio
import io
import re
from typing import NamedTuple

import httpx
from docx import Document
from pypdf import PdfReader

from app.logger import logger
from app.services.google_oauth import get_credentials

_MEDIA_URL = "https://chat.googleapis.com/v1/media/{resource}?alt=media"
_MAX_BYTES = 5 * 1024 * 1024  # 5 МБ — резюме столько не весит
_MAX_PDF_PAGES = 30  # резюме редко длиннее; глубже не читаем
_MIN_TEXT_CHARS = 30  # меньше — считаем, что текст не извлёкся (скан/картинка)
_DOWNLOAD_TIMEOUT = 30.0

_PDF_TYPE = "application/pdf"
_DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_HINT_DOWNLOAD = (
    "Не смог скачать файл из чата. Пришли резюме текстом (с префиксом «резюме:») "
    "или другим файлом."
)
_HINT_PERMISSION = (
    "Не хватило прав скачать файл (бот качает только вложения из доступных ему "
    "сообщений). Пришли резюме текстом с префиксом «резюме:»."
)
_HINT_TOO_LARGE = "Файл великоват (лимит 5 МБ). Пришли версию полегче или резюме текстом."
_HINT_PARSE = (
    "Не смог разобрать файл. Проверь, что это обычный PDF/DOCX, или пришли резюме текстом."
)
_HINT_SCAN = (
    "Похоже, это скан или картинка — текст не извлекается. "
    "Пришли резюме текстом (с префиксом «резюме:»)."
)


class AttachmentText(NamedTuple):
    text: str | None
    hint: str | None


class MediaDownloadError(Exception):

    def __init__(self, status: int) -> None:
        super().__init__(f"media download failed: {status}")
        self.status = status


def _kind(content_type: str, filename: str) -> str | None:
    ct = (content_type or "").lower()
    name = (filename or "").lower()
    if ct == _PDF_TYPE or name.endswith(".pdf"):
        return "pdf"
    if ct == _DOCX_TYPE or name.endswith(".docx"):
        return "docx"
    return None


async def _download_media(resource_name: str) -> bytes:
    creds = await asyncio.to_thread(get_credentials)
    url = _MEDIA_URL.format(resource=resource_name)
    async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as client:
        async with client.stream(
            "GET", url, headers={"Authorization": f"Bearer {creds.token}"}
        ) as resp:
            if resp.status_code != 200:
                raise MediaDownloadError(resp.status_code)
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _MAX_BYTES:
                    raise MediaDownloadError(413)
                chunks.append(chunk)
            return b"".join(chunks)


def _download_hint(status: int) -> str:
    if status in (401, 403):
        return _HINT_PERMISSION
    if status == 413:
        return _HINT_TOO_LARGE
    return _HINT_DOWNLOAD


def _cleanup(raw: str) -> str:
    lines = [ln.rstrip() for ln in raw.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _parse_pdf(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("") 
        except Exception:
            pass
    texts: list[str] = []
    for page in reader.pages[:_MAX_PDF_PAGES]:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            continue  
    return _cleanup("\n".join(texts))


def _parse(data: bytes, kind: str) -> str:
    if kind == "pdf":
        return _parse_pdf(data)
    doc = Document(io.BytesIO(data))
    return _cleanup("\n".join(p.text for p in doc.paragraphs))


async def extract_text_from_attachments(attachments: list[dict]) -> AttachmentText:
    for att in attachments:
        ref = (att.get("attachmentDataRef") or {}).get("resourceName")
        kind = _kind(att.get("contentType", ""), att.get("contentName", ""))
        if not ref or kind is None:
            continue  

        try:
            data = await _download_media(ref)
        except MediaDownloadError as exc:
            logger.error("attachment_download_failed", status=exc.status)
            return AttachmentText(None, _download_hint(exc.status))
        except Exception as exc:
            logger.error("attachment_download_error", error=str(exc))
            return AttachmentText(None, _HINT_DOWNLOAD)

        try:
            text = _parse(data, kind)
        except Exception as exc:
            logger.error("attachment_parse_failed", kind=kind, error=str(exc))
            return AttachmentText(None, _HINT_PARSE)

        if len(text) < _MIN_TEXT_CHARS:
            logger.info("attachment_no_text", kind=kind, chars=len(text))
            return AttachmentText(None, _HINT_SCAN)

        logger.info("attachment_extracted", kind=kind, chars=len(text))
        return AttachmentText(text, None)
    return AttachmentText(None, None)
