"""Юнит-тесты ядра трендов compute_trend_direction.

Чистая функция (без БД) — считает направление и величину изменения спроса между
двумя окнами. Главный риск — деление на ноль при prev=0 (новая технология),
поэтому контракт «новая технология → direction=new, growth=None» фиксируем явно.
"""

import pytest

from app.services.trends import compute_trend_direction


def test_rising():
    r = compute_trend_direction(prev=4, cur=6)
    assert r["direction"] == "rising"
    assert r["delta"] == 2
    assert r["growth"] == pytest.approx(0.5)


def test_falling():
    r = compute_trend_direction(prev=10, cur=4)
    assert r["direction"] == "falling"
    assert r["delta"] == -6
    assert r["growth"] == pytest.approx(-0.6)


def test_flat():
    r = compute_trend_direction(prev=5, cur=5)
    assert r["direction"] == "flat"
    assert r["delta"] == 0
    assert r["growth"] == 0.0


def test_new_skill_no_previous_window():
    # prev=0 → деления на ноль нет: growth=None, технология помечена как новая.
    r = compute_trend_direction(prev=0, cur=3)
    assert r["direction"] == "new"
    assert r["growth"] is None
    assert r["delta"] == 3


def test_zero_to_zero_is_flat():
    r = compute_trend_direction(prev=0, cur=0)
    assert r["direction"] == "flat"
    assert r["growth"] is None
