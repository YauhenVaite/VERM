"""Модуль Stack — объединение и разъединение карточек товаров Wildberries.

Модуль управляет группами карточек (nmID) через метод Content API:
    POST https://content-api.wildberries.ru/content/v2/cards/moveNm

Возможности:
  * объединение нескольких карточек в одну группу (общий imtID);
  * групповое разъединение — выбранные карточки получают один общий новый imtID;
  * поштучное разъединение — каждой карточке присваивается свой уникальный imtID
    (последовательные запросы гейтируются Rate Limiter'ом Token Bucket).

Все данные о товарах модуль читает напрямую из SQLite-базы data/inventory.db
(таблица wb_products). Токен Wildberries читается из общего файла .env
через DBase.get_wb_token("CONTENT") с fallback на мастер-токен.
"""

import logging
import os
import sqlite3
import sys
import threading
import tkinter as tk
from tkinter import messagebox

import requests
import customtkinter as ctk

# ---------------------------------------------------------------------------
# Пути проекта (определяются до импорта apps.DBase, чтобы работал и прямой
# запуск "python apps/Stack.py" без лаунчера).
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "inventory.db")
ENV_PATH = os.path.join(BASE_DIR, ".env")

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import apps.DBase as DBase  # noqa: E402 — импорт после настройки sys.path

# ---------------------------------------------------------------------------
# Константы интеграции с Wildberries Content API.
# ---------------------------------------------------------------------------
WB_API_BASE_URL = "https://content-api.wildberries.ru"
MOVE_NM_URL = f"{WB_API_BASE_URL}/content/v2/cards/moveNm"

# Лимит Wildberries: не более 30 карточек за одну операцию moveNm.
MAX_CARDS = 30

_logger = logging.getLogger("Stack")


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
def _configure_logging() -> None:
    """Настраивает логгер модуля, чтобы сообщения были видны в консоли."""
    if _logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


# ---------------------------------------------------------------------------
# Работа с .env
# ---------------------------------------------------------------------------
def _load_api_key():
    """Возвращает валидный токен категории CONTENT (или мастер-токен).

    Чтение .env и логику fallback реализует общий модуль apps.DBase.
    """
    return DBase.get_wb_token("CONTENT")


# ---------------------------------------------------------------------------
# Работа с базой данных SQLite (только чтение данных).
# ---------------------------------------------------------------------------
_SEARCH_BASE = """
    SELECT nmID, imtID, subjectID, subjectName, vendorCode, title
    FROM wb_products
"""


def _open_connection() -> sqlite3.Connection:
    """Открывает соединение с базой данных data/inventory.db (только чтение)."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _search_products(conn: sqlite3.Connection, query: str):
    """Ищет карточки в wb_products по введённому артикулу (vendorCode).

    Если введён только числовой ART — возвращает ВСЕ карточки с этим артикулом
    (одинаковый ART, но разные буквенные суффиксы SUP). Иначе пытается найти
    точное совпадение vendorCode, а затем — частичное (LIKE).
    """
    q = (query or "").strip()
    if not q:
        return []

    art, sup = DBase.extract_art_sup(q)

    if art and sup is None:
        rows = conn.execute(
            _SEARCH_BASE + " WHERE nmID IS NOT NULL AND ART = ? ORDER BY vendorCode",
            (art,),
        ).fetchall()
        if rows:
            return rows

    rows = conn.execute(
        _SEARCH_BASE
        + " WHERE nmID IS NOT NULL AND vendorCode = ? COLLATE NOCASE ORDER BY vendorCode",
        (q,),
    ).fetchall()
    if rows:
        return rows

    rows = conn.execute(
        _SEARCH_BASE + " WHERE nmID IS NOT NULL AND vendorCode LIKE ? ORDER BY vendorCode",
        (f"%{q}%",),
    ).fetchall()
    return rows


# ---------------------------------------------------------------------------
# Вызов метода Wildberries POST /content/v2/cards/moveNm.
# ---------------------------------------------------------------------------
def _safe_json(response: requests.Response):
    try:
        return response.json()
    except ValueError:
        return None


def _format_additional_errors(data) -> str:
    """Форматирует вложенные ошибки additionalErrors из ответа WB."""
    additional = data.get("additionalErrors") if isinstance(data, dict) else None
    if isinstance(additional, dict) and additional:
        parts = [f"{key}: {value}" for key, value in additional.items()]
        return "; ".join(parts)[:400]
    return "Неизвестная ошибка API"


def _format_http_error(response: requests.Response) -> str:
    """Формирует понятное сообщение об ошибке HTTP-ответа."""
    data = _safe_json(response)
    if isinstance(data, dict):
        if data.get("errorText"):
            return f"HTTP {response.status_code}: {data['errorText']}"
        if data.get("additionalErrors"):
            return f"HTTP {response.status_code}: {_format_additional_errors(data)}"
    body = (response.text or "").strip()
    snippet = body[:300] if body else f"HTTP {response.status_code}"
    return f"HTTP {response.status_code}: {snippet}"


def _move_nm(nm_ids, target_imt, api_key: str, success_text: str = "Операция выполнена успешно."):
    """Отправляет POST /content/v2/cards/moveNm.

    Если target_imt указан — объединяет nm_ids в группу с этим imtID.
    Если target_imt is None — разъединяет: WB сгенерирует новый imtID.
    Возвращает кортеж (ok, message).
    """
    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json",
    }
    payload = {"nmIDs": [int(n) for n in nm_ids]}
    if target_imt is not None:
        payload["targetIMT"] = int(target_imt)

    try:
        # Соблюдаем Rate-limit категории CONTENT перед каждым запросом к WB.
        DBase.LIMITER.wait_for_token("CONTENT")
        response = requests.post(MOVE_NM_URL, headers=headers, json=payload, timeout=30)
    except requests.RequestException as exc:
        _logger.error("Сетевая ошибка при вызове moveNm: %s", exc)
        return False, f"Сетевая ошибка: {exc}"

    if response.status_code >= 400:
        return False, _format_http_error(response)

    data = _safe_json(response)
    if isinstance(data, dict) and data.get("error"):
        return False, data.get("errorText") or _format_additional_errors(data)

    return True, success_text


def _get_default_root():
    """Возвращает корневое окно Tkinter лаунчера, если оно доступно."""
    try:
        root = tk._default_root  # noqa: SLF001 — штатный способ получить корень
        if root is not None:
            return root
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------------------
# Графическое окно управления группами (создаётся на главном потоке).
# ---------------------------------------------------------------------------
class StackWindow:
    """Окно объединения/разъединения карточек через метод moveNm."""

    def __init__(self, root, on_close=None):
        self.root = root
        self.on_close = on_close

        # Список карточек: {nmID, imtID, subjectID, subjectName, vendorCode, title}
        self.items = []
        self.subject_id = None  # общий subjectID рабочего списка
        self._busy = False

        self.window = ctk.CTkToplevel(root)
        self.window.title("Stack — объединение и разъединение карточек")
        self.window.geometry("780x700")
        self.window.minsize(680, 560)

        self._build_ui()
        self._refresh_list()

        self.window.protocol("WM_DELETE_WINDOW", self._on_close)
        self.window.after(120, self._focus_search)

    # --------------------------- Построение UI ---------------------------
    def _build_ui(self) -> None:
        header = ctk.CTkLabel(
            self.window,
            text="Управление группами карточек (moveNm)",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        header.pack(anchor="w", padx=20, pady=(20, 4))

        hint = ctk.CTkLabel(
            self.window,
            text="Введите vendorCode (артикул). Карточки с одинаковым ART будут добавлены разом.",
            font=ctk.CTkFont(size=12),
            text_color=("gray40", "gray70"),
        )
        hint.pack(anchor="w", padx=20, pady=(0, 12))

        search_row = ctk.CTkFrame(self.window, corner_radius=0, fg_color="transparent")
        search_row.pack(fill="x", padx=20, pady=(0, 8))
        self.search_entry = ctk.CTkEntry(search_row, placeholder_text="vendorCode (артикул)")
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Return>", lambda _event: self._add_by_query())
        self.search_entry.bind("<KeyPress>", self._on_key_press)
        ctk.CTkButton(search_row, text="Добавить", width=120, command=self._add_by_query).pack(
            side="left", padx=(10, 0)
        )

        info_row = ctk.CTkFrame(self.window, corner_radius=0, fg_color="transparent")
        info_row.pack(fill="x", padx=20, pady=(0, 8))
        self.count_label = ctk.CTkLabel(
            info_row, text="", font=ctk.CTkFont(size=13, weight="bold")
        )
        self.count_label.pack(side="left")
        ctk.CTkButton(info_row, text="Очистить список", width=140, command=self._clear_list).pack(
            side="right"
        )

        self.listbox = ctk.CTkTextbox(self.window, wrap="word")
        self.listbox.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        self.listbox.configure(state="disabled")

        actions = ctk.CTkFrame(self.window, corner_radius=0, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(0, 8))
        self.merge_btn = ctk.CTkButton(actions, text="Объединить", command=self._merge)
        self.merge_btn.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.split_group_btn = ctk.CTkButton(
            actions, text="Разъединить в новую группу", command=self._split_group
        )
        self.split_group_btn.pack(side="left", fill="x", expand=True, padx=6)

        self.split_each_btn = ctk.CTkButton(
            self.window,
            text="Разъединить поштучно (Уникализировать)",
            command=self._split_each,
        )
        self.split_each_btn.pack(fill="x", padx=20, pady=(0, 8))

        self.status_label = ctk.CTkLabel(
            self.window,
            text="Готово.",
            anchor="w",
            text_color=("gray40", "gray70"),
        )
        self.status_label.pack(fill="x", padx=20, pady=(0, 20))

    def _focus_search(self) -> None:
        try:
            self.search_entry.focus_set()
        except Exception:  # noqa: BLE001
            pass

    def _on_key_press(self, event):
        """Обрабатывает Ctrl+V / Ctrl+М (обе раскладки) — вставка штрихкода."""
        if event.state & 0x0004:  # Control
            keysym = (event.keysym or "").lower()
            if keysym in ("v", "m", "cyrillic_em"):
                return self._on_paste()
        return None

    def _on_paste(self, event=None):
        """Вставляет штрихкод из буфера обмена и сразу обрабатывает его."""
        try:
            text = self.window.clipboard_get()
        except Exception:  # noqa: BLE001 — буфер обмена может быть пуст/недоступен
            return "break"
        text = (text or "").strip()
        if not text:
            return "break"
        self.search_entry.delete(0, "end")
        self.search_entry.insert(0, text)
        self._add_by_query()
        return "break"

    def _on_close(self) -> None:
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            pass
        if self.on_close is not None:
            self.on_close()

    # --------------------------- Утилиты UI ---------------------------
    def _ui(self, callback) -> None:
        """Маршалирует вызов обратно в главный поток (безопасно для Tk)."""
        try:
            self.window.after(0, callback)
        except Exception:  # noqa: BLE001
            pass

    def _set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _set_buttons_state(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.merge_btn.configure(state=state)
        self.split_group_btn.configure(state=state)
        self.split_each_btn.configure(state=state)

    def _nm_ids(self):
        return [item["nmID"] for item in self.items]


    def _subject_text(self) -> str:
        if self.subject_id is None:
            return "subjectID: —"
        name = ""
        if self.items:
            name = (self.items[0].get("subjectName") or "").strip()
        return f"subjectID: {self.subject_id}" + (f" ({name})" if name else "")

    def _refresh_list(self) -> None:
        subject_text = self._subject_text()
        self.count_label.configure(
            text=f"Добавлено карточек: {len(self.items)} / {MAX_CARDS}   |   {subject_text}"
        )
        self.listbox.configure(state="normal")
        self.listbox.delete("1.0", "end")
        if not self.items:
            self.listbox.insert("end", "Список пуст. Добавьте карточки по артикулу.\n")
        else:
            for index, item in enumerate(self.items, start=1):
                imt = item["imtID"] if item["imtID"] is not None else "—"
                title = (item["title"] or "").strip() or "(без названия)"
                self.listbox.insert(
                    "end",
                    f"{index}. vendorCode: {item['vendorCode']}  |  nmID: {item['nmID']}"
                    f"  |  imtID: {imt}  |  subjectID: {item['subjectID']}\n"
                    f"    {title}\n",
                )
        self.listbox.configure(state="disabled")

    # --------------------------- Поиск и наполнение списка ---------------------------
    def _add_by_query(self) -> None:
        query = self.search_entry.get().strip()
        if not query:
            messagebox.showwarning(
                "Stack", "Введите артикул (vendorCode) для поиска.", parent=self.window
            )
            return

        # Автоочистка поля — оно готово к следующему штрихкоду.
        self.search_entry.delete(0, "end")

        if len(self.items) >= MAX_CARDS:
            messagebox.showwarning(
                "Stack",
                f"Достигнут лимит: {MAX_CARDS} карточек за одну операцию.",
                parent=self.window,
            )
            return

        try:
            conn = _open_connection()
        except sqlite3.Error as exc:
            messagebox.showerror(
                "Stack", f"Не удалось открыть базу данных: {exc}", parent=self.window
            )
            return

        try:
            found = _search_products(conn, query)
        except sqlite3.Error as exc:
            messagebox.showerror(
                "Stack", f"Ошибка поиска в базе данных: {exc}", parent=self.window
            )
            return
        finally:
            conn.close()

        if not found:
            messagebox.showinfo(
                "Stack", f"По запросу «{query}» карточки не найдены.", parent=self.window
            )
            return

        existing_nm = {item["nmID"] for item in self.items}
        added = 0
        skipped_dup = 0
        skipped_subject = 0
        skipped_subject_ids = set()
        limit_reached = False

        for row in found:
            nm_id = row["nmID"]
            if nm_id in existing_nm:
                skipped_dup += 1
                continue

            subject_id = row["subjectID"]
            if self.subject_id is not None and subject_id != self.subject_id:
                skipped_subject += 1
                if subject_id is not None:
                    skipped_subject_ids.add(str(subject_id))
                continue

            if len(self.items) >= MAX_CARDS:
                limit_reached = True
                break

            self.items.append(
                {
                    "nmID": nm_id,
                    "imtID": row["imtID"],
                    "subjectID": subject_id,
                    "subjectName": row["subjectName"],
                    "vendorCode": row["vendorCode"],
                    "title": row["title"],
                }
            )
            if self.subject_id is None:
                self.subject_id = subject_id
            existing_nm.add(nm_id)
            added += 1

        self._refresh_list()

        messages = []
        if added:
            messages.append(f"Добавлено карточек: {added}.")
        if skipped_dup:
            messages.append(f"Уже были в списке: {skipped_dup}.")
        if skipped_subject:
            ids = ", ".join(sorted(skipped_subject_ids)) or "—"
            messages.append(
                f"Пропущено карточек другого предмета (subjectID {ids}): {skipped_subject}."
            )
        if limit_reached:
            messages.append(f"Достигнут лимит: {MAX_CARDS} карточек.")

        if not added:
            messagebox.showwarning(
                "Stack", "Карточки не добавлены.\n" + "\n".join(messages), parent=self.window
            )
        elif skipped_dup or skipped_subject or limit_reached:
            messagebox.showinfo("Stack", "\n".join(messages), parent=self.window)

    def _clear_list(self) -> None:
        self.items.clear()
        self.subject_id = None
        self._refresh_list()
        self._set_status("Список очищен.")


    # --------------------------- Операции moveNm ---------------------------
    def _start_operation(self, worker) -> None:
        self._busy = True
        self._set_buttons_state(False)
        self._set_status("Выполняется запрос к API Wildberries…")
        threading.Thread(target=self._worker_wrapper, args=(worker,), daemon=True).start()

    def _worker_wrapper(self, worker) -> None:
        api_key = _load_api_key()
        if not api_key:
            self._ui(
                lambda: self._finish_operation(
                    False, "Ошибка: токен Wildberries (CONTENT/мастер) не найден в файле .env."
                )
            )
            return
        try:
            ok, message = worker(api_key)
        except requests.RequestException as exc:
            _logger.error("Сетевая ошибка операции Stack: %s", exc)
            ok, message = False, f"Сетевая ошибка: {exc}"
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Неожиданная ошибка операции Stack")
            ok, message = False, f"Ошибка: {exc}"
        self._ui(lambda ok=ok, message=message: self._finish_operation(ok, message))

    def _finish_operation(self, ok: bool, message: str) -> None:
        self._busy = False
        self._set_buttons_state(True)
        self._set_status("Готово.")
        if ok:
            messagebox.showinfo("Stack", message, parent=self.window)
        else:
            messagebox.showerror("Stack", message, parent=self.window)

    def _merge(self) -> None:
        if self._busy:
            return
        if len(self.items) < 2:
            messagebox.showwarning(
                "Stack", "Для объединения нужно минимум 2 карточки.", parent=self.window
            )
            return

        target_imt = self.items[0]["imtID"]
        if target_imt is None:
            messagebox.showerror(
                "Stack",
                "У первой карточки отсутствует imtID — невозможно указать targetIMT.",
                parent=self.window,
            )
            return

        nm_ids = self._nm_ids()
        if not messagebox.askyesno(
            "Подтверждение",
            f"Объединить {len(nm_ids)} карточек в группу с targetIMT={target_imt}?",
            parent=self.window,
        ):
            return

        self._start_operation(
            lambda api_key: _move_nm(
                nm_ids,
                target_imt,
                api_key,
                success_text=f"Карточки успешно объединены (targetIMT={target_imt}).",
            )
        )

    def _split_group(self) -> None:
        if self._busy:
            return
        if not self.items:
            messagebox.showwarning(
                "Stack", "Список пуст — нечего разъединять.", parent=self.window
            )
            return

        nm_ids = self._nm_ids()
        if not messagebox.askyesno(
            "Подтверждение",
            f"Разъединить {len(nm_ids)} карточек в новую общую группу (новый imtID)?",
            parent=self.window,
        ):
            return

        self._start_operation(
            lambda api_key: _move_nm(
                nm_ids,
                None,
                api_key,
                success_text="Карточки разъединены в новую общую группу.",
            )
        )

    def _split_each(self) -> None:
        if self._busy:
            return
        if not self.items:
            messagebox.showwarning(
                "Stack", "Список пуст — нечего разъединять.", parent=self.window
            )
            return

        nm_ids = self._nm_ids()
        if not messagebox.askyesno(
            "Подтверждение",
            f"Уникализировать {len(nm_ids)} карточек (каждой — свой imtID)?\n"
            f"Будет отправлено {len(nm_ids)} запросов "
            f"с соблюдением rate-limit (Token Bucket).",
            parent=self.window,
        ):
            return

        self._start_operation(lambda api_key: self._run_split_each(nm_ids, api_key))

    def _run_split_each(self, nm_ids, api_key):
        total = len(nm_ids)
        ok_count = 0
        errors = []

        for index, nm_id in enumerate(nm_ids, start=1):
            self._ui(
                lambda i=index, n=nm_id: self._set_status(
                    f"Уникализация: {i}/{total} (nmID {n})…"
                )
            )
            ok, message = _move_nm([nm_id], None, api_key)
            if ok:
                ok_count += 1
            else:
                errors.append(f"nmID {nm_id}: {message}")

        if errors:
            detail = "\n".join(errors[:10])
            if len(errors) > 10:
                detail += f"\n…и ещё {len(errors) - 10} ошибок."
            return False, f"Уникализировано: {ok_count}/{total}.\nОшибки:\n{detail}"

        return True, f"Уникализировано карточек: {ok_count}/{total} (каждой присвоен свой imtID)."


# ---------------------------------------------------------------------------
# Точка входа модуля
# ---------------------------------------------------------------------------
def _run_with_root(root) -> None:
    """Запускает окно на главном потоке лаунчера и ждёт его закрытия."""
    done = threading.Event()
    window_ref = {}
    errors = []

    def launch() -> None:
        try:
            window_ref["win"] = StackWindow(root, on_close=done.set)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            _logger.exception("Ошибка создания окна Stack")
            done.set()

    root.after(0, launch)
    done.wait()

    if errors:
        print(f"[Stack] Ошибка интерфейса: {errors[0]}")


def _run_standalone() -> None:
    """Прямой запуск без лаунчера: создаём собственное корневое окно."""
    ctk.set_appearance_mode("System")
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()
    root.withdraw()

    done = threading.Event()
    window = StackWindow(root, on_close=done.set)

    def _watch() -> None:
        if done.is_set():
            root.destroy()
        else:
            root.after(100, _watch)

    root.after(100, _watch)
    root.mainloop()


def run() -> None:
    """Главная точка входа, вызываемая CustomTkinter-лаунчером."""
    print("[Stack] Started execution...")
    _configure_logging()
    _logger.info("Модуль Stack запущен.")

    # 1. Обновляем локальную базу данных по курсору (подтягиваем свежие карточки).
    print("[Stack] Обновляю базу данных через DBase…")
    try:
        DBase.run()
    except Exception as exc:  # noqa: BLE001
        print(f"[Stack] Ошибка обновления базы данных через DBase: {exc}")
        _logger.exception("Ошибка вызова DBase.run()")

    # 2. Открываем графическое окно управления группами.
    root = _get_default_root()
    if root is not None:
        _run_with_root(root)
    else:
        _run_standalone()

    print("[Stack] Finished successfully.")


if __name__ == "__main__":
    # Позволяет запускать модуль и напрямую: python apps/Stack.py
    run()





