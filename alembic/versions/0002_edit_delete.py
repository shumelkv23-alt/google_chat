"""edit/delete flags + revision idempotency

Revision ID: 0002_edit_delete
Revises: 0001_initial
Create Date: 2026-05-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_edit_delete"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "chat_messages",
        sa.Column("is_edited", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "vacancies",
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    # На случай уже накопленных дублей (source_message_id, action) в dev-БД —
    # убираем их перед созданием unique-индекса, оставляя самую раннюю строку.
    op.execute(
        """
        DELETE FROM vacancy_revisions a
        USING vacancy_revisions b
        WHERE a.source_message_id IS NOT NULL
          AND a.source_message_id = b.source_message_id
          AND a.action = b.action
          AND a.ctid > b.ctid
        """
    )

    op.create_index(
        "uq_vacancy_revisions_source_action",
        "vacancy_revisions",
        ["source_message_id", "action"],
        unique=True,
        postgresql_where=sa.text("source_message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_vacancy_revisions_source_action", table_name="vacancy_revisions")
    op.drop_column("vacancies", "is_deleted")
    op.drop_column("chat_messages", "is_edited")
    op.drop_column("chat_messages", "is_deleted")
