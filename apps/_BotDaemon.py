"""Фоновый демон Telegram-бота (запускается модулем Bot.py как отдельный процесс).

Демон не отображается в лаунчере (имя файла начинается с "_"), работает в
собственном процессе и продолжает работу после закрытия лаунчера. Данные
хранятся в data/inventory.db, состояние — в data/bot_status.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp

BASE_DIR = Path(__file__).resolve().parent.parent
APPS_DIR = BASE_DIR / "apps"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "inventory.db"
STATUS_PATH = DATA_DIR / "bot_status.json"
LOCK_PATH = DATA_DIR / "bot_daemon.lock"
STATE_PATH = DATA_DIR / "bot_state.json"
LOG_DIR = DATA_DIR / "logs"
LOG_PATH = LOG_DIR / "bot.log"

# Разрешаем импорт DBase из apps/.
for _p in (str(BASE_DIR), str(APPS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import DBase  # noqa: E402

def _configure_logging() -> None:
    """Настраивает логирование демона.

    Логи пишутся в data/logs/bot.log: по одному файлу на час
    (TimedRotatingFileHandler), хранятся последние сутки (24 файла).
    Папка data/ исключена из Git, поэтому логи не попадают в репозиторий.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Файл: ротация каждый час, храним 23 резервных файла + текущий = сутки.
    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_PATH,
        when="H",
        interval=1,
        backupCount=23,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Консоль: полезно при ручном запуске демона в терминале.
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


logger = logging.getLogger("BotDaemon")
_configure_logging()


# ---------------------------------------------------------------------------
# Конфигурация из .env
# ---------------------------------------------------------------------------
def _env(key: str, default: str = "") -> str:
    value = DBase.read_env_value(key)
    return value if value else default


TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
YOUR_TELEGRAM_ID = _env("YOUR_TELEGRAM_ID")
TELEGRAM_GROUP_ID = _env("TELEGRAM_GROUP_ID")
TELEGRAM_ORDERS_GROUP_ID = _env("TELEGRAM_ORDERS_GROUP_ID")
TELEGRAM_CANCELS_GROUP_ID = _env("TELEGRAM_CANCELS_GROUP_ID")
TELEGRAM_ORDER_BUTTON_GROUP_ID = _env("TELEGRAM_ORDER_BUTTON_GROUP_ID") or TELEGRAM_ORDERS_GROUP_ID
OZON_ENABLED = _env("OZON_ENABLED", "false").lower() == "true"
NOTIFICATIONS_ENABLED = _env("NOTIFICATIONS_ENABLED", "true").lower() == "true"

WB_CHECK_INTERVAL = int(_env("WB_CHECK_INTERVAL", "5") or 5)
OZON_DELAY_AFTER_WB = int(_env("OZON_DELAY_AFTER_WB", "30") or 30)
ORDERS_HISTORY_DAYS = int(_env("ORDERS_HISTORY_DAYS", "35") or 35)

WB_WAREHOUSE_ID = (DBase.get_wb_warehouse_ids() or [None])[0]
OZON_WAREHOUSE_ID = (DBase.get_oz_warehouse_ids() or [None])[0]
OZON_CLIENT_ID = DBase.get_oz_client_id()

# Статусы wbStatus, означающие "товар уже прошёл сортировку" (вариант 2).
SORTED_SEEN_STATUSES = {
    "sorted", "ready_for_pickup", "accepted_by_carrier",
    "sent_to_carrier", "sold", "canceled_by_carrier",
}
# Отмена: wbStatus в CANCEL_WB_STATUSES + supplierStatus в
# CANCEL_SUPPLIER_STATUSES, при условии что saw_sorted == 0.
CANCEL_WB_STATUSES = {"canceled_by_client", "declined_by_client"}
CANCEL_SUPPLIER_STATUSES = {"new", "confirm", "complete"}

# Маппинг «сочетание статусов → уникальное сообщение об отмене».
_STAGE_LABEL = {
    "new": "нового",
    "confirm": "собранного",
    "complete": "отправленного",
}
_REASON_LABEL = {
    "declined_by_client": "покупатель отказался в первый час",
    "canceled_by_client": "покупатель отказался при получении",
    "canceled": "сборочное задание отменено",
    "defect": "брак",
    "canceled_by_carrier": "перевозчик отменил заказ",
}


def cancel_message(supplier_status, wb_status):
    """Возвращает уникальное сообщение об отмене или None, если это не отмена."""
    if supplier_status == "cancel":
        return "Отмена заказа продавцом"
    if supplier_status == "cancel_carrier":
        return "Отмена заказа перевозчиком (трансграничная)"
    stage = _STAGE_LABEL.get(supplier_status)
    reason = _REASON_LABEL.get(wb_status)
    if stage and reason:
        return f"Отмена {stage} заказа — {reason}"
    if reason:
        return f"Отмена заказа — {reason}"
    return None

def copyable_article(article) -> str:
    """Возвращает артикул для HTML-сообщения Telegram.

    В тег <code> оборачивается только цифровая часть артикула до знака «-»,
    поэтому при нажатии на неё копируется именно это значение, а суффикс
    (например, «-АК») остаётся обычным текстом рядом с ней.
    """
    article = str(article or "").strip()
    if not article:
        return ""
    prefix = article.split("-", 1)[0].strip()
    if not prefix:
        return article
    suffix = article[len(prefix):]
    return f"<code>{prefix}</code>{suffix}"



WB_API_BASE = "https://marketplace-api.wildberries.ru"
OZ_API_BASE = "https://api-seller.ozon.ru"
TG_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# ---------------------------------------------------------------------------
# Служебные файлы (lock + статус)
# ---------------------------------------------------------------------------
def _pid_alive(pid) -> bool:
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


def write_status(state: str, message: str = "") -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with STATUS_PATH.open("w", encoding="utf-8") as file:
            json.dump(
                {
                    "pid": os.getpid(),
                    "state": state,
                    "message": message,
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "last_heartbeat": time.time(),
                },
                file,
                ensure_ascii=False,
                indent=2,
            )
    except OSError as exc:
        logger.warning("Не удалось записать статус: %s", exc)


def update_heartbeat() -> None:
    try:
        if STATUS_PATH.exists():
            with STATUS_PATH.open("r", encoding="utf-8") as file:
                data = json.load(file)
        else:
            data = {}
        data["pid"] = os.getpid()
        data["state"] = data.get("state", "running")
        data["last_heartbeat"] = time.time()
        with STATUS_PATH.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
    except (OSError, json.JSONDecodeError):
        pass


def acquire_lock() -> bool:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            old_pid = 0
        if old_pid and _pid_alive(old_pid):
            logger.warning("Демон уже запущен (PID %s). Выход.", old_pid)
            return False
    try:
        LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return False


def release_lock() -> None:
    try:
        if LOCK_PATH.exists():
            LOCK_PATH.unlink()
    except OSError:
        pass


def _read_state() -> dict:
    """Читает data/bot_state.json (пустой словарь при отсутствии/ошибке)."""
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _write_state(updates: dict) -> None:
    """Сливает переданные поля с текущим состоянием и сохраняет в файл."""
    try:
        state = _read_state()
        state.update(updates)
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def load_last_reconcile() -> int | None:
    return _read_state().get("last_reconcile_at")


def save_last_reconcile(ts: int) -> None:
    _write_state({"last_reconcile_at": ts})


def load_telegram_offset() -> int:
    """Возвращает сохранённый offset Telegram-обновлений (0 при первом запуске)."""
    value = _read_state().get("telegram_offset")
    return int(value) if isinstance(value, (int, float)) else 0


def save_telegram_offset(offset: int) -> None:
    _write_state({"telegram_offset": int(offset)})


# ---------------------------------------------------------------------------
# Работа с БД
# ---------------------------------------------------------------------------
# Wildberries charcID характеристик книг (значения лежат в wb_product_values):
# тип обложки и количество страниц.
COVER_TYPE_CHARC_ID = 1185
PAGE_COUNT_CHARC_ID = 90633


def db_connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=30)


def first_photo_url(raw) -> str | None:
    """Извлекает первый URL фото из JSON (структура фото WB)."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(data, list):
        for item in data:
            url = first_photo_url(json.dumps(item, ensure_ascii=False))
            if url:
                return url
        return None
    if isinstance(data, dict):
        for key in ("big", "c246x328", "small", "tm", "c516x688"):
            if data.get(key):
                return data[key]
        for value in data.values():
            if isinstance(value, str) and value.startswith("http"):
                return value
        return None
    if isinstance(data, str) and data.startswith("http"):
        return data
    return None


def get_card(article: str) -> dict:
    """Возвращает данные карточки WB по артикулу (vendorCode).

    Словарь: {title, chrtId, photo_url, cover_type, page_count}.
    cover_type — тип обложки, page_count — количество страниц.
    """
    art, sup = DBase.extract_art_sup(article)
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT title, chrtID FROM wb_products WHERE vendorCode = ? LIMIT 1",
            (article,),
        ).fetchone()

        title = row[0] if row else ""
        chrt_id = None
        if row and row[1]:
            first = str(row[1]).split(",")[0].strip()
            try:
                chrt_id = int(first)
            except ValueError:
                chrt_id = None

        cover_type = ""
        page_count = ""
        photo_url = None
        if art is not None:
            def char_value(charc_id: int) -> str:
                """Значение характеристики карточки по charcID (или пустая строка)."""
                if sup is None:
                    crow = conn.execute(
                        "SELECT value FROM wb_product_values "
                        "WHERE ART=? AND charcID=? AND SUP IS NULL LIMIT 1",
                        (art, charc_id),
                    ).fetchone()
                else:
                    crow = conn.execute(
                        "SELECT value FROM wb_product_values "
                        "WHERE ART=? AND SUP=? AND charcID=? LIMIT 1",
                        (art, sup, charc_id),
                    ).fetchone()
                return (crow[0] or "").strip() if crow else ""

            cover_type = char_value(COVER_TYPE_CHARC_ID)
            page_count = char_value(PAGE_COUNT_CHARC_ID)

            if sup is None:
                prow = conn.execute(
                    "SELECT value FROM wb_product_values "
                    "WHERE ART=? AND field_name='photos' AND SUP IS NULL LIMIT 1",
                    (art,),
                ).fetchone()
            else:
                prow = conn.execute(
                    "SELECT value FROM wb_product_values "
                    "WHERE ART=? AND SUP=? AND field_name='photos' LIMIT 1",
                    (art, sup),
                ).fetchone()
            if prow:
                photo_url = first_photo_url(prow[0])

        return {
            "title": title,
            "chrtId": chrt_id,
            "photo_url": photo_url,
            "cover_type": cover_type,
            "page_count": page_count,
        }
    finally:
        conn.close()


def oz_offer_exists(offer_id: str) -> bool:
    """True, если товар с таким offer_id есть в активных карточках Ozon."""
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM oz_products WHERE offer_id = ? AND is_deleted = 0 LIMIT 1", (offer_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def upsert_wb_order(order: dict) -> None:
    key = order.get("id")
    fields = {k: v for k, v in order.items() if k != "id"}
    conn = db_connect()
    try:
        DBase.upsert_order(conn.cursor(), "wb_orders", "id", key, fields)
        conn.commit()
    finally:
        conn.close()


def upsert_oz_order(order: dict) -> None:
    key = order.get("posting_number")
    fields = {k: v for k, v in order.items() if k != "posting_number"}
    conn = db_connect()
    try:
        DBase.upsert_order(conn.cursor(), "oz_orders", "posting_number", key, fields)
        conn.commit()
    finally:
        conn.close()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_wb_order_row(oid: int) -> dict | None:
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT notified_new, notified_cancel, saw_sorted, is_active, first_seen_at "
            "FROM wb_orders WHERE id = ?",
            (oid,),
        ).fetchone()
        if not row:
            return None
        return dict(zip(
            ["notified_new", "notified_cancel", "saw_sorted", "is_active", "first_seen_at"],
            row,
        ))
    finally:
        conn.close()


def active_wb_ids() -> set:
    conn = db_connect()
    try:
        return {r[0] for r in conn.execute("SELECT id FROM wb_orders WHERE is_active = 1")}
    finally:
        conn.close()


def active_oz_posting_numbers() -> set:
    conn = db_connect()
    try:
        return {r[0] for r in conn.execute(
            "SELECT posting_number FROM oz_orders WHERE is_active = 1"
        )}
    finally:
        conn.close()


def get_oz_order_row(posting_number: str) -> dict | None:
    conn = db_connect()
    try:
        row = conn.execute(
            "SELECT notified_new, is_active, first_seen_at FROM oz_orders "
            "WHERE posting_number = ?",
            (posting_number,),
        ).fetchone()
        if not row:
            return None
        return dict(zip(["notified_new", "is_active", "first_seen_at"], row))
    finally:
        conn.close()


def mark_oz_vanished(posting_number: str) -> None:
    upsert_oz_order({
        "posting_number": posting_number,
        "is_active": 0,
        "history_at": now_str(),
    })



# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
}


def _tg_retry_after(text: str) -> int:
    try:
        data = json.loads(text)
        return int(data.get("parameters", {}).get("retry_after", 0))
    except Exception:  # noqa: BLE001
        return 0


class TelegramClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    def _chat(self, chat_id) -> str:
        return str(chat_id or TELEGRAM_GROUP_ID or YOUR_TELEGRAM_ID or "")

    async def send_message(self, text: str, chat_id=None, reply_markup=None, parse_mode="HTML") -> None:
        if not NOTIFICATIONS_ENABLED:
            return
        chat = self._chat(chat_id)
        if not chat:
            return
        url = f"{TG_API_BASE}/sendMessage"
        payload = {"chat_id": chat, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        for _ in range(3):
            try:
                async with self.session.post(url, json=payload) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(_tg_retry_after(await resp.text()) or 5)
                        continue
                    return
            except aiohttp.ClientError as exc:
                logger.warning("Telegram sendMessage: %s", exc)
                await asyncio.sleep(2)
        return

    async def send_photo(self, caption: str, photo_url: str, chat_id=None, reply_markup=None, parse_mode="HTML") -> None:
        if not NOTIFICATIONS_ENABLED:
            return
        chat = self._chat(chat_id)
        if not chat:
            return
        image = await self._download(photo_url)
        if image is None:
            await self.send_message(caption, chat_id, reply_markup=reply_markup, parse_mode=parse_mode)
            return
        url = f"{TG_API_BASE}/sendPhoto"
        data = aiohttp.FormData()
        data.add_field("chat_id", chat)
        data.add_field("caption", caption)
        if parse_mode:
            data.add_field("parse_mode", parse_mode)
        if reply_markup:
            data.add_field("reply_markup", json.dumps(reply_markup, ensure_ascii=False))
        data.add_field("photo", image, filename="photo.jpg", content_type="image/jpeg")
        for _ in range(3):
            try:
                async with self.session.post(url, data=data) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(_tg_retry_after(await resp.text()) or 5)
                        continue
                    return
            except aiohttp.ClientError as exc:
                logger.warning("Telegram sendPhoto: %s", exc)
                await asyncio.sleep(2)
        return

    async def _download(self, url: str):
        headers = dict(_BROWSER_HEADERS)
        headers["Referer"] = "https://www.wildberries.ru/"
        try:
            async with self.session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass
        return None

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> None:
        """Отвечает на inline-кнопку, чтобы убрать «часики» на кнопке."""
        if not NOTIFICATIONS_ENABLED:
            return
        url = f"{TG_API_BASE}/answerCallbackQuery"
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        for _ in range(3):
            try:
                async with self.session.post(url, json=payload) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(_tg_retry_after(await resp.text()) or 5)
                        continue
                    return
            except aiohttp.ClientError as exc:
                logger.warning("Telegram answerCallbackQuery: %s", exc)
                await asyncio.sleep(2)
        return


# ---------------------------------------------------------------------------
# Базовый API-клиент с circuit breaker (анти-бан)
# ---------------------------------------------------------------------------
class ApiClient:
    """Асинхронный клиент с ретраями и предохранителем.

    При устойчивых ошибках 4xx/5xx открывает circuit breaker на N минут и
    прекращает запросы, чтобы не получить блокировку по IP.
    """

    COOLDOWN_DELAYS_MINUTES = [5, 15, 30, 60]

    def __init__(self, session: aiohttp.ClientSession, base_headers: dict | None = None):
        self.session = session
        self.base_headers = base_headers or {}
        self._circuit_open_until = 0.0
        self._cooldown_stage = 0
        self._error_streak = 0

    def _circuit_is_open(self) -> bool:
        return time.monotonic() < self._circuit_open_until

    def _register_error(self, url: str, status, text: str) -> None:
        self._error_streak += 1
        logger.warning("Ошибка API %s: status=%s %s", url, status, text[:200])
        if self._error_streak >= 3:
            self._error_streak = 0
            delay = self.COOLDOWN_DELAYS_MINUTES[
                min(self._cooldown_stage, len(self.COOLDOWN_DELAYS_MINUTES) - 1)
            ]
            self._cooldown_stage += 1
            self._circuit_open_until = time.monotonic() + delay * 60
            logger.warning("Circuit breaker: пауза %d мин (url=%s)", delay, url)

    def _register_success(self) -> None:
        self._error_streak = 0
        self._cooldown_stage = 0

    async def request(self, method: str, url: str, *, json_body=None, headers=None):
        if self._circuit_is_open():
            return False, None

        merged = dict(self.base_headers)
        if headers:
            merged.update(headers)

        for _ in range(3):
            try:
                async with self.session.request(
                    method, url, headers=merged, json=json_body,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    if resp.status in (200, 204):
                        self._register_success()
                        if resp.status == 204:
                            return True, None
                        try:
                            return True, await resp.json()
                        except Exception:  # noqa: BLE001
                            return True, None
                    text = await resp.text()
                    if resp.status == 429:
                        await asyncio.sleep(5)
                        continue
                    # 4xx жжёт квоту как 10 запросов — не ретраим.
                    self._register_error(url, resp.status, text)
                    return False, None
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("Сетевая ошибка %s: %s", url, exc)
                await asyncio.sleep(3)
                continue

        self._register_error(url, 0, "превышено число попыток")
        return False, None


# ---------------------------------------------------------------------------
# Клиенты маркетплейсов
# ---------------------------------------------------------------------------
class WbClient(ApiClient):
    def __init__(self, session, token):
        super().__init__(session, {"Authorization": token})

    async def get_new_orders(self):
        ok, data = await self.request("GET", f"{WB_API_BASE}/api/v3/orders/new")
        if not ok or not isinstance(data, dict):
            return []
        return data.get("orders") or []

    async def get_orders_status(self, ids: list) -> dict:
        ok, data = await self.request(
            "POST", f"{WB_API_BASE}/api/v3/orders/status", json_body={"orders": list(ids)}
        )
        if not ok or not isinstance(data, dict):
            return {}
        return {o["id"]: o for o in (data.get("orders") or []) if "id" in o}

    async def get_stocks(self, chrt_ids: list) -> dict:
        ok, data = await self.request(
            "POST", f"{WB_API_BASE}/api/v3/stocks/{WB_WAREHOUSE_ID}",
            json_body={"chrtIds": list(chrt_ids)},
        )
        if not ok or not isinstance(data, dict):
            return {}
        return {
            s["chrtId"]: s.get("amount", 0)
            for s in (data.get("stocks") or [])
            if "chrtId" in s
        }

    async def update_stocks(self, stocks: dict) -> bool:
        payload = {"stocks": [{"chrtId": c, "amount": a} for c, a in stocks.items()]}
        ok, _ = await self.request(
            "PUT", f"{WB_API_BASE}/api/v3/stocks/{WB_WAREHOUSE_ID}", json_body=payload
        )
        return ok

    async def get_orders_by_date(self, date_from: int, date_to: int) -> list:
        orders = []
        next_cursor = 0
        while True:
            url = (
                f"{WB_API_BASE}/api/v3/orders?limit=1000&next={next_cursor}"
                f"&dateFrom={date_from}&dateTo={date_to}"
            )
            ok, data = await self.request("GET", url)
            if not ok or not isinstance(data, dict):
                break
            orders.extend(data.get("orders") or [])
            next_cursor = data.get("next", 0)
            if not next_cursor:
                break
        return orders


class OzClient(ApiClient):
    def __init__(self, session, api_key, client_id):
        super().__init__(session, {"Client-Id": client_id, "Api-Key": api_key})

    async def get_unfulfilled_postings(self):
        """Невыполненные FBS-отправления (постраничная выгрузка).

        Схема соответствует v4/posting/fbs/unfulfilled/list: сортировка через
        `sort_dir`, фильтр по `cutoff_from`/`cutoff_to` (окно ±30 дней от
        завтрашнего дня 12:01) и `statuses`, пагинация через `cursor`.
        """

        def _fmt(dt: datetime) -> str:
            return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

        base = (datetime.now() + timedelta(days=1)).replace(
            hour=12, minute=1, second=0, microsecond=0
        )
        cutoff_from = _fmt(base - timedelta(days=30))
        cutoff_to = _fmt(base + timedelta(days=30))

        postings = []
        cursor = ""
        for _ in range(100):  # защита от бесконечного цикла (до 10 000 записей)
            ok, data = await self.request(
                "POST", f"{OZ_API_BASE}/v4/posting/fbs/unfulfilled/list",
                json_body={
                    "sort_dir": "ASC",
                    "limit": 100,
                    "filter": {
                        "cutoff_from": cutoff_from,
                        "cutoff_to": cutoff_to,
                        "statuses": ["awaiting_packaging"],
                    },
                    "cursor": cursor,
                    "with": {
                        "analytics_data": False,
                        "barcodes": False,
                        "financial_data": False,
                        "legal_info": False,
                    },
                },
            )
            if not ok or not isinstance(data, dict):
                break
            page = data.get("postings") or []
            if not page:
                result = data.get("result")
                if isinstance(result, dict):
                    page = result.get("postings") or []
            postings.extend(page)
            if not data.get("has_next"):
                break
            next_cursor = data.get("cursor") or ""
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return postings

    async def update_stocks(self, stocks: dict) -> bool:
        payload = {
            "stocks": [
                {"offer_id": offer_id, "stock": amount, "warehouse_id": OZON_WAREHOUSE_ID}
                for offer_id, amount in stocks.items()
            ]
        }
        ok, _ = await self.request(
            "POST", f"{OZ_API_BASE}/v2/products/stocks", json_body=payload
        )
        return ok

    async def get_stocks(self, offer_ids: list) -> dict:
        """Остатки Ozon FBS: {offer_id: present - reserved} по списку offer_id.

        v4/product/info/stocks; в items[].stocks[] берём запись type == "fbs".
        """
        if not offer_ids:
            return {}
        ok, data = await self.request(
            "POST",
            f"{OZ_API_BASE}/v4/product/info/stocks",
            json_body={
                "cursor": "",
                "filter": {"offer_id": list(offer_ids), "visibility": "ALL"},
                "limit": 1000,
            },
        )
        if not ok or not isinstance(data, dict):
            return {}
        result = {}
        for item in data.get("items") or []:
            oid = item.get("offer_id")
            if not oid:
                continue
            for stock in item.get("stocks") or []:
                if stock.get("type") != "fbs":
                    continue
                try:
                    present = int(stock.get("present", 0))
                    reserved = int(stock.get("reserved", 0))
                except (TypeError, ValueError):
                    continue
                result[oid] = present - reserved
                break
        return result


# ---------------------------------------------------------------------------
# Оркестрация бота
# ---------------------------------------------------------------------------
class Bot:
    def __init__(self, session, tg, wb, oz):
        self.session = session
        self.tg = tg
        self.wb = wb
        self.oz = oz

    @staticmethod
    def _price(converted_price) -> str:
        if converted_price:
            return f"\n💰 Цена: {converted_price / 100:.2f} ₽"
        return ""

    async def _notify_new_wb(self, order: dict) -> None:
        cover_type = order.get("cover_type", "")
        page_count = order.get("page_count", "")

        if cover_type and page_count:
            spec = f"{cover_type},{page_count} стр"
        elif cover_type:
            spec = cover_type
        elif page_count:
            spec = f"{page_count} стр"
        else:
            spec = ""

        msg = f"Новый заказ ВБ💜№ {order['id']}\nАрт. {copyable_article(order['article'])}"
        name = order.get("name", "")
        if name:
            msg += f"\n{name}"
        if spec:
            msg += f"\n📖{spec}"
        msg += self._price(order.get("convertedPrice"))
        stock = order.get("stock")
        if stock is not None:
            msg += f"\nОстаток: {stock} шт"

        if order.get("photo_url"):
            await self.tg.send_photo(msg, order["photo_url"], chat_id=TELEGRAM_ORDERS_GROUP_ID)
        else:
            await self.tg.send_message(msg, chat_id=TELEGRAM_ORDERS_GROUP_ID)

        # Дублируем уведомление в личный чат владельца с кнопкой «Заказать».
        if YOUR_TELEGRAM_ID:
            keyboard = {
                "inline_keyboard": [
                    [{"text": "Заказать", "callback_data": f"order:{order['id']}"}]
                ]
            }
            if order.get("photo_url"):
                await self.tg.send_photo(
                    msg, order["photo_url"], chat_id=YOUR_TELEGRAM_ID, reply_markup=keyboard
                )
            else:
                await self.tg.send_message(
                    msg, chat_id=YOUR_TELEGRAM_ID, reply_markup=keyboard
                )

    async def check_wb(self) -> None:
        await self.reconcile_orders()
        raw_orders = await self.wb.get_new_orders()
        now = now_str()
        current_ids = []
        new_orders = []

        for raw in raw_orders:
            oid = raw.get("id")
            if oid is None:
                continue
            oid = int(oid)
            current_ids.append(oid)

            article = raw.get("article", "")
            card = get_card(article)
            wb_status = raw.get("wbStatus")
            chrt_id = raw.get("chrtId") or card.get("chrtId")

            row = get_wb_order_row(oid)
            is_new_order = row is None or not row["notified_new"]

            fields = {
                "article": article,
                "nmId": raw.get("nmId"),
                "chrtId": chrt_id,
                "supplier_status": raw.get("supplierStatus"),
                "wb_status": wb_status,
                "is_cancellable": 1 if raw.get("isCancellable") else 0,
                "is_active": 1,
                "last_seen_at": now,
            }
            if row is None:
                fields.update(
                    first_seen_at=now, notified_new=0, notified_cancel=0, saw_sorted=0
                )
            else:
                fields["first_seen_at"] = row["first_seen_at"] or now

            if wb_status in SORTED_SEEN_STATUSES:
                fields["saw_sorted"] = 1

            upsert_wb_order({**fields, "id": oid})

            if is_new_order:
                new_orders.append({
                    "id": oid,
                    "article": article,
                    "name": card.get("title", ""),
                    "chrtId": chrt_id,
                    "convertedPrice": raw.get("convertedPrice"),
                    "photo_url": card.get("photo_url"),
                    "cover_type": card.get("cover_type", ""),
                    "page_count": card.get("page_count", ""),
                })

        stocks = {}
        if new_orders:
            chrt_ids = [o["chrtId"] for o in new_orders if o["chrtId"]]
            if chrt_ids:
                stocks = await self.wb.get_stocks(chrt_ids)

        for order in new_orders:
            order["stock"] = stocks.get(order["chrtId"]) if order["chrtId"] else None
            await self._notify_new_wb(order)
            upsert_wb_order({"id": order["id"], "notified_new": 1})

        if new_orders:
            await self._sync_stocks_wb_to_oz(new_orders, stocks)

        vanished = active_wb_ids() - set(current_ids)
        if vanished:
            await self._handle_vanished(vanished)

    async def _sync_stocks_wb_to_oz(self, new_orders: list, stocks: dict | None = None) -> None:
        chrt_ids = [o["chrtId"] for o in new_orders if o["chrtId"]]
        if not chrt_ids:
            return
        if stocks is None:
            stocks = await self.wb.get_stocks(chrt_ids)

        oz_stocks = {}
        for o in new_orders:
            chrt_id = o["chrtId"]
            if not chrt_id or chrt_id not in stocks:
                continue
            amount = stocks[chrt_id]

            if amount <= 0:
                msg = f"{o['name']} {copyable_article(o['article'])} Закончился на ВБ💜"
                if o.get("photo_url"):
                    await self.tg.send_photo(msg, o["photo_url"], chat_id=TELEGRAM_GROUP_ID)
                else:
                    await self.tg.send_message(msg, chat_id=TELEGRAM_GROUP_ID)

            # Синхронизация на Ozon — только если товар есть на Ozon.
            if OZON_ENABLED and oz_offer_exists(o["article"]):
                oz_stocks[o["article"]] = amount

        if oz_stocks:
            ok = await self.oz.update_stocks(oz_stocks)
            if ok:
                logger.info("Остатки Ozon обновлены для %d товаров", len(oz_stocks))

    async def _handle_vanished(self, vanished: set) -> None:
        statuses = await self.wb.get_orders_status(list(vanished))
        for oid in vanished:
            st = statuses.get(oid) or {}
            supplier_status = st.get("supplierStatus")
            wb_status = st.get("wbStatus")

            fields = {
                "supplier_status": supplier_status,
                "wb_status": wb_status,
                "is_cancellable": 1 if st.get("isCancellable") else 0,
                "is_active": 0,
                "history_at": now_str(),
            }
            if wb_status in SORTED_SEEN_STATUSES:
                fields["saw_sorted"] = 1
            upsert_wb_order({**fields, "id": oid})

            cancel_text = cancel_message(supplier_status, wb_status)
            if not cancel_text:
                continue
            row = get_wb_order_row(oid)
            if not row or row["notified_cancel"]:
                continue
            article = self._wb_article(oid)
            card = get_card(article) if article else {}
            await self._notify_cancel(oid, article, card, cancel_text)
            upsert_wb_order({"id": oid, "notified_cancel": 1})

    async def _notify_cancel(self, oid, article, card, cancel_text) -> None:
        card = card or {}
        title = card.get("title") or ""
        article = article or ""
        art_html = copyable_article(article)
        if title:
            msg = f"{cancel_text} 💔\nЗаказ {oid} {title} {art_html}"
        else:
            msg = f"{cancel_text} 💔\nЗаказ {oid} {art_html}"
        if card.get("photo_url"):
            await self.tg.send_photo(msg, card["photo_url"], chat_id=TELEGRAM_CANCELS_GROUP_ID)
        else:
            await self.tg.send_message(msg, chat_id=TELEGRAM_CANCELS_GROUP_ID)

    async def reconcile_orders(self) -> None:
        """Ловит отмены, случившиеся пока бот был выключен (сверка по /orders)."""
        now = int(time.time())
        last = load_last_reconcile() or (now - 24 * 3600)
        orders = await self.wb.get_orders_by_date(last, now)
        save_last_reconcile(now)

        # GET /api/v3/orders не возвращает supplierStatus/wbStatus, поэтому
        # статусы дозапрашиваются отдельным батч-запросом /orders/status.
        ids = [int(o["id"]) for o in orders if o.get("id") is not None]
        statuses = {}
        for start in range(0, len(ids), 1000):
            statuses.update(await self.wb.get_orders_status(ids[start:start + 1000]))

        for o in orders:
            oid = o.get("id")
            if oid is None:
                continue
            oid = int(oid)
            st = statuses.get(oid) or {}
            supplier_status = st.get("supplierStatus")
            wb_status = st.get("wbStatus")
            cancel_text = cancel_message(supplier_status, wb_status)
            if not cancel_text:
                continue
            row = get_wb_order_row(oid)
            if row and row["notified_cancel"]:
                continue
            article = o.get("article", "")
            upsert_wb_order({
                "id": oid,
                "article": article,
                "nmId": o.get("nmId"),
                "chrtId": o.get("chrtId"),
                "supplier_status": supplier_status,
                "wb_status": wb_status,
                "is_cancellable": 1 if st.get("isCancellable") else 0,
                "is_active": 0,
                "history_at": now_str(),
            })
            card = get_card(article) if article else {}
            await self._notify_cancel(oid, article, card, cancel_text)
            upsert_wb_order({"id": oid, "notified_cancel": 1})

    @staticmethod
    def _wb_article(oid: int) -> str:
        conn = db_connect()
        try:
            row = conn.execute("SELECT article FROM wb_orders WHERE id = ?", (oid,)).fetchone()
            return row[0] if row else ""
        finally:
            conn.close()

    async def check_oz(self) -> None:
        if not OZON_ENABLED:
            return
        postings = await self.oz.get_unfulfilled_postings()
        current = []
        new_postings = []

        for posting in postings:
            posting_number = posting.get("posting_number")
            if not posting_number:
                continue
            current.append(posting_number)
            products = posting.get("products") or []
            offer_id = products[0].get("offer_id") if products else posting.get("offer_id")

            row = get_oz_order_row(posting_number)
            is_new = row is None or not row["notified_new"]

            fields = {
                "offer_id": offer_id,
                "status": posting.get("status"),
                "products": products,
                "is_active": 1,
                "last_seen_at": now_str(),
            }
            if row is None:
                fields["first_seen_at"] = now_str()
                fields["notified_new"] = 0
            upsert_oz_order({**fields, "posting_number": posting_number})

            if is_new:
                new_postings.append({
                    "posting_number": posting_number,
                    "offer_id": offer_id,
                    "products": products,
                })

        for p in new_postings:
            wb_stock = await self._reduce_wb_stock(p)
            upsert_oz_order({"posting_number": p["posting_number"], "notified_new": 1})
            await self._notify_new_oz(p, wb_stock)

        vanished = active_oz_posting_numbers() - set(current)
        for pn in vanished:
            mark_oz_vanished(pn)

    async def _oz_stock_line(self, offer_id: str, wb_stock: int | None = None) -> str:
        """Формирует строку остатков для Ozon-заказа: 'Остаток: Oz - Xшт, Wb - Yшт'.

        wb_stock берётся уже вычисленным после списания (WB не перечитываем,
        чтобы не получить устаревшее значение из-за асинхронного применения PUT).
        """
        if not offer_id:
            return ""

        oz_stock = None
        oz_stocks = await self.oz.get_stocks([offer_id])
        oz_stock = oz_stocks.get(offer_id)

        parts = []
        if oz_stock is not None:
            parts.append(f"Oz - {oz_stock}шт")
        if wb_stock is not None:
            parts.append(f"Wb - {wb_stock}шт")
        if not parts:
            return ""
        return "Остаток: " + ", ".join(parts)

    async def _notify_new_oz(self, p: dict, wb_stock: int | None = None) -> None:
        offer_id = p.get("offer_id") or ""
        products = p.get("products") or []
        name = products[0].get("name", "") if products else ""
        qty = products[0].get("quantity", 1) if products else 1

        card = get_card(offer_id) if offer_id else {}
        photo = card.get("photo_url")

        msg = f"Новый заказ OZ💙:\n{p['posting_number']} {name} <code>{offer_id}</code> ({qty} шт)"
        stock_line = await self._oz_stock_line(offer_id, wb_stock)
        if stock_line:
            msg += f"\n{stock_line}"

        if photo:
            await self.tg.send_photo(msg, photo, chat_id=TELEGRAM_ORDERS_GROUP_ID)
        else:
            await self.tg.send_message(msg, chat_id=TELEGRAM_ORDERS_GROUP_ID)

    async def _reduce_wb_stock(self, p: dict) -> int | None:
        """Списывает остаток WB на количество заказанных товаров Ozon.

        Возвращает новый остаток WB (после списания) или None, если списание
        не выполнено (нет offer_id/chrtId или запрос обновления не удался).
        """
        offer_id = p.get("offer_id") or ""
        if not offer_id:
            return None
        card = get_card(offer_id)
        chrt_id = card.get("chrtId")
        if not chrt_id:
            return None
        products = p.get("products") or []
        qty = sum(prod.get("quantity", 0) for prod in products) or 1
        stocks = await self.wb.get_stocks([chrt_id])
        current_stock = stocks.get(chrt_id, 0)
        new_stock = max(0, current_stock - qty)
        ok = await self.wb.update_stocks({chrt_id: new_stock})
        if ok and new_stock <= 0:
            msg = f"{card.get('title', '')} {copyable_article(offer_id)} Закончился на ВБ💜"
            if card.get("photo_url"):
                await self.tg.send_photo(msg, card["photo_url"], chat_id=TELEGRAM_GROUP_ID)
            else:
                await self.tg.send_message(msg, chat_id=TELEGRAM_GROUP_ID)
        return new_stock if ok else None


# ---------------------------------------------------------------------------
# Циклы и точка входа
# ---------------------------------------------------------------------------
async def heartbeat_loop() -> None:
    while True:
        update_heartbeat()
        await asyncio.sleep(5)


async def order_loop(bot: Bot, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await bot.check_wb()
            logger.info("Проверка WB завершена")
        except Exception as exc:  # noqa: BLE001
            logger.error("check_wb: %s", exc)
            await bot.tg.send_message(f"Ошибка проверки WB: {exc}", chat_id=YOUR_TELEGRAM_ID)

        if stop_event.is_set():
            break
        await asyncio.sleep(WB_CHECK_INTERVAL * 60)

        if OZON_ENABLED and not stop_event.is_set():
            await asyncio.sleep(OZON_DELAY_AFTER_WB)
            try:
                await bot.check_oz()
                logger.info("Проверка OZ завершена")
            except Exception as exc:  # noqa: BLE001
                logger.error("check_oz: %s", exc)
                await bot.tg.send_message(f"Ошибка проверки OZ: {exc}", chat_id=YOUR_TELEGRAM_ID)


async def status_text() -> str:
    conn = db_connect()
    try:
        wb_new = conn.execute("SELECT COUNT(*) FROM wb_orders WHERE is_active=1").fetchone()[0]
        wb_hist = conn.execute("SELECT COUNT(*) FROM wb_orders WHERE is_active=0").fetchone()[0]
        oz = conn.execute("SELECT COUNT(*) FROM oz_orders WHERE is_active=1").fetchone()[0]
    finally:
        conn.close()
    return (
        "📊 <b>СТАТУС</b>\n\n"
        f"🟣 WB активных: {wb_new}\n"
        f"📜 WB в истории: {wb_hist}\n"
        f"🔵 OZ активных: {oz}\n"
        f"Ozon: {'вкл' if OZON_ENABLED else 'выкл'}"
    )


async def handle_command(bot: Bot, chat_id, text: str, stop_event: asyncio.Event) -> None:
    cmd = text.strip().lower().split()[0]
    if YOUR_TELEGRAM_ID and str(chat_id) != str(YOUR_TELEGRAM_ID):
        return
    if cmd in ("/start", "/status"):
        await bot.tg.send_message(await status_text(), chat_id=YOUR_TELEGRAM_ID)
    elif cmd == "/stop":
        await bot.tg.send_message("Останавливаю бота…", chat_id=YOUR_TELEGRAM_ID)
        stop_event.set()


async def handle_callback(bot: Bot, callback_query: dict) -> None:
    """Обрабатывает нажатия inline-кнопок Telegram (кнопка «Заказать»)."""
    cq_id = callback_query.get("id")
    if not cq_id:
        return

    data = callback_query.get("data") or ""
    from_id = callback_query.get("from", {}).get("id")
    logger.info("Callback получен: data=%r from=%s", data, from_id)

    # Кнопки нажимает только владелец бота.
    if YOUR_TELEGRAM_ID and str(from_id) != str(YOUR_TELEGRAM_ID):
        await bot.tg.answer_callback(cq_id)
        return

    if data.startswith("order:"):
        try:
            oid = int(data.split(":", 1)[1])
        except ValueError:
            await bot.tg.answer_callback(cq_id)
            return

        article = Bot._wb_article(oid)
        if not article:
            await bot.tg.answer_callback(cq_id, "Артикул не найден")
            return

        card = get_card(article)
        title = (card.get("title") or "").strip()
        msg = f"Арт. {article}"
        if title:
            msg += f"\n{title}"

        logger.info("Отправка заказа oid=%s в группу %s", oid, TELEGRAM_ORDER_BUTTON_GROUP_ID)
        if card.get("photo_url"):
            await bot.tg.send_photo(
                msg, card["photo_url"],
                chat_id=TELEGRAM_ORDER_BUTTON_GROUP_ID,
                parse_mode=None,
            )
        else:
            await bot.tg.send_message(
                msg, chat_id=TELEGRAM_ORDER_BUTTON_GROUP_ID, parse_mode=None
            )
        await bot.tg.answer_callback(cq_id, "Отправлено в группу")
        return

    await bot.tg.answer_callback(cq_id)


async def telegram_poll(bot: Bot, stop_event: asyncio.Event) -> None:
    offset = load_telegram_offset()
    while not stop_event.is_set():
        try:
            url = f"{TG_API_BASE}/getUpdates"
            async with bot.session.get(url, params={"timeout": 30, "offset": offset}) as resp:
                if resp.status != 200:
                    await asyncio.sleep(5)
                    continue
                data = await resp.json()
            for update in data.get("result") or []:
                offset = update["update_id"] + 1
                save_telegram_offset(offset)
                callback = update.get("callback_query")
                if callback:
                    await handle_callback(bot, callback)
                    continue
                msg = update.get("message") or {}
                text = msg.get("text") or ""
                chat_id = msg.get("chat", {}).get("id")
                if text:
                    await handle_command(bot, chat_id, text, stop_event)
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Telegram polling: %s", exc)
            await asyncio.sleep(5)


async def run_daemon() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("Нет TELEGRAM_BOT_TOKEN в .env")
        write_status("error", "Нет TELEGRAM_BOT_TOKEN")
        return
    if not acquire_lock():
        return

    wb_token = DBase.get_wb_token("MASTER")
    if not wb_token:
        logger.error("Нет WB токена в .env")
        write_status("error", "Нет WB токена")
        release_lock()
        return

    write_status("running", "Бот работает")
    logger.info(
        "Демон бота запущен (PID %s). WB-интервал: %s мин, Ozon: %s",
        os.getpid(),
        WB_CHECK_INTERVAL,
        "вкл" if OZON_ENABLED else "выкл",
    )

    async with aiohttp.ClientSession() as session:
        tg = TelegramClient(session)
        wb = WbClient(session, wb_token)
        oz = OzClient(session, DBase.get_oz_token(), OZON_CLIENT_ID or "")
        bot = Bot(session, tg, wb, oz)

        stop_event = asyncio.Event()
        tasks = [
            asyncio.create_task(order_loop(bot, stop_event)),
            asyncio.create_task(telegram_poll(bot, stop_event)),
            asyncio.create_task(heartbeat_loop()),
        ]
        try:
            await stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    write_status("stopped", "Бот остановлен")
    release_lock()


if __name__ == "__main__":
    try:
        asyncio.run(run_daemon())
    except KeyboardInterrupt:
        pass








