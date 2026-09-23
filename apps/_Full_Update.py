"""Модуль Full Update — полное обновление базы данных Wildberries.

Удаляет сохранённый курсор инкрементальной выгрузки (data/wb_cards_cursor.json)
и запускает DBase.run(), чтобы карточки товаров были скачаны заново целиком.

Удобно, когда нужно гарантированно перечитать весь каталог: например, после
изменения схемы БД или появления новых полей у уже существующих карточек.
"""

import importlib
import os
import sys
import threading
import tkinter as tk
from tkinter import messagebox

# Пути проекта (определяются до импорта apps.DBase, чтобы работал и прямой
# запуск "python apps/_Full_Update.py" без лаунчера).
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import apps.DBase as DBase  # noqa: E402 — импорт после настройки sys.path


def _get_default_root():
    """Возвращает корневое окно Tkinter лаунчера, если оно доступно."""
    try:
        root = tk._default_root  # noqa: SLF001 — штатный способ получить корень
        if root is not None:
            return root
    except Exception:  # noqa: BLE001
        pass
    return None


def _ask_confirmation(root) -> bool:
    """Спрашивает подтверждение у пользователя.

    Если root=None (запуск без лаунчера) — используется консольный ввод.
    Иначе диалог показывается на главном потоке Tk через root.after().
    """
    if root is None:
        answer = input(
            "Удалить файл курсора и скачать все карточки заново? [y/N]: "
        ).strip().lower()
        return answer in ("y", "yes", "д", "да")

    result = {"value": False}
    done = threading.Event()

    def ask() -> None:
        result["value"] = messagebox.askyesno(
            "Полное обновление БД",
            "Удалить файл курсора и скачать все карточки товаров заново?\n\n"
            "Будет выполнена полная выгрузка каталога Wildberries.",
            parent=root,
        )
        done.set()

    root.after(0, ask)
    done.wait()
    return result["value"]


def _show_message(root, title: str, message: str) -> None:
    """Показывает информационное сообщение (на главном потоке, если есть root)."""
    if root is None:
        print(f"[Full Update] {title}: {message}")
        return
    root.after(0, lambda: messagebox.showinfo(title, message, parent=root))


def _delete_cursor() -> bool:
    """Удаляет файл курсора выгрузки. Возвращает True, если всё прошло успешно."""
    cursor_path = DBase.CARDS_CURSOR_PATH
    if not os.path.exists(cursor_path):
        print(f"[Full Update] Файл курсора отсутствует: {cursor_path}")
        return True
    try:
        os.remove(cursor_path)
        print(f"[Full Update] Файл курсора удалён: {cursor_path}")
        return True
    except OSError as exc:  # noqa: BLE001
        print(f"[Full Update] Ошибка удаления курсора: {exc}")
        return False


def run() -> None:
    """Главная точка входа, вызываемая лаунчером в отдельном потоке."""
    print("[Full Update] Started execution...")

    root = _get_default_root()

    if not _ask_confirmation(root):
        print("[Full Update] Отменено пользователем.")
        return

    if not _delete_cursor():
        _show_message(root, "Ошибка", "Не удалось удалить файл курсора.")
        return

    print("[Full Update] Запускаю полное обновление базы данных…")
    try:
        # Лаунчер кэширует apps.DBase при импорте (main.py). Перезагружаем модуль,
        # чтобы полная выгрузка выполнялась актуальной версией кода, а не старой
        # копией из sys.modules.
        importlib.reload(DBase)
        DBase.run()
    except Exception as exc:  # noqa: BLE001
        print(f"[Full Update] Ошибка обновления БД: {exc}")
        _show_message(root, "Ошибка", f"Ошибка обновления БД:\n{exc}")
        return

    _show_message(root, "Готово", "Полное обновление базы данных завершено.")
    print("[Full Update] Finished successfully.")


if __name__ == "__main__":
    run()
