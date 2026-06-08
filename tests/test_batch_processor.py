"""Юнит-тесты оркестрации batch-конвейера.

БД не трогаем: функции, лезущие в базу (_fetch_pending, _embed_batch,
_mark_processed и т.д.), и resolve_and_save мокаются. Проверяем именно
маршрутизацию результатов и пометку обработанными.
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.llm.batch_extractor import BatchItem
from app.schemas.incoming import IncomingMessage
from app.services import batch_processor
from app.services.batch_processor import _apply_items, flush_batch


def _row(i: int, mid: str, text: str = "t", thread=None, quoted=None) -> dict:
    return {
        "id": f"uuid{i}",
        "message_id": mid,
        "space_id": "sp",
        "thread_id": thread,
        "author_id": "a",
        "author_name": "Иван",
        "text": text,
        "created_at": datetime.now(timezone.utc),
        "source": "chat_a",
        "quoted_message_id": quoted,
    }


def test_apply_items_routes_by_index_and_skips_none():
    batch = [_row(0, "m0"), _row(1, "m1"), _row(2, "m2")]
    items = [
        BatchItem(message_index=0, action="create", entity_ref="x", confidence=0.9),
        BatchItem(message_index=1, action="none", confidence=0.0),
        BatchItem(message_index=2, action="update", entity_ref="x", confidence=0.8),
    ]
    fake = AsyncMock()
    with patch.object(batch_processor, "resolve_and_save", fake):
        asyncio.run(_apply_items(batch, items))
    assert fake.await_count == 2  # none пропущен
    called_mids = [c.args[0].message_id for c in fake.await_args_list]
    assert called_mids == ["m0", "m2"]  # привязка к нужным сообщениям по индексу


def test_apply_items_skips_out_of_range_index():
    batch = [_row(0, "m0")]
    items = [BatchItem(message_index=99, action="create", entity_ref="x", confidence=0.9)]
    fake = AsyncMock()
    with patch.object(batch_processor, "resolve_and_save", fake):
        asyncio.run(_apply_items(batch, items))
    fake.assert_not_awaited()  # битый индекс не роняет, просто пропуск


def test_flush_batch_orchestrates_and_marks_processed():
    batch = [_row(0, "m0"), _row(1, "m1")]
    items = [BatchItem(message_index=0, action="create", entity_ref="x", confidence=0.9)]
    with (
        patch.object(batch_processor, "_fetch_pending", AsyncMock(return_value=batch)),
        patch.object(batch_processor, "_embed_batch", AsyncMock()),
        patch.object(batch_processor, "_build_markup", AsyncMock(return_value=[])),
        patch.object(batch_processor, "_fetch_open_vacancies", AsyncMock(return_value=[])),
        patch.object(batch_processor, "extract_batch", AsyncMock(return_value=items)),
        patch.object(batch_processor, "resolve_and_save", AsyncMock()) as rs,
        patch.object(batch_processor, "_mark_processed", AsyncMock()) as mark,
    ):
        asyncio.run(flush_batch("sp"))
    rs.assert_awaited_once()
    mark.assert_awaited_once()
    assert mark.await_args.args[0] == ["uuid0", "uuid1"]  # помечены все сообщения пачки


def test_flush_batch_empty_is_noop():
    with (
        patch.object(batch_processor, "_fetch_pending", AsyncMock(return_value=[])),
        patch.object(batch_processor, "extract_batch", AsyncMock()) as ex,
        patch.object(batch_processor, "_mark_processed", AsyncMock()) as mark,
    ):
        asyncio.run(flush_batch("sp"))
    ex.assert_not_awaited()
    mark.assert_not_awaited()


def _incoming(mid: str) -> IncomingMessage:
    return IncomingMessage(
        message_id=mid,
        space_id="sp",
        thread_id="t",
        author_id="a",
        author_name="Иван",
        text="350k",
        created_at=datetime.now(timezone.utc),
    )


def test_route_created_batch_inline_when_anchor():
    # Есть привязка к существующей вакансии → обработать сразу + пометить.
    with (
        patch.object(
            batch_processor,
            "find_anchor_vacancy",
            AsyncMock(return_value=("vid", "thread")),
        ),
        patch.object(batch_processor, "embed_message", AsyncMock()) as emb,
        patch.object(batch_processor, "run_extraction", AsyncMock()) as ext,
        patch.object(batch_processor, "mark_processed_message", AsyncMock()) as mark,
    ):
        asyncio.run(batch_processor.route_created_batch(_incoming("m0")))
    emb.assert_awaited_once()
    ext.assert_awaited_once()
    mark.assert_awaited_once()


def test_route_created_batch_waits_when_no_anchor():
    # Нет привязки → ничего не делаем, сообщение остаётся пачке.
    with (
        patch.object(
            batch_processor,
            "find_anchor_vacancy",
            AsyncMock(return_value=(None, None)),
        ),
        patch.object(batch_processor, "embed_message", AsyncMock()) as emb,
        patch.object(batch_processor, "run_extraction", AsyncMock()) as ext,
        patch.object(batch_processor, "mark_processed_message", AsyncMock()) as mark,
    ):
        asyncio.run(batch_processor.route_created_batch(_incoming("m0")))
    emb.assert_not_awaited()
    ext.assert_not_awaited()
    mark.assert_not_awaited()
