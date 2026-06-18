"""vacancies: company + seniority + role_category + skills

Revision ID: 0006_vacancy_enrichment
Revises: 0005_vacancy_location_extra
Create Date: 2026-06-16

Аналитические оси под тренды и кластеры (чат — агрегатор многих компаний):
- company — работодатель (для разреза «зп по компаниям»);
- seniority — грейд (intern|junior|middle|senior|lead);
- role_category — категория специальности (backend|frontend|data|ml|...);
- skills — нормализованный стек технологий, TEXT[] NOT NULL DEFAULT '{}'.

Бэкфилл не нужен: у существующих записей company/seniority/role_category = NULL,
skills = пустой массив. Индексы: btree на created_at (оконная аналитика),
GIN на skills (фильтр по технологии skills @> ARRAY[...]).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_vacancy_enrichment"
down_revision: Union[str, None] = "0005_vacancy_location_extra"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("vacancies", sa.Column("company", sa.Text(), nullable=True))
    op.add_column("vacancies", sa.Column("seniority", sa.Text(), nullable=True))
    op.add_column("vacancies", sa.Column("role_category", sa.Text(), nullable=True))
    op.add_column(
        "vacancies",
        sa.Column(
            "skills",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.create_index(
        "ix_vacancies_created_at",
        "vacancies",
        [sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_vacancies_skills",
        "vacancies",
        ["skills"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_vacancies_skills", table_name="vacancies")
    op.drop_index("ix_vacancies_created_at", table_name="vacancies")
    op.drop_column("vacancies", "skills")
    op.drop_column("vacancies", "role_category")
    op.drop_column("vacancies", "seniority")
    op.drop_column("vacancies", "company")
