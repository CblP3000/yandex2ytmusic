"""Демо-версия интерактивных меню.

Запуск: python -m cli.demo
"""

from cli.menu import Menu, MultiSelectMenu


def demo_single() -> None:
    print("\n=== Демо: одиночное меню (12 пунктов) ===\n")
    menu = Menu("Одиночное меню", "Стрелки ↑/↓, цифры + Enter (например 12), Esc — назад")
    for i in range(1, 13):
        menu.add(f"Пункт {i}", handler=lambda i=i: print(f"\nВыбран пункт {i}"))
    menu.add("Завершить демо")
    menu.run()


def demo_multi() -> None:
    print("\n=== Демо: мультивыбор ===\n")
    menu = MultiSelectMenu(
        "Мультивыбор",
        "Space — отметить · цифры+Enter — по номеру · a/n — всё/ничего · Enter — готово",
    )
    for i in range(1, 7):
        menu.add(f"Вариант {i}")
    selected = menu.run()
    if selected is None:
        print("Выбор отменён")
    else:
        print("Выбрано:", ", ".join(item.label for item in selected) or "ничего")


def main() -> None:
    print("Демо интерактивных меню")
    demo_single()
    demo_multi()


if __name__ == "__main__":
    main()
