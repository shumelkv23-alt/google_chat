"""batch processing: is_processed + quoted_message_id on chat_messages

Revision ID: 0003_batch_processing
Revises: 0002_edit_delete
Create Date: 2026-06-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_batch_processing"
down_revision: Union[str, None] = "0002_edit_delete"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Флаг «сообщение уже разобрано пайплайном». Новые строки рождаются с false;
    # батч-конвейер выбирает именно их и проставляет true после обработки.
    op.add_column(
        "chat_messages",
        sa.Column(
            "is_processed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # message_id процитированного сообщения (quoted reply). Сейчас приходит в
    # событии, но в БД не писался — без него батч не восстановит связь цитаты.
    op.add_column(
        "chat_messages",
        sa.Column("quoted_message_id", sa.Text(), nullable=True),
    )

    # Всё, что уже лежит в БД, разобрано старым per-message путём — помечаем
    # обработанным, чтобы при включении batch-режима пачка не схватила историю.
    op.execute("UPDATE chat_messages SET is_processed = true")

    # Частичный индекс под выборку необработанного по space в хронопорядке.
    op.create_index(
        "ix_chat_messages_unprocessed",
        "chat_messages",
        ["space_id", "created_at"],
        postgresql_where=sa.text("is_processed = false"),
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_unprocessed", table_name="chat_messages")
    op.drop_column("chat_messages", "quoted_message_id")
    op.drop_column("chat_messages", "is_processed")
