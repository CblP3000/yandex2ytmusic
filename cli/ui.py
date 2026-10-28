"""Низкоуровневые примитивы интерактивного интерфейса.

Работает на Linux/macOS (termios/tty).
"""

import os
import select
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Callable


class Key(StrEnum):
    """Управляющие клавиши."""

    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    HOME = "home"
    END = "end"
    ENTER = "enter"
    ESC = "esc"
    BACKSPACE = "backspace"
    SPACE = "space"
    TAB = "tab"
    CHAR = "char"
    DIGIT = "digit"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CharKey:
    """Символьная клавиша (буква или цифра)."""

    kind: Key
    char: str


class TerminalSession:
    """Переводит терминал в режим чтения клавиш и восстанавливает его.

    Используется как context manager: в `__enter__` отключается
    канонический режим и эхо, прячется курсор; при выходе всё
    восстанавливается, даже в случае исключения.
    """

    def __enter__(self) -> "TerminalSession":
        self._fd = sys.stdin.fileno()
        if not sys.stdin.isatty():
            raise RuntimeError("Меню требует интерактивный терминал")
        import termios
        import tty

        self._termios = termios
        self._old_attrs = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        sys.stdout.write("\x1b[?25l")
        sys.stdout.flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._old_attrs)
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()


def _read_byte(fd: int) -> bytes:
    data = os.read(fd, 1)
    if not data:
        raise KeyboardInterrupt
    return data


def _read_escape(fd: int) -> Key:
    """Читает escape-последовательность после ESC.

    Если сразу за ESC нет других байтов — это одиночный Esc.
    """
    readable, _, _ = select.select([fd], [], [], 0.05)
    if not readable:
        return Key.ESC

    ch = _read_byte(fd)
    if ch != b"[":
        return Key.ESC

    seq = b"["
    while True:
        byte = os.read(fd, 1)
        if not byte:
            break
        seq += byte
        if byte not in b"0123456789;<>":
            break

    mappings = {
        b"[A": Key.UP,
        b"[B": Key.DOWN,
        b"[C": Key.RIGHT,
        b"[D": Key.LEFT,
        b"[H": Key.HOME,
        b"[F": Key.END,
        b"[1~": Key.HOME,
        b"[4~": Key.END,
        b"[3~": Key.UNKNOWN,
        b"[2~": Key.UNKNOWN,
    }
    return mappings.get(seq, Key.UNKNOWN)


def _key_from_bytes(byte: bytes) -> Key | CharKey:
    if byte in (b"\r", b"\n"):
        return Key.ENTER
    if byte in (b"\x7f", b"\x08"):
        return Key.BACKSPACE
    if byte == b" ":
        return Key.SPACE
    if byte == b"\t":
        return Key.TAB
    if byte in b"0123456789":
        return CharKey(Key.DIGIT, byte.decode())
    text = byte.decode(errors="ignore")
    if text and text.isprintable():
        return CharKey(Key.CHAR, text)
    return Key.UNKNOWN


def read_key() -> Key | CharKey:
    """Читает одно нажатие клавиши."""
    fd = sys.stdin.fileno()
    byte = _read_byte(fd)
    if byte == b"\x1b":
        return _read_escape(fd)
    return _key_from_bytes(byte)
