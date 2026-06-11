"""Хэш текста сообщения — версия содержимого для batch-конвейера.

Инвариант: chat_messages.text_hash обновляется ВСЕГДА вместе с text (persist и
handle_edit) и только этой функцией — иначе условный mark_processed (сверка
хэша) перестаёт ловить правки во время флаша.
"""

import hashlib


def sha256_hex(text: str | None) -> str:
    """sha256 от текста в hex — тот же формат, что бэкфилл миграции 0004."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
