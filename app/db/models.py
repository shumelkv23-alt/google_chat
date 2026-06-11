"""SQLAlchemy-модели по спеке из docs.md (раздел 6).

Все таблицы:
- chat_messages — append-only лог событий
- chat_messages_embeddings — векторы для cosine-поиска
- vacancies — текущее состояние вакансий (изменяемая)
- vacancy_revisions — журнал всех изменений (append-only)
- conversations — память диалога пользователя с ботом
"""

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _uuid_pk() -> Any:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    message_id: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    space_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(sa.Text)
    author_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    author_name: Mapped[str | None] = mapped_column(sa.Text)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=sa.text("NOW()")
    )
    source: Mapped[str] = mapped_column(sa.Text, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    is_edited: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    # batch-конвейер: false — ещё не разобрано пайплайном, ждёт пачку.
    # Переходный дубль process_status, обновляется синхронно с ним.
    is_processed: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    # message_id процитированного сообщения (quoted reply); нужен батчу, чтобы
    # восстановить связь цитаты при разборе пачки.
    quoted_message_id: Mapped[str | None] = mapped_column(sa.Text)
    # sha256 текста — версия содержимого; обновляется всегда вместе с text.
    # Условный mark_processed сверяет его, чтобы не проглотить правку,
    # пришедшую во время флаша (batch_mode_v2, B1).
    text_hash: Mapped[str | None] = mapped_column(sa.Text)
    # Счётчик неудачных флашей: драйвер бисекции пачки и dead-letter (B10).
    flush_attempts: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("0")
    )
    # Задел под мультиворкер (claim пачки на уровне БД); пока всегда NULL.
    claimed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # 'pending' | 'processed' | 'failed' — замена голому is_processed с
    # терминальным состоянием failed (ядовитое сообщение выведено из очереди).
    process_status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'pending'")
    )

    __table_args__ = (
        sa.CheckConstraint("source IN ('chat_a', 'chat_b')", name="chat_messages_source_check"),
        sa.CheckConstraint(
            "process_status IN ('pending', 'processed', 'failed')",
            name="chat_messages_process_status_check",
        ),
        sa.Index("ix_chat_messages_space_created", "space_id", sa.text("created_at DESC")),
        sa.Index("ix_chat_messages_thread", "thread_id"),
        sa.Index("ix_chat_messages_author", "author_id"),
        sa.Index(
            "ix_chat_messages_pending",
            "space_id",
            "created_at",
            postgresql_where=sa.text("process_status = 'pending' AND is_deleted = false"),
        ),
    )


class ChatMessageEmbedding(Base):
    __tablename__ = "chat_messages_embeddings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("chat_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=sa.text("NOW()")
    )


class Vacancy(Base):
    __tablename__ = "vacancies"

    id: Mapped[uuid.UUID] = _uuid_pk()
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=sa.text("'open'")
    )
    salary_min: Mapped[int | None] = mapped_column(sa.Integer)
    salary_max: Mapped[int | None] = mapped_column(sa.Integer)
    currency: Mapped[str | None] = mapped_column(sa.Text, server_default=sa.text("'RUB'"))
    owner_id: Mapped[str | None] = mapped_column(sa.Text)
    owner_name: Mapped[str | None] = mapped_column(sa.Text)
    team: Mapped[str | None] = mapped_column(sa.Text)
    description: Mapped[str | None] = mapped_column(sa.Text)
    # Где предстоит работать: "удалёнка", "Москва, офис", "гибрид, СПб".
    location: Mapped[str | None] = mapped_column(sa.Text)
    # Полезные детали, не вошедшие в отдельные поля (грейд, уровень языка,
    # формат, бенефиты). Семантика замены, как у остальных полей вакансии.
    additional_info: Mapped[str | None] = mapped_column(sa.Text)
    last_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("chat_messages.id")
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1536))
    confidence: Mapped[float | None] = mapped_column(sa.Float)
    is_deleted: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=sa.text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=sa.text("NOW()")
    )

    __table_args__ = (
        sa.CheckConstraint(
            "status IN ('open', 'closed', 'on_hold', 'filled')",
            name="vacancies_status_check",
        ),
        sa.Index("ix_vacancies_status", "status"),
    )


class VacancyRevision(Base):
    __tablename__ = "vacancy_revisions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    vacancy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("vacancies.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    changed_field: Mapped[str | None] = mapped_column(sa.Text)
    old_value: Mapped[str | None] = mapped_column(sa.Text)
    new_value: Mapped[str | None] = mapped_column(sa.Text)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("chat_messages.id")
    )
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=sa.text("NOW()")
    )

    __table_args__ = (
        sa.CheckConstraint(
            "action IN ('create','update','close','pending')",
            name="vacancy_revisions_action_check",
        ),
        sa.Index(
            "ix_vacancy_revisions_vacancy",
            "vacancy_id",
            sa.text("created_at DESC"),
        ),
        sa.Index("ix_vacancy_revisions_source", "source_message_id"),
        # Идемпотентность: одно сообщение не даёт двух ревизий одного действия.
        # Частичный — source_message_id nullable (pending/системные без источника).
        sa.Index(
            "uq_vacancy_revisions_source_action",
            "source_message_id",
            "action",
            unique=True,
            postgresql_where=sa.text("source_message_id IS NOT NULL"),
        ),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    space_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    running_summary: Mapped[str | None] = mapped_column(sa.Text)
    recent_turns: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=sa.text("'[]'::jsonb")
    )
    user_profile: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=sa.text("'{}'::jsonb")
    )
    turns_count: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"))
    summary_updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=sa.text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=sa.text("NOW()")
    )

    __table_args__ = (
        sa.UniqueConstraint("user_id", "space_id", name="conversations_user_space_unique"),
    )
