"""Создать или продлить Workspace Events подписку на Чат А.

Подписка с includeResource=True живёт максимум 4 часа. Обычно продление
происходит автоматически фоновой задачей FastAPI-приложения
(см. app/services/subscription.py). Этот скрипт нужен для ручного запуска —
например, чтобы создать подписку до первого старта приложения.

Запуск:
    python -m scripts.create_subscription
"""

import json

from app.services.subscription import ensure_subscription


def main() -> None:
    print("Создаём/продлеваем подписку...")
    result = ensure_subscription()
    print("Готово!")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
