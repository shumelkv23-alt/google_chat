"""Тесты извлечения текста из вложений (app/services/attachments.py).

Скачивание (_download_media) мокаем; DOCX/PDF генерируем на лету и парсим реальные
байты — так проверяем настоящий парсер, а не его заглушку.
"""

import asyncio
import io
from unittest.mock import AsyncMock, patch

from docx import Document
from pypdf import PdfWriter

from app.services import attachments

_DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_bytes(content: str) -> bytes:
    doc = Document()
    for line in content.split("\n"):
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _blank_pdf_bytes() -> bytes:
    """PDF с пустой страницей — extract_text вернёт пусто (эмуляция скана)."""
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=300)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _attachment(*, ctype="", name="", ref="res-1", drive=False) -> dict:
    att: dict = {"contentType": ctype, "contentName": name}
    if drive:
        att["driveDataRef"] = {"driveFileId": "file-1"}
    elif ref:
        att["attachmentDataRef"] = {"resourceName": ref}
    return att


def _extract(attachments_list, **download):
    with patch.object(attachments, "_download_media", new=AsyncMock(**download)):
        return asyncio.run(attachments.extract_text_from_attachments(attachments_list))


def test_extract_text_from_docx() -> None:
    data = _docx_bytes("Python разработчик\n5 лет опыта, FastAPI и PostgreSQL")
    text, hint = _extract([_attachment(ctype=_DOCX_TYPE, name="resume.docx")], return_value=data)
    assert hint is None
    assert text is not None
    assert "Python разработчик" in text
    assert "FastAPI и PostgreSQL" in text


def test_detects_docx_by_extension_when_type_missing() -> None:
    data = _docx_bytes("QA инженер с опытом автоматизации тестов на Python")
    text, hint = _extract(
        [_attachment(ctype="application/octet-stream", name="cv.docx")], return_value=data
    )
    assert hint is None
    assert "QA инженер" in text


def test_scanned_pdf_returns_scan_hint() -> None:
    # Пустая PDF-страница → текста нет → подсказка про скан.
    text, hint = _extract(
        [_attachment(ctype="application/pdf", name="scan.pdf")],
        return_value=_blank_pdf_bytes(),
    )
    assert text is None
    assert "скан" in hint.lower()


def test_skips_unsupported_type_without_downloading() -> None:
    dl = AsyncMock()
    with patch.object(attachments, "_download_media", new=dl):
        text, hint = asyncio.run(
            attachments.extract_text_from_attachments(
                [_attachment(ctype="image/png", name="photo.png")]
            )
        )
    assert (text, hint) == (None, None)
    dl.assert_not_awaited()  # неподдерживаемое даже не качаем


def test_skips_drive_attachment() -> None:
    dl = AsyncMock()
    with patch.object(attachments, "_download_media", new=dl):
        text, hint = asyncio.run(
            attachments.extract_text_from_attachments(
                [_attachment(ctype=_DOCX_TYPE, name="resume.docx", ref=None, drive=True)]
            )
        )
    assert (text, hint) == (None, None)
    dl.assert_not_awaited()


def test_permission_error_returns_permission_hint() -> None:
    text, hint = _extract(
        [_attachment(ctype=_DOCX_TYPE, name="resume.docx")],
        side_effect=attachments.MediaDownloadError(403),
    )
    assert text is None
    assert "прав" in hint.lower()


def test_too_large_returns_size_hint() -> None:
    text, hint = _extract(
        [_attachment(ctype="application/pdf", name="huge.pdf")],
        side_effect=attachments.MediaDownloadError(413),
    )
    assert text is None
    assert "5 МБ" in hint


def test_generic_download_error_returns_download_hint() -> None:
    text, hint = _extract(
        [_attachment(ctype=_DOCX_TYPE, name="resume.docx")],
        side_effect=RuntimeError("boom"),
    )
    assert text is None
    assert "скачать" in hint.lower()


def test_first_supported_attachment_wins() -> None:
    data = _docx_bytes("резюме: бэкенд-разработчик, Python, 6 лет коммерческого опыта")
    atts = [
        _attachment(ctype="image/png", name="a.png"),  # пропустим
        _attachment(ctype=_DOCX_TYPE, name="b.docx"),  # возьмём
    ]
    text, hint = _extract(atts, return_value=data)
    assert hint is None
    assert "бэкенд-разработчик" in text
