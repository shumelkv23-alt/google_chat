"""Юнит-тесты сборки контекстного окна (_merge_recent в extraction).

_merge_recent сливает тред (точную ветку reply) и space-фон в одно окно:
дедуп по message_id, сортировка по времени, обрезка до _MERGED_CONTEXT_LIMIT.
Чистая логика над списками dict — без БД/сети.
"""

from datetime import datetime, timedelta, timezone

from app.services.extraction import _MERGED_CONTEXT_LIMIT, _merge_recent

_BASE = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)


def _msg(message_id: str, offset_min: int, text: str = "msg") -> dict:
    """Сообщение со сдвигом по времени: больше offset_min → свежее."""
    return {
        "message_id": message_id,
        "author_name": "user",
        "text": text,
        "created_at": _BASE + timedelta(minutes=offset_min),
    }


def test_merges_and_sorts_by_time():
    # Тред и space сливаются в одно окно, упорядоченное по времени
    space = [_msg("s1", 1), _msg("s2", 3)]
    thread = [_msg("t1", 2)]

    merged = _merge_recent(thread, space)

    assert [m["message_id"] for m in merged] == ["s1", "t1", "s2"]


def test_deduplicates_by_message_id_thread_wins():
    # Сообщение треда лежит и в space — в окне оно одно, версия треда побеждает
    space = [_msg("m1", 1, "space version")]
    thread = [_msg("m1", 1, "thread version")]

    merged = _merge_recent(thread, space)

    assert len(merged) == 1
    assert merged[0]["text"] == "thread version"


def test_truncates_to_merged_limit_keeping_newest():
    # Больше лимита → остаются только самые свежие _MERGED_CONTEXT_LIMIT штук
    n = _MERGED_CONTEXT_LIMIT + 2
    space = [_msg(f"s{i}", i) for i in range(n)]

    merged = _merge_recent([], space)

    ids = [m["message_id"] for m in merged]
    assert len(merged) == _MERGED_CONTEXT_LIMIT
    assert ids[0] == "s2"          # два самых старых (s0, s1) отброшены
    assert ids[-1] == f"s{n - 1}"  # самое свежее на месте


def test_empty_inputs_return_empty():
    assert _merge_recent([], []) == []
