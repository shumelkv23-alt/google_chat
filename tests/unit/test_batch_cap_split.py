
from app.services.batch_processor import _cap_by_tokens, _split_on_thread_boundary


def _msg(text: str = "x" * 30, thread_id: str | None = None) -> dict:
    return {"text": text, "thread_id": thread_id}


# --------------------------------------------------------------------------
# _cap_by_tokens
# --------------------------------------------------------------------------


def test_cap_returns_all_when_under_budget():
    batch = [_msg(), _msg(), _msg()]

    assert _cap_by_tokens(batch, budget=1000) == batch


def test_cap_cuts_tail_over_budget():
    # Каждое сообщение ~10 «токенов» (30 символов // 3). Бюджет 25 → две штуки.
    batch = [_msg(), _msg(), _msg()]

    out = _cap_by_tokens(batch, budget=25)

    assert out == batch[:2]


def test_cap_does_not_split_thread():
    # Бюджет вмещает соло + одно сообщение треда, но тред атомарен —
    # он либо входит целиком, либо целиком остаётся следующему тику.
    solo = _msg()
    thread = [_msg(thread_id="T"), _msg(thread_id="T"), _msg(thread_id="T")]

    out = _cap_by_tokens([solo] + thread, budget=25)

    assert out == [solo]


def test_cap_takes_first_group_even_if_over_budget():
    # Иначе огромный первый тред навечно заклинил бы очередь space.
    thread = [_msg(thread_id="T"), _msg(thread_id="T")]

    out = _cap_by_tokens(thread + [_msg()], budget=5)

    assert out == thread


def test_cap_empty_batch():
    assert _cap_by_tokens([], budget=100) == []


# --------------------------------------------------------------------------
# _split_on_thread_boundary
# --------------------------------------------------------------------------


def test_split_halves_batch_without_threads():
    batch = [_msg() for _ in range(6)]

    left, right = _split_on_thread_boundary(batch)

    assert left == batch[:3]
    assert right == batch[3:]


def test_split_moves_cut_out_of_thread():
    # Середина (индекс 2) попадает внутрь треда T (индексы 1–3) —
    # разрез сдвигается вперёд на ближайшую границу (индекс 4).
    batch = [
        _msg(),
        _msg(thread_id="T"),
        _msg(thread_id="T"),
        _msg(thread_id="T"),
        _msg(),
        _msg(),
    ]

    left, right = _split_on_thread_boundary(batch)

    assert left == batch[:4]
    assert right == batch[4:]
    # Тред целиком в одной половине.
    assert all(m["thread_id"] != "T" for m in right)


def test_split_single_thread_falls_back_to_middle():
    # Вся пачка — один тред: границы нет, изоляция ядовитого сообщения
    # важнее целостности контекста — режем посередине.
    batch = [_msg(thread_id="T") for _ in range(4)]

    left, right = _split_on_thread_boundary(batch)

    assert left == batch[:2]
    assert right == batch[2:]
    assert left and right  # обе половины непустые — бисекция сходится


def test_split_both_halves_nonempty_for_two_messages():
    batch = [_msg(), _msg()]

    left, right = _split_on_thread_boundary(batch)

    assert len(left) == 1 and len(right) == 1
