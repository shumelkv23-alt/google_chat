from app.db.models import Vacancy
from app.services.edits import _REVERTABLE_FIELDS
from app.services.entity_resolution import _FIELD_MAP

_VACANCY_COLUMNS = set(Vacancy.__table__.columns.keys())


def test_new_fields_present_in_field_map():
    # Регресс на текущую задачу: location и additional_info заведены в маппинг.
    assert _FIELD_MAP["location"] == "location"
    assert _FIELD_MAP["additional_info"] == "additional_info"


def test_field_map_targets_are_real_columns():
    # Каждая колонка-назначение из _FIELD_MAP должна существовать на модели,
    # иначе setattr в _update_vacancy упадёт на ровном месте.
    for ext_key, column in _FIELD_MAP.items():
        assert column in _VACANCY_COLUMNS, f"{ext_key} → {column} нет в Vacancy"


def test_field_map_targets_are_revertable():
    # Любое поле, которое умеем писать, должны уметь и откатывать при удалении
    # источника — иначе откат молча теряет это поле (подводный камень из docs).
    for column in _FIELD_MAP.values():
        assert column in _REVERTABLE_FIELDS, f"{column} не в _REVERTABLE_FIELDS"


def test_revertable_fields_are_real_columns():
    # _REVERTABLE_FIELDS оперирует именами колонок (setattr на Vacancy) —
    # опечатка здесь так же тихо ломает откат.
    for column in _REVERTABLE_FIELDS:
        assert column in _VACANCY_COLUMNS, f"{column} нет в Vacancy"
