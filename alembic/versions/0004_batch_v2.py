"""batch v2: text_hash, process_status, flush_attempts, claimed_at on chat_messages

Revision ID: 0004_batch_v2
Revises: 0003_batch_processing
Create Date: 2026-06-11

Фундамент batch_mode_v2 (этап 0): версия содержимого сообщения (text_hash) для
условного mark_processed, статус обработки с терминальным 'failed' (dead-letter),
счётчик неудачных флашей (бисекция) и задел под мультиворкер (claimed_at).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_batch_v2"
down_revision: Union[str, None] = "0003_batch_processing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # text_hash — sha256 текста; обновляется всегда вместе с text (persist и
    # handle_edit). Версия содержимого: условный mark_processed сверяет её,
    # чтобы правка во время флаша не была молча проглочена (B1).
    op.add_column("chat_messages", sa.Column("text_hash", sa.Text(), nullable=True))

    # Счётчик неудачных флашей сообщения — драйвер бисекции и dead-letter (B10).
    op.add_column(
        "chat_messages",
        sa.Column(
            "flush_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # Задел под мультиворкер (claim пачки на уровне БД); пока всегда NULL.
    op.add_column(
        "chat_messages",
        sa.Column("claimed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    # Замена голому is_processed: добавляет терминальное состояние 'failed'.
    # is_processed остаётся на переходный период и обновляется синхронно.
    op.add_column(
        "chat_messages",
        sa.Column(
            "process_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
    )
    op.create_check_constraint(
        "chat_messages_process_status_check",
        "chat_messages",
        "process_status IN ('pending', 'processed', 'failed')",
    )

    # Бэкфилл: статус — из is_processed, hash — от текущего текста.
    op.execute(
        """
        UPDATE chat_messages
        SET process_status = CASE WHEN is_processed THEN 'processed' ELSE 'pending' END,
            text_hash      = encode(sha256(convert_to(coalesce(text, ''), 'UTF8')), 'hex')
        """
    )

    # Пересоздание partial-индекса очереди под новый статус.
    op.drop_index("ix_chat_messages_unprocessed", table_name="chat_messages")
    op.create_index(
        "ix_chat_messages_pending",
        "chat_messages",
        ["space_id", "created_at"],
        postgresql_where=sa.text("process_status = 'pending' AND is_deleted = false"),
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_pending", table_name="chat_messages")
    op.create_index(
        "ix_chat_messages_unprocessed",
        "chat_messages",
        ["space_id", "created_at"],
        postgresql_where=sa.text("is_processed = false"),
    )
    op.drop_constraint(
        "chat_messages_process_status_check", "chat_messages", type_="check"
    )
    op.drop_column("chat_messages", "process_status")
    op.drop_column("chat_messages", "claimed_at")
    op.drop_column("chat_messages", "flush_attempts")
    op.drop_column("chat_messages", "text_hash")
