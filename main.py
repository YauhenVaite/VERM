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
import time
from pathlib import Path

import customtkinter as ctk

# ---------------------------------------------------------------------------
# Пути проекта
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
APPS_DIR = BASE_DIR / "apps"
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "config.json"
BOT_STATUS_PATH = DATA_DIR / "bot_status.json"
BOT_LOG_DIR = DATA_DIR / "logs"

# Максимум строк, отображаемых в консоли логов (защита от разрастания GUI).
MAX_CONSOLE_LINES = 10000

# Служебные папки создаём автоматически, чтобы проект сразу был готов к работе.
APPS_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Разрешаем импорт модулей из корня проекта и из папки apps/.
for _path in (BASE_DIR, APPS_DIR):
    _path_str = str(_path)
    if _path_str not in sys.path:
        sys.path.insert(0, _path_str)

# Общий модуль токенов Wildberries: get_wb_token(), WB_TOKEN_LABELS и т.д.
import apps.DBase as DBase  # noqa: E402 — импорт после настройки sys.path

# ---------------------------------------------------------------------------
# Общий конфиг
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "appearance_mode": "System",  # "Light" | "Dark" | "System"
    "color_theme": "blue",
    "window_size": "560x680",
    # Автозапуск модуля DBase при старте и интервал его перезапуска по таймеру.
    "dbase_auto_start": True,
    "dbase_interval_minutes": 60,
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

        self.config = load_config()
        self._appearance = self.config.get("appearance_mode", "System")

        ctk.set_appearance_mode(self._appearance)
        ctk.set_default_color_theme(self.config.get("color_theme", "blue"))

        self.title("Модульный лаунчер")
        self.geometry(self.config.get("window_size", "560x680"))
        self.minsize(440, 520)

        # Очередь сообщений из рабочих потоков в GUI-поток.
        # CustomTkinter/Tk не потокобезопасен, поэтому обновляем интерфейс
        # только из главного потока через опрос очереди.
        self._ui_queue: "queue.Queue[tuple]" = queue.Queue()

        # Состояние запуска модулей и ссылки на их кнопки (по stem файла).
        self._running_modules: set[str] = set()
        self._module_buttons: dict[str, ctk.CTkButton] = {}

        # Планировщик автозапуска DBase и модальное окно настроек.
        self._dbase_timer_id = None
        self._settings_window = None
        self._dbase_settings_button = None
        self._dbase_refresh_button = None
        self._bot_status_dot = None
        self._bot_console_button = None
        self._bot_console_window = None
        self._bot_console_textbox = None
        self._bot_console_offset = 0
        self._bot_console_files_read: set[str] = set()

        self._build_ui()
        self._refresh_modules()

        # Периодически забираем сообщения из очереди (безопасно для Tk).
        self.after(100, self._poll_queue)

        # Автозапуск DBase (если включён) и цикличный таймер перезапуска.
        self._setup_dbase_scheduler()

        # Индикатор состояния демона бота (зелёный/красный кружок).
        self.after(1000, self._poll_bot_status)

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
        self._module_buttons.clear()
        self._dbase_settings_button = None
        self._dbase_refresh_button = None
        self._bot_console_button = None

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

            row = ctk.CTkFrame(self.scroll, corner_radius=0, fg_color="transparent")
            row.pack(fill="x", padx=10, pady=6)

            button = ctk.CTkButton(
                row,
                text=label,
                height=48,
                corner_radius=10,
                font=ctk.CTkFont(size=15),
            )
            button.pack(side="left", fill="x", expand=True)
            button.configure(
                command=lambda p=module_path, b=button: self._on_click(p, b)
            )
            self._module_buttons[module_path.stem] = button

            # Рядом с кнопкой модуля DBase — кнопки полного обновления (↻) и
            # настроек автозапуска/таймера (⚙️).
            if module_path.stem == "DBase":
                refresh_button = ctk.CTkButton(
                    row,
                    text="🔄",
                    width=48,
                    height=48,
                    corner_radius=10,
                    font=ctk.CTkFont(size=16),
                )
                refresh_button.pack(side="left", padx=(8, 0))
                refresh_button.configure(command=self._on_full_update_click)
                self._dbase_refresh_button = refresh_button

                settings_button = ctk.CTkButton(
                    row,
                    text="⚙️",
                    width=48,
                    height=48,
                    corner_radius=10,
                    font=ctk.CTkFont(size=16),
                )
                settings_button.pack(side="left", padx=(8, 0))
                settings_button.configure(command=self._open_settings)
                self._dbase_settings_button = settings_button

            # Для модуля Bot — индикатор состояния демона (зелёный/красный кружок)
            # и кнопка консоли логов (🖥) справа от индикатора.
            if module_path.stem == "Bot":
                console_button = ctk.CTkButton(
                    row,
                    text="🖥",
                    width=48,
                    height=48,
                    corner_radius=10,
                    font=ctk.CTkFont(size=16),
                )
                console_button.pack(side="right", padx=(8, 6))
                console_button.configure(command=self._toggle_bot_console)
                self._bot_console_button = console_button

                dot = ctk.CTkLabel(
                    row,
                    text="●",
                    width=24,
                    font=ctk.CTkFont(size=20),
                    text_color="#e74c3c",
                )
                dot.pack(side="right", padx=(8, 0))
                self._bot_status_dot = dot

        self._set_status(f"Найдено модулей: {len(self._modules)}")

    # --------------------------- Запуск модуля ---------------------------
    def _on_click(self, module_path: Path, button: ctk.CTkButton) -> None:
        """Обработчик клика по кнопке модуля (запускает, если модуль свободен)."""
        self._start_module(module_path)

    def _start_module(self, module_path: Path) -> bool:
        """Запускает модуль в фоне, если он ещё не выполняется.

        Возвращает True, если запуск начат, иначе False — модуль уже выполняется,
        поэтому клик/таймер пропускает этот цикл и не запускает параллельную копию.
        """
        stem = module_path.stem
        if stem in self._running_modules:
            return False

        self._running_modules.add(stem)
        button = self._module_buttons.get(stem)
        label = (
            button.cget("text")
            if button is not None
            else stem.replace("_", " ").title()
        )

        if button is not None:
            button.configure(state="disabled", text=f"{label} — выполняется…")
        self._set_status(f"Запуск модуля '{stem}'…")

        threading.Thread(
            target=self._run_in_thread,
            args=(module_path, button, label, stem),
            daemon=True,
        ).start()
        return True

    def _run_in_thread(
        self,
        module_path: Path,
        button: ctk.CTkButton | None,
        label: str,
        stem: str,
    ) -> None:
        try:
            run_module(module_path)
            self._ui_queue.put(
                ("finish", button, label, stem, f"Модуль '{stem}' выполнен.")
            )
        except Exception as exc:  # noqa: BLE001 — показываем любую ошибку в статусе
            self._ui_queue.put(("finish", button, label, stem, f"Ошибка: {exc}"))

    def _on_full_update_click(self) -> None:
        """Обработчик кнопки «полное обновление БД» (↻ рядом с DBase)."""
        module_path = APPS_DIR / "_Full_Update.py"
        if not module_path.exists():
            self._set_status("Модуль полного обновления не найден.")
            return

        stem = module_path.stem
        if stem in self._running_modules or "DBase" in self._running_modules:
            self._set_status("Обновление БД уже выполняется…")
            return

        self._running_modules.add(stem)
        button = self._dbase_refresh_button
        if button is not None:
            button.configure(state="disabled")
        self._set_status("Полное обновление БД…")

        threading.Thread(
            target=self._run_full_update_in_thread,
            args=(module_path, button, stem),
            daemon=True,
        ).start()

    def _run_full_update_in_thread(
        self,
        module_path: Path,
        button: ctk.CTkButton | None,
        stem: str,
    ) -> None:
        try:
            run_module(module_path)
            self._ui_queue.put(
                ("finish_refresh", button, stem, "Полное обновление БД завершено.")
            )
        except Exception as exc:  # noqa: BLE001
            self._ui_queue.put(("finish_refresh", button, stem, f"Ошибка: {exc}"))

    # ------------------- Очередь сообщений (GUI-поток) -------------------
    def _poll_queue(self) -> None:
        try:
            while True:
                self._handle_message(self._ui_queue.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_message(self, message: tuple) -> None:
        kind = message[0]
        if kind == "finish":
            _, button, label, stem, status = message
            self._running_modules.discard(stem)
            if button is not None:
                button.configure(state="normal", text=label)
            self._set_status(status)
        elif kind == "finish_refresh":
            _, button, stem, status = message
            self._running_modules.discard(stem)
            if button is not None:
                button.configure(state="normal")
            self._set_status(status)

    # ------------------- Индикатор состояния демона бота -------------------
    def _poll_bot_status(self) -> None:
        """Периодически обновляет цвет индикатора бота по data/bot_status.json."""
        color = "#e74c3c"  # красный: не запущен
        try:
            if BOT_STATUS_PATH.exists():
                with BOT_STATUS_PATH.open("r", encoding="utf-8") as file:
                    data = json.load(file)
                state = data.get("state", "")
                heartbeat = data.get("last_heartbeat", 0)
                if state in ("running", "starting"):
                    if time.time() - float(heartbeat) < 15:
                        color = "#2ecc71"  # зелёный: работает
                    else:
                        color = "#f1c40f"  # жёлтый: запускается/нет heartbeat
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        if self._bot_status_dot is not None:
            self._bot_status_dot.configure(text_color=color)
        self.after(1000, self._poll_bot_status)

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    # ------------------------ Консоль логов бота ------------------------
    def _toggle_bot_console(self) -> None:
        """Переключает окно консоли логов бота (открыть/скрыть)."""
        if (
            self._bot_console_window is not None
            and self._bot_console_window.winfo_exists()
        ):
            self._close_bot_console()
        else:
            self._open_bot_console()

    def _open_bot_console(self) -> None:
        """Открывает отдельное окно с логами бота и запускает их обновление."""
        window = ctk.CTkToplevel(self)
        window.title("Консоль бота — логи")
        window.geometry("780x480")
        window.minsize(480, 320)

        header = ctk.CTkFrame(window, corner_radius=0, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(
            header,
            text="Логи бота (реальное время)",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left")
        ctk.CTkButton(
            header,
            text="Очистить",
            width=90,
            command=self._clear_bot_console,
        ).pack(side="right")

        self._bot_console_textbox = ctk.CTkTextbox(
            window,
            wrap="none",
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self._bot_console_textbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self._bot_console_textbox.configure(state="disabled")

        self._bot_console_window = window
        # При открытии показываем все имеющиеся логи, затем ведём «хвост».
        self._bot_console_offset = 0
        self._bot_console_files_read = set()

        window.protocol("WM_DELETE_WINDOW", self._close_bot_console)
        self._poll_bot_console()

    def _close_bot_console(self) -> None:
        """Закрывает окно консоли и прекращает опрос логов."""
        if self._bot_console_window is not None:
            try:
                self._bot_console_window.destroy()
            except Exception:  # noqa: BLE001 — окно могло быть уже закрыто
                pass
        self._bot_console_window = None
        self._bot_console_textbox = None

    def _clear_bot_console(self) -> None:
        """Очищает видимый текст консоли (поток логов продолжается)."""
        if self._bot_console_textbox is not None:
            self._bot_console_textbox.configure(state="normal")
            self._bot_console_textbox.delete("1.0", "end")
            self._bot_console_textbox.configure(state="disabled")

    def _poll_bot_console(self) -> None:
        """Периодически читает новые строки логов и добавляет их в консоль."""
        if (
            self._bot_console_window is None
            or not self._bot_console_window.winfo_exists()
        ):
            return

        new_lines = self._read_new_bot_log_lines()
        if new_lines:
            self._append_bot_log_lines(new_lines)

        self.after(1000, self._poll_bot_console)

    def _read_new_bot_log_lines(self) -> list[str]:
        """Возвращает новые строки логов бота.

        Ротированные файлы `bot.log.*` читаются целиком один раз каждый,
        а текущий `bot.log` — только новая часть (по байтовому смещению).
        """
        lines: list[str] = []
        if not BOT_LOG_DIR.exists():
            return lines

        current = BOT_LOG_DIR / "bot.log"

        # 1) Ротированные файлы bot.log.* — читаем целиком один раз каждый.
        for path in sorted(BOT_LOG_DIR.glob("bot.log.*"), key=lambda p: p.name):
            if path.name in self._bot_console_files_read:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            self._bot_console_files_read.add(path.name)
            if text:
                lines.extend(text.splitlines())

        # 2) Текущий файл bot.log — читаем только новую часть.
        if current.exists():
            try:
                size = current.stat().st_size
            except OSError:
                size = -1
            if size < self._bot_console_offset:
                # Файл пересоздан после ротации — начинаем читать заново.
                self._bot_console_offset = 0
            if size > self._bot_console_offset:
                try:
                    with current.open("rb") as file:
                        file.seek(self._bot_console_offset)
                        raw = file.read()
                    self._bot_console_offset = size
                except OSError:
                    return lines
                if raw:
                    lines.extend(raw.decode("utf-8", errors="replace").splitlines())
        return lines

    def _append_bot_log_lines(self, lines: list[str]) -> None:
        """Добавляет строки в текстовое поле консоли и прокручивает вниз."""
        box = self._bot_console_textbox
        if box is None or not lines:
            return
        box.configure(state="normal")
        box.insert("end", "\n".join(lines) + "\n")

        # Ограничиваем объём текста, чтобы окно не «тормозило».
        line_count = int(box.index("end-1c").split(".")[0])
        if line_count > MAX_CONSOLE_LINES:
            box.delete("1.0", f"{line_count - MAX_CONSOLE_LINES}.0")

        box.see("end")
        box.configure(state="disabled")

    # -------------------- Автозапуск и таймер DBase ---------------------
    def _setup_dbase_scheduler(self) -> None:
        """Настраивает автозапуск DBase при старте и цикличный таймер."""
        if self.config.get("dbase_auto_start"):
            dbase_path = APPS_DIR / "DBase.py"
            if dbase_path.exists():
                # Небольшая задержка, чтобы окно успело отрисоваться до запуска.
                self.after(500, lambda: self._start_module(dbase_path))
        self._schedule_dbase_timer()

    def _dbase_interval_ms(self) -> int:
        """Возвращает интервал перезапуска DBase в миллисекундах."""
        try:
            minutes = int(self.config.get("dbase_interval_minutes", 60))
        except (TypeError, ValueError):
            minutes = 60
        if minutes <= 0:
            minutes = 60
        return minutes * 60 * 1000

    def _schedule_dbase_timer(self) -> None:
        """Переустанавливает таймер перезапуска DBase под текущий интервал."""
        if self._dbase_timer_id is not None:
            try:
                self.after_cancel(self._dbase_timer_id)
            except Exception:  # noqa: BLE001 — таймер мог уже сработать
                pass
            self._dbase_timer_id = None

        self._dbase_timer_id = self.after(
            self._dbase_interval_ms(), self._on_dbase_timer
        )

    def _on_dbase_timer(self) -> None:
        """Срабатывает по таймеру: перезапускает DBase, если он не выполняется."""
        self._dbase_timer_id = None
        dbase_path = APPS_DIR / "DBase.py"
        if dbase_path.exists():
            # Если модуль уже выполняется, _start_module вернёт False и копия
            # не запустится; следующий цикл наступит по расписанию.
            self._start_module(dbase_path)
        self._schedule_dbase_timer()

    # ----------------------- Настройки лаунчера ---------------------
    def _open_settings(self) -> None:
        """Открывает модальное окно настроек: автозапуск DBase, токены и параметры API."""
        window = self._settings_window
        if window is not None and window.winfo_exists():
            window.lift()
            window.focus()
            return

        window = ctk.CTkToplevel(self)
        window.title("Настройки")
        window.geometry("520x720")
        window.resizable(False, False)
        window.transient(self)
        window.grab_set()
        self._settings_window = window

        # --- Секция: автозапуск DBase ---
        auto_var = ctk.BooleanVar(value=bool(self.config.get("dbase_auto_start", True)))
        ctk.CTkSwitch(
            window,
            text="Автозапуск DBase при старте",
            variable=auto_var,
        ).pack(anchor="w", padx=20, pady=(20, 12))

        interval_row = ctk.CTkFrame(window, corner_radius=0, fg_color="transparent")
        interval_row.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkLabel(interval_row, text="Интервал обновления (минут):").pack(side="left")
        interval_entry = ctk.CTkEntry(interval_row, width=80)
        interval_entry.pack(side="left", padx=(12, 0))
        interval_entry.insert(0, str(self.config.get("dbase_interval_minutes", 60)))

        # --- Секция: токены WB API (прокручиваемый фрейм) ---
        token_scroll = ctk.CTkScrollableFrame(
            window,
            label_text="Токены WB API",
            label_font=ctk.CTkFont(size=14, weight="bold"),
            height=250,
        )
        token_scroll.pack(fill="x", padx=20, pady=(0, 12))

        # --- Секция: токены Ozon API (прокручиваемый фрейм) ---
        oz_scroll = ctk.CTkScrollableFrame(
            window,
            label_text="Токены Ozon API",
            label_font=ctk.CTkFont(size=14, weight="bold"),
            height=70,
        )
        oz_scroll.pack(fill="x", padx=20, pady=(0, 12))

        # --- Секция: прочие параметры API (склады, Client-Id) ---
        params_scroll = ctk.CTkScrollableFrame(
            window,
            label_text="Параметры API (склады, Client-Id)",
            label_font=ctk.CTkFont(size=14, weight="bold"),
            height=140,
        )
        params_scroll.pack(fill="x", padx=20, pady=(0, 12))

        token_entries: dict[str, ctk.CTkEntry] = {}

        def _bind_paste(entry: ctk.CTkEntry) -> None:
            """Вставка значения из буфера обмена: Ctrl+V (EN) или Ctrl+М (RU-раскладка)."""
            def _on_key(event):
                if event.state & 0x0004:  # Control
                    keysym = (event.keysym or "").lower()
                    if keysym in ("v", "m", "cyrillic_em"):
                        try:
                            text = window.clipboard_get()
                        except Exception:
                            return "break"
                        if text:
                            entry.insert("insert", text)
                        return "break"
                return None

            entry.bind("<KeyPress>", _on_key)

        def _add_env_fields(container, labels, master_key=None, master_value="", masked=True):
            """Добавляет поля ввода в контейнер и регистрирует их в token_entries."""
            for key, label in labels:
                ctk.CTkLabel(container, text=label, anchor="w").pack(fill="x", pady=(8, 0))
                entry = ctk.CTkEntry(container, show="•" if masked else "")
                entry.pack(fill="x", pady=(2, 2))
                if master_key is not None and key == master_key:
                    entry.insert(0, master_value)
                else:
                    entry.insert(0, DBase.read_env_value(key) or "")
                _bind_paste(entry)
                token_entries[key] = entry

        # Мастер-токен WB предзаполняем через get_wb_token("MASTER"), чтобы учесть
        # обратную совместимость со старым ключом WB_API_KEY.
        _add_env_fields(
            token_scroll,
            DBase.WB_TOKEN_LABELS,
            master_key="WB_MASTER_TOKEN",
            master_value=DBase.get_wb_token("MASTER") or "",
        )
        _add_env_fields(oz_scroll, DBase.OZ_TOKEN_LABELS)
        _add_env_fields(params_scroll, DBase.ENV_PARAM_LABELS, masked=False)

        def close() -> None:
            self._settings_window = None
            window.destroy()

        def save() -> None:
            try:
                interval = int(interval_entry.get())
                if interval <= 0:
                    interval = self.config.get("dbase_interval_minutes", 60)
            except ValueError:
                interval = self.config.get("dbase_interval_minutes", 60)

            self.config["dbase_auto_start"] = bool(auto_var.get())
            self.config["dbase_interval_minutes"] = interval
            self._save_config()
            self._schedule_dbase_timer()

            # Сохраняем введённые токены в .env (аккуратный шаблон из 8 полей).
            values = {key: entry.get() for key, entry in token_entries.items()}
            try:
                DBase.write_env_file(values)
            except OSError as exc:
                self._set_status(f"Ошибка сохранения .env: {exc}")
                return

            auto_text = "вкл" if auto_var.get() else "выкл"
            close()
            self._set_status(
                f"Настройки сохранены: автозапуск={auto_text}, интервал={interval} мин., "
                "токены и параметры API обновлены."
            )

        window.protocol("WM_DELETE_WINDOW", close)
        ctk.CTkButton(window, text="Сохранить", width=140, command=save).pack(pady=(0, 16))

    def _save_config(self) -> None:
        """Сохраняет текущие настройки в data/config.json (UTF-8, читаемый вид)."""
        try:
            with CONFIG_PATH.open("w", encoding="utf-8") as file:
                json.dump(self.config, file, ensure_ascii=False, indent=2)
        except OSError as exc:
            self._set_status(f"Ошибка сохранения конфига: {exc}")


def main() -> None:
    app = LauncherApp()
    app.mainloop()


if __name__ == "__main__":
    main()
