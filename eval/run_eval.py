"""Сравнение режимов (batch / per_message) и моделей на размеченном датасете.

Реальные LLM/эмбеддинги (твои ключи, стоит токенов) — поэтому это отдельный
скрипт, а не pytest. Гоняет каждую пару (режим × модель) на изолированной
chatbot_test N раз (LLM недетерминирован — усредняем), считает метрики против
эталона и печатает сравнительную таблицу + частоту расхождений.

Запуск:
    python -m eval.run_eval --models "deepseek/deepseek-v4-flash" --runs 3
    python -m eval.run_eval --models "a/b,c/d" --modes batch --runs 1

DATABASE_URL переставляется на тестовую БД ДО импорта app — иначе пайплайн
писал бы в рабочую базу.
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from collections import Counter

import structlog

# Консоль Windows по умолчанию cp1251 — кириллица и стрелки в отчёте её роняют.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# Глушим info-логи пайплайна (их сотни на прогон) — оставляем warning/error.
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))

_TEST_DB = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:password@localhost:5432/chatbot_test",
)
os.environ["DATABASE_URL"] = _TEST_DB

from eval.dataset import EXPECTED, EXPECTED_NONE, MESSAGES  # noqa: E402
from eval.runner import (  # noqa: E402
    get_calls,
    install_call_counter,
    install_embedding_retry,
    replay,
    reset_calls,
    reset_db,
    set_model,
)
from eval.scorer import ComboScore, fetch_state, score  # noqa: E402

_MODES = ("per_message", "batch")
_REPORT_PATH = os.path.join(os.path.dirname(__file__), "last_report.txt")


async def run_once(model: str, mode: str) -> ComboScore:
    await reset_db()
    reset_calls()
    set_model(model)
    started = time.monotonic()
    await replay(MESSAGES, mode)
    elapsed = time.monotonic() - started

    vacancies, sources = await fetch_state()
    result = score(vacancies, sources, EXPECTED, EXPECTED_NONE)
    result.llm_calls = get_calls()
    result.seconds = elapsed
    return result


def aggregate(scores: list[ComboScore]) -> dict:
    n = len(scores)

    def avg(getter) -> float:
        return sum(getter(s) for s in scores) / n

    notes: Counter[str] = Counter()
    for s in scores:
        for note in set(s.notes):  # уникальные расхождения за прогон
            notes[note] += 1

    # skills F1 усредняем по всем сматченным эталонам всех прогонов (микро-среднее).
    f1_sum = sum(s.skills_f1_sum for s in scores)
    f1_scored = sum(s.skills_scored for s in scores)

    return {
        "runs": n,
        "vac_expected": scores[0].vac_expected,
        "vac_produced": avg(lambda s: s.vac_produced),
        "grouped": avg(lambda s: s.grouped_ok),
        "status": avg(lambda s: s.status_ok),
        "field_ok": avg(lambda s: s.field_ok),
        "field_total": avg(lambda s: s.field_total),
        "skills_f1": f1_sum / f1_scored if f1_scored else 0.0,
        "noise": avg(lambda s: s.noise_violations),
        "dupes": avg(lambda s: s.dupes),
        "calls": avg(lambda s: s.llm_calls),
        "seconds": avg(lambda s: s.seconds),
        "notes": notes,
    }


def _short(model: str) -> str:
    return model.split("/")[-1][:22]


def build_report(rows: list[tuple[str, str, dict]]) -> str:
    out: list[str] = []
    runs = rows[0][2]["runs"] if rows else 0
    exp = rows[0][2]["vac_expected"] if rows else 0
    out.append(f"\nМатрица: усреднено по {runs} прогон(ам), ожидается вакансий: {exp}")
    header = (
        f"{'model':<24}{'mode':<13}{'vac':<7}{'group':<8}{'status':<8}"
        f"{'fields':<9}{'skF1':<7}{'noise':<7}{'calls':<7}{'sec':<7}"
    )
    out.append(header)
    out.append("-" * len(header))
    for model, mode, a in rows:
        fields = f"{a['field_ok']:.1f}/{a['field_total']:.0f}"
        out.append(
            f"{_short(model):<24}{mode:<13}"
            f"{a['vac_produced']:<7.1f}"
            f"{a['grouped']:<8.1f}"
            f"{a['status']:<8.1f}"
            f"{fields:<9}"
            f"{a['skills_f1']:<7.2f}"
            f"{a['noise']:<7.1f}{a['calls']:<7.1f}{a['seconds']:<7.1f}"
        )

    out.append("\nРасхождения с эталоном (частота по прогонам):")
    for model, mode, a in rows:
        if a["notes"]:
            out.append(f"\n[{_short(model)} · {mode}]")
            for note, cnt in a["notes"].most_common():
                out.append(f"  - {note}  (×{cnt}/{a['runs']})")
    return "\n".join(out)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="deepseek/deepseek-v4-flash")
    parser.add_argument("--modes", default=",".join(_MODES))
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    install_call_counter()
    install_embedding_retry()
    rows: list[tuple[str, str, dict]] = []
    for model in models:
        for mode in modes:
            print(f"-> прогон: {model} · {mode} ×{args.runs} ...", flush=True)
            scores = [await run_once(model, mode) for _ in range(args.runs)]
            rows.append((model, mode, aggregate(scores)))
            # Пишем отчёт после каждого комбо — при сбое готовое не теряется.
            with open(_REPORT_PATH, "w", encoding="utf-8") as fh:
                fh.write(build_report(rows))

    report = build_report(rows)
    print(report)
    print(f"\n(отчёт сохранён в {_REPORT_PATH})")


if __name__ == "__main__":
    asyncio.run(main())
