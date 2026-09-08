"""Главный лаунчер модульного рабочего пространства.

При старте сканирует папку apps/, находит все файлы *.py (кроме
__init__.py и служебных файлов, начинающихся с "_"), создаёт для каждого
модуля кнопку в интерфейсе CustomTkinter и запускает модуль в отдельном
потоке, чтобы выполнение скрипта не "вешало" интерфейс.

Каждый модуль должен содержать главную функцию run().
"""

from __future__ import annotations

import importlib
import json
import queue
import sys
import threading
from pathlib import Path

import customtkinter as ctk

# ---------------------------------------------------------------------------
# Пути проекта
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
APPS_DIR = BASE_DIR / "apps"
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"

# Служебные папки создаём автоматически, чтобы проект сразу был готов к работе.
APPS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Разрешаем импорт модулей из корня проекта и из папки apps/.
for _path in (BASE_DIR, APPS_DIR):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

# ---------------------------------------------------------------------------
# Общий конфиг
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "appearance_mode": "System",  # "Light" | "Dark" | "System"
    "color_theme": "blue",
    "window_size": "560x680",
}


def load_config() -> dict:
    """Читает общий конфиг из data/config.json (если он существует)."""
    config = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open("r", encoding="utf-8") as file:
                config.update(json.load(file))
    except (OSError, json.JSONDecodeError):
        pass
    return config


# ---------------------------------------------------------------------------
# Обнаружение и запуск модулей
# ---------------------------------------------------------------------------
def discover_modules(apps_dir: Path) -> list[Path]:
    """Возвращает отсортированный список .py-файлов из папки apps/.

    Игнорирует __init__.py и файлы, начинающиеся с "_".
    """
    modules = []
    for path in apps_dir.glob("*.py"):
        if path.name == "__init__.py" or path.name.startswith("_"):
            continue
        modules.append(path)
    return sorted(modules)


def run_module(module_path: Path) -> None:
    """Импортирует модуль и вызывает его главную функцию run().

    Выполняется в отдельном потоке. Повторный запуск перезагружает модуль,
    чтобы подхватить свежие изменения в коде.
    """
    module_name = f"apps.{module_path.stem}"

    if module_name in sys.modules:
        module = importlib.reload(sys.modules[module_name])
    else:
        module = importlib.import_module(module_name)

    run_func = getattr(module, "run", None)
    if run_func is None or not callable(run_func):
        raise RuntimeError(
            f"В модуле '{module_path.name}' отсутствует функция run()"
        )
    run_func()


# ---------------------------------------------------------------------------
# Главное окно лаунчера
# ---------------------------------------------------------------------------
class LauncherApp(ctk.CTk):
    """Главное окно лаунчера."""

    def __init__(self):
        super().__init__()

        config = load_config()
        self._appearance = config.get("appearance_mode", "System")

        ctk.set_appearance_mode(self._appearance)
        ctk.set_default_color_theme(config.get("color_theme", "blue"))

        self.title("Модульный лаунчер")
        self.geometry(config.get("window_size", "560x680"))
        self.minsize(440, 520)

        # Очередь сообщений из рабочих потоков в GUI-поток.
        # CustomTkinter/Tk не потокобезопасен, поэтому обновляем интерфейс
        # только из главного потока через опрос очереди.
        self._ui_queue: "queue.Queue[tuple]" = queue.Queue()

        self._build_ui()
        self._refresh_modules()

        # Периодически забираем сообщения из очереди (безопасно для Tk).
        self.after(100, self._poll_queue)

    # --------------------------- Построение UI ---------------------------
    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(20, 10))

        ctk.CTkLabel(
            header,
            text="Модульное рабочее пространство",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).pack(anchor="w")

        ctk.CTkLabel(
            header,
            text=f"Папка модулей: {APPS_DIR}",
            font=ctk.CTkFont(size=12),
            text_color=("gray40", "gray70"),
        ).pack(anchor="w", pady=(2, 0))

        controls = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        controls.pack(fill="x", padx=20, pady=(0, 10))

        self.theme_switch = ctk.CTkSwitch(
            controls,
            text="Тёмная тема",
            command=self._toggle_theme,
        )
        self.theme_switch.pack(side="left")
        if self._appearance.lower() == "dark":
            self.theme_switch.select()

        ctk.CTkButton(
            controls,
            text="Обновить список",
            width=140,
            command=self._refresh_modules,
        ).pack(side="right")

        self.scroll = ctk.CTkScrollableFrame(
            self,
            label_text="Доступные модули",
            label_font=ctk.CTkFont(size=14, weight="bold"),
        )
        self.scroll.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        self.status = ctk.CTkLabel(
            self,
            text="Готово.",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=("gray40", "gray70"),
        )
        self.status.pack(fill="x", padx=20, pady=(0, 20))

    def _toggle_theme(self) -> None:
        if self.theme_switch.get() == 1:
            ctk.set_appearance_mode("Dark")
        else:
            ctk.set_appearance_mode("Light")

    # -------------------------- Список модулей ---------------------------
    def _refresh_modules(self) -> None:
        for child in self.scroll.winfo_children():
            child.destroy()

        self._modules = discover_modules(APPS_DIR)

        if not self._modules:
            ctk.CTkLabel(
                self.scroll,
                text="Модули не найдены.\nДобавьте .py-файл в папку apps/.",
                text_color=("gray40", "gray70"),
                justify="center",
            ).pack(padx=10, pady=20)
            self._set_status("Модули не найдены.")
            return

        for module_path in self._modules:
            label = module_path.stem.replace("_", " ").title()
            button = ctk.CTkButton(
                self.scroll,
                text=label,
                height=48,
                corner_radius=10,
                font=ctk.CTkFont(size=15),
            )
            button.pack(fill="x", padx=10, pady=6)
            button.configure(
                command=lambda p=module_path, b=button: self._on_click(p, b)
            )

        self._set_status(f"Найдено модулей: {len(self._modules)}")

    # --------------------------- Запуск модуля ---------------------------
    def _on_click(self, module_path: Path, button: ctk.CTkButton) -> None:
        if button.cget("state") == "disabled":
            return

        label = button.cget("text")
        button.configure(state="disabled", text=f"{label} — выполняется…")
        self._set_status(f"Запуск модуля '{module_path.stem}'…")

        threading.Thread(
            target=self._run_in_thread,
            args=(module_path, button, label),
            daemon=True,
        ).start()

    def _run_in_thread(
        self, module_path: Path, button: ctk.CTkButton, label: str
    ) -> None:
        try:
            run_module(module_path)
            self._ui_queue.put(
                ("finish", button, label, f"Модуль '{module_path.stem}' выполнен.")
            )
        except Exception as exc:  # noqa: BLE001 — показываем любую ошибку в статусе
            self._ui_queue.put(("finish", button, label, f"Ошибка: {exc}"))

    # ------------------- Очередь сообщений (GUI-поток) -------------------
    def _poll_queue(self) -> None:
        try:
            while True:
                self._handle_message(self._ui_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_message(self, message: tuple) -> None:
        kind, button, label, status = message
        if kind == "finish":
            button.configure(state="normal", text=label)
            self._set_status(status)

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)


def main() -> None:
    app = LauncherApp()
    app.mainloop()


if __name__ == "__main__":
    main()
