"""Юнит-тесты метрики качества извлечения skills (F1 на множествах).

Чистая функция из eval/scorer — мерит, насколько верно модель извлекла стек.
Множество, а не «совпало/нет»: 2 верных из 3 ≠ ноль. Это объективный порог
качества извлечения («бот работает отлично»), а не на глаз.
"""

import pytest

from eval.scorer import skills_f1


def test_exact_match():
    assert skills_f1({"python", "docker"}, {"python", "docker"}) == 1.0


def test_both_empty_is_perfect():
    assert skills_f1(set(), set()) == 1.0


def test_partial_overlap():
    # predicted={python}, expected={python,docker}: P=1.0, R=0.5 → F1≈0.667
    assert skills_f1({"python"}, {"python", "docker"}) == pytest.approx(2 / 3)


def test_extra_prediction_lowers_precision():
    # predicted={python,go}, expected={python}: P=0.5, R=1.0 → F1≈0.667
    assert skills_f1({"python", "go"}, {"python"}) == pytest.approx(2 / 3)


@pytest.mark.parametrize(
    "pred, exp",
    [
        (set(), {"python"}),  # ничего не извлёк
        ({"python"}, set()),  # извлёк лишнее, эталон пуст
        ({"java"}, {"python"}),  # нет пересечения
    ],
)
def test_zero_cases(pred, exp):
    assert skills_f1(pred, exp) == 0.0
