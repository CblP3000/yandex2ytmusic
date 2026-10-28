"""Интерактивные меню: одиночный выбор и мультивыбор.

- `Menu` — выбор одного пункта из списка (вызывается обработчик).
- `MultiSelectMenu` — выбор нескольких пунктов (возвращает список).

Управление:
- стрелки ↑/↓, Home/End — навигация;
- цифры — набираемое число (для списка любого размера), Enter — подтвердить;
- Esc — сбросить набранное число или отменить выбор;
- q / Ctrl+C — выход.
"""

import sys
from dataclasses import dataclass
from typing import Any, Callable

from cli.ui import CharKey, Key, TerminalSession, read_key


@dataclass
class MenuItem:
    label: str
    handler: Callable[[], None] | None = None
    payload: Any = None


class BaseMenu:
    """Общая логика экранов меню."""

    def __init__(self, title: str = "", subtitle: str = ""):
        self.title = title
        self.subtitle = subtitle
        self.items: list[MenuItem] = []
        self.selected = 0
        self.pending_digits = ""
        self._flash = ""
        self._height = 0

    def add(self, label: str, handler: Callable[[], None] | None = None, payload: Any = None) -> MenuItem:
        item = MenuItem(label=label, handler=handler, payload=payload)
        self.items.append(item)
        return item

    def run(self) -> Any:
        """Показывает меню. Возвращает результат подтверждения/отмены."""
        with TerminalSession():
            try:
                confirmed = self._loop()
            except KeyboardInterrupt:
                confirmed = False
        print()
        if confirmed:
            return self._on_confirm()
        return self._on_abort()

    def _loop(self) -> bool:
        self._draw()
        while True:
            key = read_key()
            action = self._handle_key(key)
            if action == "confirm":
                return True
            if action == "abort":
                return False
            self._draw()

    def _draw(self) -> None:
        text = "\n".join(self._render_lines()) + "\n"
        if self._height:
            sys.stdout.write(f"\x1b[{self._height}A\x1b[J")
        sys.stdout.write(text)
        sys.stdout.flush()
        self._height = text.count("\n")

    def _render_lines(self) -> list[str]:
        lines = []
        if self.title:
            lines.append(self.title)
        if self.subtitle:
            lines.append(self.subtitle)
        for i, item in enumerate(self.items):
            marker = "> " if i == self.selected else "  "
            lines.append(f"{marker}{self._prefix(i)}{item.label}")
        if self.pending_digits:
            lines.append(f"Выбор: {self.pending_digits}")
        if self._flash:
            lines.append(self._flash)
            self._flash = ""
        lines.append(self._hint())
        return lines

    def _apply_digits(self) -> bool:
        n = int(self.pending_digits)
        self.pending_digits = ""
        if 1 <= n <= len(self.items):
            self.selected = n - 1
            return True
        self._flash = f"Нет пункта под номером {n}"
        return False

    def _handle_key(self, key: Key | CharKey) -> str:
        if isinstance(key, CharKey):
            if key.kind is Key.DIGIT:
                self.pending_digits += key.char
            elif key.kind is Key.CHAR and key.char in ("q", "Q"):
                return "abort"
            return "draw"
        if key is Key.UP:
            self.selected = (self.selected - 1) % len(self.items)
        elif key is Key.DOWN:
            self.selected = (self.selected + 1) % len(self.items)
        elif key is Key.HOME:
            self.selected = 0
        elif key is Key.END:
            self.selected = len(self.items) - 1
        elif key is Key.ENTER:
            if self.pending_digits and not self._apply_digits():
                return "draw"
            return "confirm"
        elif key is Key.ESC:
            if self.pending_digits:
                self.pending_digits = ""
            else:
                return "abort"
        elif key is Key.BACKSPACE:
            self.pending_digits = self.pending_digits[:-1]
        return "draw"

    def _prefix(self, index: int) -> str:
        return ""

    def _hint(self) -> str:
        return "↑/↓ — навигация · цифры — номер пункта · Enter — выбрать · Esc/q — выход"

    def _on_confirm(self) -> Any:
        raise NotImplementedError

    def _on_abort(self) -> Any:
        raise NotImplementedError


class Menu(BaseMenu):
    """Меню одиночного выбора: при подтверждении вызывается обработчик."""

    def _hint(self) -> str:
        return "↑/↓ — навигация · Home/End — крайние · цифры — номер пункта · Enter — выбрать · Esc/q — назад"

    def _on_confirm(self) -> None:
        item = self.items[self.selected]
        if item.handler:
            item.handler()
        return None

    def _on_abort(self) -> None:
        return None


class MultiSelectMenu(BaseMenu):
    """Меню множественного выбора: возвращает список выбранных MenuItem.

    `run()` возвращает `list[MenuItem]` при подтверждении и `None`
    при отмене (Esc/q).
    """

    def __init__(self, title: str = "", subtitle: str = "", allow_none: bool = True):
        super().__init__(title, subtitle)
        self.allow_none = allow_none
        self._checked: list[bool] = []

    def add(self, label: str, handler: Callable[[], None] | None = None, payload: Any = None) -> MenuItem:
        item = super().add(label, handler, payload)
        self._checked.append(False)
        return item

    def _prefix(self, index: int) -> str:
        return "[x] " if self._checked[index] else "[ ] "

    def _hint(self) -> str:
        return "↑/↓ — навигация · Space — отметить · цифры+Enter — по номеру · a — всё · n — ничего · Enter — готово · Esc/q — назад"

    def _handle_key(self, key: Key | CharKey) -> str:
        if key is Key.SPACE:
            self._checked[self.selected] = not self._checked[self.selected]
            return "draw"
        if key is Key.ENTER and self.pending_digits:
            if self._apply_digits():
                self._checked[self.selected] = not self._checked[self.selected]
            return "draw"
        if key is Key.ENTER:
            if not self.allow_none and not any(self._checked):
                self._flash = "Ничего не выбрано — отметь хотя бы один пункт"
                return "draw"
            return "confirm"
        if isinstance(key, CharKey) and key.kind is Key.CHAR:
            char = key.char.lower()
            if char == "a":
                self._checked = [True] * len(self._checked)
            elif char == "n":
                self._checked = [False] * len(self._checked)
            else:
                return super()._handle_key(key)
            return "draw"
        return super()._handle_key(key)

    def _on_confirm(self) -> list[MenuItem]:
        return [item for i, item in enumerate(self.items) if self._checked[i]]

    def _on_abort(self) -> None:
        return None
