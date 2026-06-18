"""Скоринг итогового состояния БД против эталона датасета.

Матчим произведённые вакансии к ожидаемым по источникам (source_message_id
ревизий), а не по названию — название модель может переврать, привязка к
сообщениям точнее. На сматченной вакансии проверяем статус и ключевые поля.
"""

from dataclasses import dataclass, field

from sqlalchemy import text

from app.db.session import AsyncSessionLocal

from eval.dataset import ExpectedVacancy


@dataclass
class ComboScore:
    vac_expected: int = 0
    vac_produced: int = 0
    grouped_ok: int = 0  # ожидаемых вакансий, верно собранных в одну произведённую
    status_ok: int = 0
    field_ok: int = 0
    field_total: int = 0
    noise_violations: int = 0  # вакансии, порождённые сообщениями-мусором
    dupes: int = 0  # лишние произведённые вакансии сверх сматченных
    skills_f1_sum: float = 0.0  # сумма F1 по skills сматченных вакансий
    skills_scored: int = 0  # сколько эталонов имели ожидаемые skills
    llm_calls: int = 0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)


def skills_f1(predicted: set[str], expected: set[str]) -> float:
    """F1 на множествах технологий. Чистая функция (без БД) — юнит-тестируема.

    skills — множество, поэтому точность/полнота важнее голого «совпало/нет»:
    модель может верно назвать 2 из 3 технологий, и это не ноль.
    """
    if not expected and not predicted:
        return 1.0
    if not predicted or not expected:
        return 0.0
    tp = len(predicted & expected)
    if tp == 0:
        return 0.0
    precision = tp / len(predicted)
    recall = tp / len(expected)
    return 2 * precision * recall / (precision + recall)


async def fetch_state() -> tuple[list[dict], dict[str, set[str]]]:
    """Живые вакансии + карта vacancy_id → множество source message_id."""
    async with AsyncSessionLocal() as session:
        vac_rows = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, title, status, salary_min, salary_max,
                           location, additional_info, description,
                           skills, company, seniority, role_category
                    FROM vacancies WHERE is_deleted = false
                    """
                )
            )
        ).fetchall()
        rev_rows = (
            await session.execute(
                text(
                    """
                    SELECT v.id::text AS vid, cm.message_id AS mid
                    FROM vacancy_revisions r
                    JOIN vacancies v ON v.id = r.vacancy_id AND v.is_deleted = false
                    JOIN chat_messages cm ON cm.id = r.source_message_id
                    """
                )
            )
        ).fetchall()

    vacancies = [dict(r._mapping) for r in vac_rows]
    sources: dict[str, set[str]] = {}
    for r in rev_rows:
        sources.setdefault(r.vid, set()).add(r.mid)
    return vacancies, sources


def _field_checks(vac: dict, expected_fields: dict) -> tuple[int, int, list[str]]:
    ok = 0
    total = 0
    notes: list[str] = []
    for key, want in expected_fields.items():
        total += 1
        if key in ("salary_min", "salary_max"):
            if vac.get(key) == want:
                ok += 1
            else:
                notes.append(f"{key}={vac.get(key)}≠{want}")
        elif key == "location_contains":
            hay = (vac.get("location") or "").lower()
            if any(kw in hay for kw in want):
                ok += 1
            else:
                notes.append(f"location={vac.get('location')!r} без {want}")
        elif key == "additional_contains":
            hay = (vac.get("additional_info") or "").lower()
            missing = [kw for kw in want if kw not in hay]
            if not missing:
                ok += 1
            else:
                notes.append(f"additional без {missing}")
        elif key in ("seniority", "role_category", "company"):
            got = (vac.get(key) or "").lower()
            if got == str(want).lower():
                ok += 1
            else:
                notes.append(f"{key}={vac.get(key)!r}≠{want}")
    return ok, total, notes


def score(
    vacancies: list[dict],
    sources: dict[str, set[str]],
    expected: list[ExpectedVacancy],
    expected_none: tuple[str, ...],
) -> ComboScore:
    """Клейм-матчинг: каждая произведённая вакансия закрывает максимум один
    эталон. Чистый матч = вакансия покрывает ВСЕ источники эталона и НЕ содержит
    источников других эталонов. Так merge двух ролей в одну (вакансия тащит чужие
    источники) и split одной на две (ни одна не покрывает все) честно штрафуются.
    """
    result = ComboScore(vac_expected=len(expected), vac_produced=len(vacancies))
    by_id = {v["id"]: v for v in vacancies}
    union_all: set[str] = set().union(*(set(e.sources) for e in expected)) if expected else set()
    claimed: set[str] = set()

    for exp in expected:
        want = set(exp.sources)
        foreign = union_all - want  # источники, принадлежащие ДРУГИМ эталонам
        candidates = [
            vid
            for vid, src in sources.items()
            if want <= src and not (src & foreign) and vid not in claimed
        ]
        if not candidates:
            result.notes.append(f"{exp.key}: группировка провалена")
            continue
        # «Чистейший» — с наименьшим числом лишних (нецелевых) источников.
        candidates.sort(key=lambda vid: len(sources[vid] - want))
        vid = candidates[0]
        claimed.add(vid)
        result.grouped_ok += 1

        vac = by_id[vid]
        if vac["status"] == exp.status:
            result.status_ok += 1
        else:
            result.notes.append(f"{exp.key}: статус {vac['status']}≠{exp.status}")
        # skills скорим отдельно (F1 на множествах), остальные поля — общими проверками.
        scalar_fields = {k: v for k, v in exp.fields.items() if k != "skills"}
        ok, total, notes = _field_checks(vac, scalar_fields)
        result.field_ok += ok
        result.field_total += total
        result.notes += [f"{exp.key}: {n}" for n in notes]

        if "skills" in exp.fields:
            f1 = skills_f1(set(vac.get("skills") or []), set(exp.fields["skills"]))
            result.skills_f1_sum += f1
            result.skills_scored += 1
            if f1 < 1.0:
                result.notes.append(
                    f"{exp.key}: skills F1={f1:.2f} got={vac.get('skills')}"
                )

    # Невостребованные произведённые вакансии — дубли/выдумки/неверные разбиения.
    result.dupes = len([v for v in vacancies if v["id"] not in claimed])

    # Мусор не должен быть источником вакансий.
    noise = set(expected_none)
    result.noise_violations = len([vid for vid, src in sources.items() if src & noise])
    return result
