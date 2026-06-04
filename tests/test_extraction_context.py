"""Юнит-тесты сборки контекста для extractor (без БД).

_merge_recent склеивает тред-ветку и space-фон: дедуп по message_id, сортировка
по времени, ограничение окна. Нужно, чтобы при reply на безымянную зарплату в
контекст попало соседнее объявление вакансии из другого треда.
"""

from datetime import datetime, timedelta, timezone

from app.services.extraction import _MERGED_CONTEXT_LIMIT, _merge_recent

_T0 = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)


def _msg(mid: str, text: str, minutes: int) -> dict:
    return {
        "message_id": mid,
        "author_name": "A",
        "text": text,
        "created_at": _T0 + timedelta(minutes=minutes),
    }


def test_merge_dedups_by_message_id() -> None:
    # Сообщение треда лежит и в space-выборке — в итоге одно, без дубля.
    shared = _msg("m-salary", "зп 1500", 2)
    thread_ctx = [shared, _msg("m-now", "теперь 2000", 3)]
    space_ctx = [_msg("m-de", "ищем дата инженера", 1), shared]

    merged = _merge_recent(thread_ctx, space_ctx)

    ids = [m["message_id"] for m in merged]
    assert ids.count("m-salary") == 1
    assert set(ids) == {"m-de", "m-salary", "m-now"}


def test_merge_sorted_chronologically() -> None:
    thread_ctx = [_msg("m-now", "теперь 2000", 3)]
    space_ctx = [_msg("m-de", "ищем дата инженера", 1), _msg("m-salary", "зп 1500", 2)]

    merged = _merge_recent(thread_ctx, space_ctx)

    assert [m["message_id"] for m in merged] == ["m-de", "m-salary", "m-now"]


def test_merge_brings_other_thread_announcement_into_window() -> None:
    # Ключевой кейс: объявление вакансии (другой тред) попадает в окно extractor'а.
    thread_ctx = [_msg("m-salary", "зп 1500", 5), _msg("m-now", "теперь 2000", 6)]
    space_ctx = [_msg("m-de", "ищем дата инженера", 1)]

    merged = _merge_recent(thread_ctx, space_ctx)

    assert any("дата инженера" in m["text"] for m in merged)


def test_merge_caps_window_size() -> None:
    space_ctx = [_msg(f"s-{i}", f"msg {i}", i) for i in range(20)]
    thread_ctx = [_msg("t-last", "follow-up", 100)]

    merged = _merge_recent(thread_ctx, space_ctx)

    assert len(merged) == _MERGED_CONTEXT_LIMIT
    assert merged[-1]["message_id"] == "t-last"  # самое свежее остаётся
