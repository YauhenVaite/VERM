"""Модуль Bot — запускает и останавливает фонового демона Telegram-бота.

Сам модуль НЕ выполняет бизнес-логику бота: он запускает отдельный
процесс-демон (`_BotDaemon.py`), чтобы бот продолжал работать даже после
закрытия лаунчера, и сразу возвращает управление. Кнопка модуля в лаунчере
переключает демон: если он запущен — останавливает, если остановлен — запускает.
Индикатор «работает/остановлен» лаунчер читает из `data/bot_status.json`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
APPS_DIR = BASE_DIR / "apps"
DATA_DIR = BASE_DIR / "data"
DAEMON_PATH = APPS_DIR / "_BotDaemon.py"
STATUS_PATH = DATA_DIR / "bot_status.json"
LOCK_PATH = DATA_DIR / "bot_daemon.lock"


def _pid_alive(pid) -> bool:
    """Проверяет, жив ли процесс с указанным PID (кросс-платформенно)."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        import ctypes

        try:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            return code.value == STILL_ACTIVE
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_status() -> dict:
    """Читает data/bot_status.json (пустой словарь при отсутствии/ошибке)."""
    try:
        with STATUS_PATH.open("r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}


def _write_status(state: str, message: str = "", pid: int | None = None) -> None:
    """Записывает служебный файл состояния демона для лаунчера."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with STATUS_PATH.open("w", encoding="utf-8") as file:
            json.dump(
                {
                    "state": state,
                    "message": message,
                    "pid": pid if pid is not None else os.getpid(),
                },
                file,
                ensure_ascii=False,
                indent=2,
            )
    except OSError:
        pass


def _lock_pid() -> int | None:
    """Возвращает PID демона из lock-файла (None, если файла нет/некорректен)."""
    try:
        if LOCK_PATH.exists():
            pid = int(LOCK_PATH.read_text(encoding="utf-8").strip() or 0)
            return pid or None
    except (OSError, ValueError):
        pass
    return None


def _terminate_process(pid: int) -> None:
    """Принудительно завершает процесс по PID (кросс-платформенно)."""
    if os.name == "nt":
        import ctypes

        try:
            PROCESS_TERMINATE = 0x0001
            handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                ctypes.windll.kernel32.TerminateProcess(handle, 1)
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass
    else:
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def is_running() -> bool:
    """True, если демон реально запущен (живой PID в lock-файле)."""
    pid = _lock_pid()
    return bool(pid and _pid_alive(pid))


def stop() -> None:
    """Останавливает запущенного демона и чистит lock-файл."""
    pid = _lock_pid()
    if pid and _pid_alive(pid):
        _terminate_process(pid)
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass
    _write_status("stopped", "Бот остановлен", pid=pid)
    print("[Bot] Бот остановлен.")


def start() -> None:
    """Запускает демон бота отдельным процессом."""
    python = sys.executable
    if os.name == "nt":
        pythonw = str(Path(python).with_name("pythonw.exe"))
        if Path(pythonw).exists():
            python = pythonw

    popen_kwargs = {
        "cwd": str(BASE_DIR),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        popen_kwargs["start_new_session"] = True

    try:
        subprocess.Popen([python, str(DAEMON_PATH)], **popen_kwargs)
    except OSError as exc:
        print(f"[Bot] Не удалось запустить демон: {exc}")
        _write_status("error", str(exc))
        return

    _write_status("starting", "Бот запускается…")
    print("[Bot] Демон бота запущен.")


def run() -> None:
    """Точка входа модуля. Переключает демон: вкл/выкл по текущему состоянию."""
    print("[Bot] Started execution...")

    if not DAEMON_PATH.exists():
        print(f"[Bot] Ошибка: демон не найден: {DAEMON_PATH}")
        return

    if is_running():
        stop()
    else:
        start()

    print("[Bot] Finished successfully.")


if __name__ == "__main__":
    run()
