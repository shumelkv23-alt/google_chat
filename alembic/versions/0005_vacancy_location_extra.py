"""vacancies: location + additional_info

Revision ID: 0005_vacancy_location_extra
Revises: 0004_batch_v2
Create Date: 2026-06-11

Два новых атрибута вакансии: location (где работать — удалёнка/город/офис) и
additional_info (полезные детали, не вошедшие в отдельные поля: грейд, уровень
языка, формат, бенефиты). Оба nullable TEXT, семантика замены — как у остальных
полей вакансии. Бэкфилл не нужен: у существующих записей значения просто NULL.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_vacancy_location_extra"
down_revision: Union[str, None] = "0004_batch_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vacancies", sa.Column("location", sa.Text(), nullable=True))
    op.add_column(
        "vacancies", sa.Column("additional_info", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("vacancies", "additional_info")
    op.drop_column("vacancies", "location")
