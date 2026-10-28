"""Отладка: конфигурация логирования и сокрытие секретов.

Логи выводятся в консоль. По умолчанию показываются только ошибки и
предупреждения (WARNING). Чтобы включить подробные debug-логи, запусти
программу с переменной окружения DEBUG=1:

    DEBUG=1 python main.py

Секретные заголовки (cookie, authorization, x-goog-authuser) в лог
попадают только как факт присутствия и длина — без значений.
"""

import logging
import os
import sys

SECRET_FIELDS = {"cookie", "authorization", "x-goog-authuser", "x-goog-visitor-id", "x-client-data"}

_formatter = logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s", datefmt="%H:%M:%S")

_configured = False


def init_logging() -> None:
    """Настраивает консольное логирование (идемпотентно).

    Уровень: DEBUG при DEBUG=1, иначе WARNING (только ошибки/предупреждения).
    """
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_formatter)
    root = logging.getLogger()
    root.addHandler(handler)
    level = logging.DEBUG if os.environ.get("DEBUG") == "1" else logging.WARNING
    root.setLevel(level)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    init_logging()
    return logging.getLogger(name)


def redact_headers(headers: dict) -> dict:
    """Возвращает словарь заголовков, безопасный для лога: секреты заменяются на факт."""
    redacted = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered == "cookie" and isinstance(value, str):
            redacted[key] = f"<cookie: {len(value)} chars, starts {value[:8]!r}>"
        elif lowered == "authorization" and isinstance(value, str):
            redacted[key] = f"<authorization: {len(value)} chars, starts {value[:12]!r}>"
        elif lowered in SECRET_FIELDS:
            redacted[key] = f"<{lowered}: {len(str(value))} chars>"
        else:
            redacted[key] = value
    return redacted
