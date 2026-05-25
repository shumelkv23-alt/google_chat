"""initial schema: chat_messages, embeddings, vacancies, revisions, conversations

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- chat_messages ---
    op.create_table(
        "chat_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("message_id", sa.Text(), nullable=False, unique=True),
        sa.Column("space_id", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column("author_id", sa.Text(), nullable=False),
        sa.Column("author_name", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "source IN ('chat_a', 'chat_b')", name="chat_messages_source_check"
        ),
    )
    op.create_index(
        "ix_chat_messages_space_created",
        "chat_messages",
        ["space_id", sa.text("created_at DESC")],
    )
    op.create_index("ix_chat_messages_thread", "chat_messages", ["thread_id"])
    op.create_index("ix_chat_messages_author", "chat_messages", ["author_id"])

    # --- chat_messages_embeddings ---
    op.create_table(
        "chat_messages_embeddings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
    )
    op.execute(
        "CREATE INDEX ix_chat_messages_embeddings_vec "
        "ON chat_messages_embeddings "
        "USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )

    # --- vacancies ---
    op.create_table(
        "vacancies",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'open'")
        ),
        sa.Column("salary_min", sa.Integer(), nullable=True),
        sa.Column("salary_max", sa.Integer(), nullable=True),
        sa.Column("currency", sa.Text(), server_default=sa.text("'RUB'")),
        sa.Column("owner_id", sa.Text(), nullable=True),
        sa.Column("owner_name", sa.Text(), nullable=True),
        sa.Column("team", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "last_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id"),
            nullable=True,
        ),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'closed', 'on_hold', 'filled')",
            name="vacancies_status_check",
        ),
    )
    op.create_index("ix_vacancies_status", "vacancies", ["status"])
    op.execute(
        "CREATE INDEX ix_vacancies_embedding "
        "ON vacancies "
        "USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 50)"
    )

    # --- vacancy_revisions ---
    op.create_table(
        "vacancy_revisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "vacancy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vacancies.id"),
            nullable=False,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("changed_field", sa.Text(), nullable=True),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column(
            "source_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id"),
            nullable=True,
        ),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "action IN ('create','update','close','pending')",
            name="vacancy_revisions_action_check",
        ),
    )
    op.create_index(
        "ix_vacancy_revisions_vacancy",
        "vacancy_revisions",
        ["vacancy_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_vacancy_revisions_source", "vacancy_revisions", ["source_message_id"]
    )

    # --- conversations ---
    op.create_table(
        "conversations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("space_id", sa.Text(), nullable=False),
        sa.Column("running_summary", sa.Text(), nullable=True),
        sa.Column(
            "recent_turns", postgresql.JSONB, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "user_profile", postgresql.JSONB, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("turns_count", sa.Integer(), server_default=sa.text("0")),
        sa.Column(
            "summary_updated_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "user_id", "space_id", name="conversations_user_space_unique"
        ),
    )


def downgrade() -> None:
    op.drop_table("conversations")
    op.drop_table("vacancy_revisions")
    op.drop_table("vacancies")
    op.drop_table("chat_messages_embeddings")
    op.drop_table("chat_messages")
