"""Модуль CardsCreator — пошаговое создание и редактирование карточек товаров
на маркетплейсах Wildberries и Ozon.

Реализована ПОДГОТОВКА БАЗЫ (фундамента) модуля:

  1. Окно ввода штрихкода/артикула: сканер / вставка из буфера обмена
     (Ctrl+V или Ctrl+М в любой раскладке, кнопка «Вставить») / ручной ввод;
     поиск запускается по Enter или кнопке «Далее».

  2. Поиск по ART в локальной базе data/inventory.db (wb_products, oz_products).

  3. Список найденных товаров (одинаковый ART, но разные SUP и/или площадки
     WB/Ozon) с кнопкой «Новый поставщик». Выбор существующего товара = режим
     редактирования карточки (будет реализован на следующем этапе).

  4. Новый товар (ART не найден в БД): показывается окно «Выбор категории».
     Список категорий (предметов Wildberries) читается из wb_products вместе
     с количеством товаров; по умолчанию сортируется по количеству (больше
     товаров — выше), с переключателем сортировки по алфавиту.

  5. После выбора категории открывается экран подготовки фото. Фотографии
     выбираются из папки data/PhotosDrop и показываются сеткой по 3 в ряд с
     превью-миниатюрами; для каждой — переключатель «Главная» (ровно одна) и
     галочка «Анализ» (для DeepSeek, по умолчанию снята). Конвертация НЕ
     выполняется: все выбранные фото (главное, «на анализ» и остальные)
     закрепляются за ART под именами <ART>_1, <ART>_2, … и сохраняются
     черновиком в data/cards_creator_drafts.json. Распознавание характеристик —
     кнопка «Анализ DeepSeek».

  6. Режим «Новый поставщик» для уже существующего ART ведёт на тот же экран
     подготовки фото, но без предварительного выбора категории.

Токен DeepSeek хранится в .env (ключ DEEPSEEK_TOKEN) и редактируется в окне
«⚙️ DeepSeek». Кнопка «Анализ DeepSeek» отправляет фото (с пометкой «Анализ») на
распознавание в модель deepseek-flash (POST /chat/completions) с system_prompt из
таблицы БД «promts» (промт extract:standard). Одновременно по тем же фото вторым
параллельным запросом генерируется «Описание» (промт description:photo). Ответ
парсится и заполняет окно «Сверка характеристик» с уже подставленным описанием,
где есть отладочный просмотр промта и сырого ответа.

Модуль НЕ изменяет схему БД (это разрешено только apps/DBase.py) и НЕ
обновляет каталог — он читает уже выгруженные таблицы. Перед поиском
актуальность базы обеспечивает модуль DBase.
"""

import base64
import concurrent.futures
import copy
import json
import logging
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox

import requests
import customtkinter as ctk

# ---------------------------------------------------------------------------
# Пути проекта (определяются до импорта apps.DBase, чтобы работал и прямой
# запуск "python apps/CardsCreator.py" без лаунчера).
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "inventory.db")
PHOTOS_DROP_DIR = os.path.join(DATA_DIR, "PhotosDrop")       # исходные фото
DRAFTS_PATH = os.path.join(DATA_DIR, "cards_creator_drafts.json")
# Временное хранение цены/себестоимости для карточек, которых ещё нет в БД:
# записывается при создании карточки и переносится в wb_products, когда карточка
# появится в базе (см. _flush_pending_costs).
PENDING_COSTS_PATH = os.path.join(DATA_DIR, "pending_card_costs.json")

if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import apps.DBase as DBase  # noqa: E402 — импорт после настройки sys.path

_logger = logging.getLogger("CardsCreator")

# ---------------------------------------------------------------------------
# Справочник брендов (таблица brands) — чтение/запись строк.
# Схема принадлежит apps/DBase.py, здесь только работа с данными.
# ---------------------------------------------------------------------------
def _normalize_brand(value) -> str:
    """Нормализует значение бренда для сравнения: без учёта регистра и лишних пробелов."""
    return " ".join(str(value or "").split()).casefold()


def _dedupe_brand_aliases(aliases) -> list:
    """Убирает пустые значения и дубликаты из списка поисковых алиасов (без учёта регистра)."""
    result = []
    seen = set()
    for alias in aliases:
        alias = str(alias or "").strip()
        key = _normalize_brand(alias)
        if not alias or key in seen:
            continue
        seen.add(key)
        result.append(alias)
    return result


def _parse_brand_aliases(raw) -> list:
    """Разбирает search_aliases в список строк.

    Основной формат — JSON-массив. Для обратной совместимости поддерживается
    и обычный текст с разделителями (запятая / точка с запятой / перенос строки).
    """
    if not raw:
        return []
    raw = str(raw).strip()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return _dedupe_brand_aliases(data)
        except ValueError:
            pass
    return _dedupe_brand_aliases(re.split(r"[,;\n]+", raw))


def search_brand_by_alias(value):
    """Ищет бренд по значению, извлечённому DeepSeek (без учёта регистра).

    Сопоставление сначала по search_aliases, затем по wb_name/oz_name/legal_name
    (на случай, если распознано точное имя площадки). Возвращает словарь строки
    таблицы brands или None.
    """
    needle = _normalize_brand(value)
    if not needle:
        return None

    connection = sqlite3.connect(DB_PATH)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM brands").fetchall()
    finally:
        connection.close()

    for row in rows:
        aliases = _parse_brand_aliases(row["search_aliases"])
        if any(_normalize_brand(alias) == needle for alias in aliases):
            return dict(row)
    for row in rows:
        if any(
            _normalize_brand(row[col]) == needle
            for col in ("wb_name", "oz_name", "legal_name")
        ):
            return dict(row)
    return None


def find_brand_by_names(wb_name=None, oz_name=None):
    """Ищет существующую строку бренда по имени WB и/или Ozon (без учёта регистра)."""
    wb = _normalize_brand(wb_name)
    oz = _normalize_brand(oz_name)
    if not wb and not oz:
        return None

    connection = sqlite3.connect(DB_PATH)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM brands").fetchall()
    finally:
        connection.close()

    for row in rows:
        if wb and _normalize_brand(row["wb_name"]) == wb:
            return dict(row)
        if oz and _normalize_brand(row["oz_name"]) == oz:
            return dict(row)
    return None


def add_brand(legal_name, wb_name, oz_name, country, aliases):
    """Создаёт новую строку бренда. aliases — список поисковых значений.

    Возвращает словарь созданной строки (со всеми колонками) или None.
    """
    data = json.dumps(_dedupe_brand_aliases(aliases), ensure_ascii=False)
    connection = sqlite3.connect(DB_PATH)
    try:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            """
            INSERT INTO brands (legal_name, wb_name, oz_name, search_aliases, country)
            VALUES (?, ?, ?, ?, ?)
            """,
            (legal_name or "", wb_name or "", oz_name or "", data, country or ""),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM brands WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def append_brand_aliases(brand_id, aliases):
    """Дозаписывает новые поисковые значения к существующему бренду (без дублей)."""
    connection = sqlite3.connect(DB_PATH)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM brands WHERE id = ?", (brand_id,)
        ).fetchone()
        if row is None:
            return None
        merged = _parse_brand_aliases(row["search_aliases"])
        merged.extend(aliases)
        merged = _dedupe_brand_aliases(merged)
        connection.execute(
            "UPDATE brands SET search_aliases = ? WHERE id = ?",
            (json.dumps(merged, ensure_ascii=False), brand_id),
        )
        connection.commit()
        updated = connection.execute(
            "SELECT * FROM brands WHERE id = ?", (brand_id,)
        ).fetchone()
        return dict(updated) if updated else None
    finally:
        connection.close()


# Расширения для выбора фотографий.
PHOTO_FILETYPES = [
    ("Изображения", "*.jpg *.jpeg *.png *.webp *.bmp *.gif"),
    ("Все файлы", "*.*"),
]


def _center_window(window, width: int, height: int) -> None:
    """Центрирует окно по экрану и не даёт ему выйти за границы экрана.

    Окна Cards Creator крупные, и при открытии у края экрана их нижние
    кнопки оказывались за пределами видимой области. Функция вычисляет
    позицию по центру рабочей области и при необходимости ужимает окно,
    чтобы оно помещалось целиком (с запасом под панель задач и рамку окна).
    """
    window.update_idletasks()
    screen_w = window.winfo_screenwidth()
    screen_h = window.winfo_screenheight()
    width = min(int(width), max(screen_w - 40, 320))
    height = min(int(height), max(screen_h - 60, 400))
    x = max((screen_w - width) // 2, 0)
    y = max((screen_h - height) // 2, 0)
    window.geometry(f"{width}x{height}+{x}+{y}")


# ---------------------------------------------------------------------------
# Константы восстановления удалённых карточек (Wildberries Content API).
# ---------------------------------------------------------------------------
WB_CONTENT_API = "https://content-api.wildberries.ru"
WB_MARKETPLACE_API = "https://marketplace-api.wildberries.ru"
CARDS_UPLOAD_URL = f"{WB_CONTENT_API}/content/v2/cards/upload"
CARDS_LIST_URL = f"{WB_CONTENT_API}/content/v2/get/cards/list"
CARDS_ERROR_LIST_URL = f"{WB_CONTENT_API}/content/v2/cards/error/list"
BARCODES_URL = f"{WB_CONTENT_API}/content/v2/barcodes"
MEDIA_FILE_URL = f"{WB_CONTENT_API}/content/v3/media/file"
MEDIA_SAVE_URL = f"{WB_CONTENT_API}/content/v3/media/save"

# ---------------------------------------------------------------------------
# Интеграция с DeepSeek (анализ фото товаров).
# ---------------------------------------------------------------------------
DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_EXTRACT_PROMT = "extract:standard"   # служебный код промта в таблице promts
DEEPSEEK_GENERATOR_PROMT = "extract:generator"   # универсальный генератор промтов категорий
DEEPSEEK_DEBUG_PATH = os.path.join(DATA_DIR, "deepseek_debug.txt")   # временный отладочный файл

# Общие (паспортные) характеристики, которые есть у карточек всех категорий.
# Это именованные поля из SEED_CHARCS, хранящиеся колонками в wb_products
# (Бренд, Наименование, Описание, Баркоды, габариты упаковки, вес). Подставляются
# в генератор как {{common_charcs}}.
COMMON_CHARC_NAMES = [name_ru for _, name_ru, _ in DBase.SEED_CHARCS]

# Пул сгенерированных баркодов хранится в data/ (читаемый JSON). Генерируем
# сразу пачкой, чтобы не дёргать API на каждую карточку.
BARCODE_POOL_PATH = os.path.join(DATA_DIR, "wb_barcode_pool.json")
BARCODE_BATCH_SIZE = 10

# Таймауты ожидания создания карточки и загрузки фото (сек).
CARD_CREATE_TIMEOUT = 120
PHOTO_VERIFY_TIMEOUT = 120

# Разрежённое расписание опроса создания карточки (экономия токенов «корзины»):
# первый запрос карточки через 15 с, затем два запроса с шагом 5 с, далее карточка
# и черновик чередуются с шагом 3 с; после минуты ожидания шаг снова 5 с.
CARD_FIRST_DELAY = 15.0
CARD_EARLY_DELAYS = (5.0, 5.0)
CARD_FAST_INTERVAL = 3.0
CARD_SLOW_INTERVAL = 5.0
CARD_SLOW_AFTER = 60.0

# Проверка загрузки фото: первый запрос через 20 с, далее каждые 5 с.
PHOTO_FIRST_DELAY = 20.0
PHOTO_VERIFY_INTERVAL = 5.0


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
# Работа с базой данных SQLite (только чтение данных).
# ---------------------------------------------------------------------------
def _search_by_art(art: str) -> list:
    """Ищет товары по артикулу ART в таблицах wb_products и oz_products.

    Возвращает список словарей с полями:
        platform, ART, SUP, vendorCode, title, nmID, imtID, subjectID,
        subjectName, is_deleted.
    Для Ozon vendorCode заполняется значением offer_id, title — name,
    nmID — product_id.
    """
    if not art or not os.path.exists(DB_PATH):
        return []

    results = []
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute(
            "SELECT 'WB' AS platform, ART, SUP, vendorCode, title, "
            "nmID, imtID, subjectID, subjectName, is_deleted "
            "FROM wb_products WHERE ART = ?",
            (art,),
        ):
            results.append(dict(row))

        for row in conn.execute(
            "SELECT 'OZON' AS platform, ART, SUP, offer_id AS vendorCode, "
            "name AS title, product_id AS nmID, NULL AS imtID, "
            "NULL AS subjectID, NULL AS subjectName, is_deleted "
            "FROM oz_products WHERE ART = ?",
            (art,),
        ):
            results.append(dict(row))
    except sqlite3.Error as exc:
        _logger.warning("Ошибка поиска товара ART=%s в БД: %s", art, exc)
    finally:
        conn.close()

    results.sort(
        key=lambda r: (
            bool(r.get("is_deleted")),
            str(r.get("platform") or ""),
            str(r.get("SUP") or ""),
        )
    )
    return results


def _load_categories() -> list:
    """Возвращает список категорий (предметов Wildberries) из базы данных.

    Каждая категория представлена словарём:
        {"subject_id": int, "name": str, "count": int}

    Список собирается из таблицы wb_products по парам (subjectID, subjectName)
    вместе с количеством товаров в каждой категории. Категории Ozon в БД
    хранятся только числовыми идентификаторами (description_category_id /
    type_id) без человекочитаемых названий, поэтому в список выбора они не
    включаются. Категории с пустым названием подписываются как «Предмет <ID>».
    """
    if not os.path.exists(DB_PATH):
        return []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT subjectID, subjectName, COUNT(*) AS cnt "
            "FROM wb_products "
            "WHERE subjectID IS NOT NULL "
            "GROUP BY subjectID, subjectName"
        ).fetchall()
    except sqlite3.Error as exc:
        _logger.warning("Не удалось прочитать категории из БД: %s", exc)
        rows = []
    finally:
        conn.close()

    categories = []
    for row in rows:
        subject_id = row["subjectID"]
        name = (row["subjectName"] or "").strip() or f"Предмет {subject_id}"
        categories.append(
            {"subject_id": subject_id, "name": name, "count": int(row["cnt"] or 0)}
        )
    return categories


# ---------------------------------------------------------------------------
# Папки: исходные фото (PhotosDrop)
# ---------------------------------------------------------------------------
def _ensure_photos_drop_dir() -> str:
    """Создаёт (если нужно) и возвращает папку исходных фото data/PhotosDrop."""
    os.makedirs(PHOTOS_DROP_DIR, exist_ok=True)
    return PHOTOS_DROP_DIR


def _open_folder(path: str) -> None:
    """Открывает папку в системном файловом менеджере (создаёт, если её нет)."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: S606 — Windows
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:  # noqa: BLE001
        _logger.warning("Не удалось открыть папку %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Черновики создаваемых карточек
# ---------------------------------------------------------------------------
def _load_drafts() -> list:
    """Загружает список черновиков из data/cards_creator_drafts.json."""
    if not os.path.exists(DRAFTS_PATH):
        return []
    try:
        with open(DRAFTS_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []
    except (OSError, ValueError) as exc:
        _logger.warning("Не удалось прочитать черновики: %s", exc)
        return []


def _save_drafts(drafts: list) -> None:
    """Перезаписывает файл черновиков (UTF-8, читаемый вид)."""
    try:
        with open(DRAFTS_PATH, "w", encoding="utf-8") as file:
            json.dump(drafts, file, ensure_ascii=False, indent=2)
    except OSError as exc:
        _logger.warning("Не удалось сохранить черновик: %s", exc)


def _draft_key(record: dict) -> str:
    """Уникальный ключ черновика — vendor_code (при отсутствии ART+SUP)."""
    vendor = (record.get("vendor_code") or "").strip()
    if vendor:
        return vendor
    art = str(record.get("art") or "").strip()
    sup = (record.get("sup") or "").strip() if record.get("sup") is not None else ""
    return f"{art}-{sup}" if art else ""


def _upsert_draft(record: dict) -> None:
    """Создаёт или обновляет черновик карточки по ключу vendor_code."""
    record = dict(record)
    record.setdefault("updated_at", datetime.now().isoformat(timespec="seconds"))
    key = _draft_key(record)
    if not key:
        return
    drafts = [d for d in _load_drafts() if _draft_key(d) != key]
    drafts.append(record)
    _save_drafts(drafts)


def _get_draft(vendor_code: str) -> dict | None:
    """Возвращает черновик по vendor_code или None."""
    key = (vendor_code or "").strip()
    if not key:
        return None
    for draft in _load_drafts():
        if _draft_key(draft) == key:
            return draft
    return None


def _delete_draft(vendor_code: str) -> None:
    """Удаляет черновик карточки по vendor_code."""
    key = (vendor_code or "").strip()
    if not key:
        return
    drafts = [d for d in _load_drafts() if _draft_key(d) != key]
    _save_drafts(drafts)


def _card_in_db(vendor_code: str) -> bool:
    """Проверяет, появилась ли карточка в локальной БД wb_products."""
    code = (vendor_code or "").strip()
    if not code:
        return False
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            cur = conn.execute(
                "SELECT 1 FROM wb_products WHERE vendorCode = ? LIMIT 1", (code,)
            )
            return cur.fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _logger.warning("Не удалось проверить карточку в БД: %s", exc)
        return False


def _delete_card_photos(photos) -> None:
    """Удаляет исходные файлы фото карточки из PhotosDrop (после успеха)."""
    for photo in photos or []:
        src = (photo.get("src") or "").strip()
        if not src or not os.path.isfile(src):
            continue
        # Удаляем только файлы из папки исходных фото, чтобы не задеть чужие пути.
        if os.path.dirname(os.path.abspath(src)) != os.path.abspath(PHOTOS_DROP_DIR):
            continue
        try:
            os.remove(src)
        except OSError as exc:
            _logger.warning("Не удалось удалить исходное фото %s: %s", src, exc)


# ---------------------------------------------------------------------------
# Миниатюры предпросмотра и токен DeepSeek
# ---------------------------------------------------------------------------
def _load_thumbnail(path: str, size=(96, 96)):
    """Возвращает квадратную миниатюру изображения для предпросмотра.

    Изображение вписывается в квадрат size×size с сохранением пропорций и
    центрируется на белой подложке (letterbox). Возвращает PIL-изображение
    или None, если файл не удалось открыть/обработать.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail(size, Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", size, (255, 255, 255))
            x = (size[0] - img.width) // 2
            y = (size[1] - img.height) // 2
            canvas.paste(img, (x, y))
            return canvas
    except Exception:  # noqa: BLE001 — повреждённый/неподдерживаемый файл
        return None


def _read_deepseek_token() -> str:
    """Возвращает токен DeepSeek из .env (или пустую строку)."""
    return DBase.read_env_value("DEEPSEEK_TOKEN") or ""


def _save_deepseek_token(token: str) -> None:
    """Сохраняет токен DeepSeek в .env, сохраняя остальные ключи неизменными."""
    values = {}
    for key, _label in DBase.WB_TOKEN_LABELS + DBase.OZ_TOKEN_LABELS + DBase.ENV_PARAM_LABELS:
        values[key] = DBase.read_env_value(key) or ""
    values["DEEPSEEK_TOKEN"] = (token or "").strip()
    DBase.write_env_file(values)


# ---------------------------------------------------------------------------
# DeepSeek: отправка фото на анализ и разбор ответа.
# ---------------------------------------------------------------------------
def _image_to_data_url(path: str) -> str:
    """Кодирует изображение в base64 data URL для передачи в DeepSeek (vision)."""
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "jpeg"
    mime = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
        "bmp": "image/bmp",
    }.get(ext, "image/jpeg")
    with open(path, "rb") as file_handle:
        encoded = base64.b64encode(file_handle.read()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _write_deepseek_debug(payload: dict, response_text: str) -> None:
    """Пишет запрос/ответ DeepSeek в data/deepseek_debug.txt (временная отладка)."""
    debug_payload = copy.deepcopy(payload)
    for message in debug_payload.get("messages") or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            url = (part.get("image_url") or {}).get("url")
            if isinstance(url, str) and url.startswith("data:"):
                part["image_url"]["url"] = url[:60] + f"… (base64, {len(url)} симв.)"
    try:
        with open(DEEPSEEK_DEBUG_PATH, "a", encoding="utf-8") as file:
            file.write("=" * 70 + "\n")
            file.write("ВРЕМЯ: " + datetime.now().isoformat(timespec="seconds") + "\n")
            file.write("-" * 70 + "\n")
            file.write("ЗАПРОС (payload):\n")
            file.write(json.dumps(debug_payload, ensure_ascii=False, indent=2) + "\n")
            file.write("-" * 70 + "\n")
            file.write("ОТВЕТ:\n")
            file.write(response_text + "\n")
            file.write("\n")
    except OSError as exc:
        _logger.warning("Не удалось записать отладочный файл DeepSeek: %s", exc)


def _call_deepseek_extract(promt: dict, image_paths: list, system_prompt: str = ""):
    """Отправляет фото в DeepSeek и возвращает (текст_ответа, raw_JSON, ошибка)."""
    token = _read_deepseek_token()
    if not token:
        return None, None, "Не задан токен DeepSeek (кнопка «⚙️ DeepSeek»)."

    content = [{"type": "text", "text": "Извлеки характеристики из фотографий товара."}]
    for path in image_paths:
        content.append(
            {"type": "image_url", "image_url": {"url": _image_to_data_url(path)}}
        )

    payload = {
        "model": promt.get("model") or "deepseek-flash",
        "messages": [
            {"role": "system", "content": system_prompt or promt.get("prompt_text") or ""},
            {"role": "user", "content": content},
        ],
        "temperature": promt.get("temperature", 0.0),
        "stream": False,
    }
    if (promt.get("response_format") or "json") == "json":
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            DEEPSEEK_CHAT_URL, headers=headers, json=payload, timeout=180
        )
    except requests.RequestException as exc:
        _write_deepseek_debug(payload, f"СЕТЕВАЯ ОШИБКА: {exc}")
        return None, None, f"Сетевая ошибка DeepSeek: {exc}"

    if response.status_code != 200:
        _write_deepseek_debug(
            payload, f"HTTP {response.status_code}: {response.text[:2000]}"
        )
        return None, None, f"DeepSeek HTTP {response.status_code}: {response.text[:400]}"

    try:
        data = response.json()
    except ValueError:
        _write_deepseek_debug(payload, f"НЕ-JSON ОТВЕТ: {response.text[:2000]}")
        return None, None, "DeepSeek вернул не-JSON ответ."

    raw_pretty = json.dumps(data, ensure_ascii=False, indent=2)
    _write_deepseek_debug(payload, raw_pretty)

    content_text = ""
    try:
        content_text = (data["choices"][0]["message"]["content"]) or ""
    except (KeyError, IndexError, TypeError):
        pass

    return content_text, raw_pretty, None


def _parse_characteristics(text: str) -> list:
    """Разбирает ответ DeepSeek в список характеристик [{name, value}, ...]."""
    if not text:
        return []

    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.strip("`")
        if clean.lower().startswith("json"):
            clean = clean[4:].lstrip()

    data = None
    try:
        data = json.loads(clean)
    except ValueError:
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(clean[start:end + 1])
            except ValueError:
                data = None

    chars = data.get("characteristics") if isinstance(data, dict) else None
    if not isinstance(chars, list):
        return []

    result = []
    for item in chars:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        value = item.get("value")
        source = str(item.get("source") or "photo").strip().lower()
        if source not in ("photo", "inferred"):
            source = "photo"
        result.append(
            {
                "name": name,
                "value": "" if value is None else str(value),
                "source": source,
            }
        )
    return result


# ---------------------------------------------------------------------------
# Генерация индивидуального промпта извлечения для категории (subjectID).
# ---------------------------------------------------------------------------
def _category_prompt_code(subject_id) -> str:
    """Служебный код персонального промпта категории в таблице promts."""
    return f"extract:subject:{subject_id}"


def _load_prompted_subject_ids() -> set:
    """Возвращает множество subject_id, для которых уже создан персональный промпт."""
    if not os.path.exists(DB_PATH):
        return set()
    result = set()
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT code FROM promts WHERE code LIKE 'extract:subject:%'"
        ).fetchall()
        for (code,) in rows:
            try:
                result.add(int(code.rsplit(":", 1)[1]))
            except (ValueError, IndexError):
                continue
    except sqlite3.Error as exc:
        _logger.warning("Не удалось прочитать персональные промпты: %s", exc)
    finally:
        conn.close()
    return result


def _fill_extract_prompt(text: str, category_name, category_charcs) -> str:
    """Подставляет категорию и список характеристик в шаблон промпта."""
    if category_charcs:
        charcs_list = "\n".join(f"- {c['name']}" for c in category_charcs)
    else:
        charcs_list = "(характеристики категории не найдены в БД)"
    return (
        (text or "")
        .replace("{{category}}", str(category_name))
        .replace("{{charcs}}", charcs_list)
    )


def _build_generator_variables(subject_id, category_name, category_charcs) -> dict:
    """Собирает переменные для подстановки в универсальный промпт-генератор."""
    common_lines = "\n".join(f"- {n}" for n in COMMON_CHARC_NAMES)
    charc_lines = []
    for c in category_charcs:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        suffix = " (обязательная)" if c.get("is_required") else ""
        charc_lines.append(f"- {name}{suffix}")
    charcs = "\n".join(charc_lines) if charc_lines else "(характеристики категории не найдены в БД)"
    return {
        "category": str(category_name),
        "subject_id": str(subject_id),
        "common_charcs": common_lines,
        "charcs": charcs,
    }


def _parse_generated_prompt(text):
    """Разбирает JSON-ответ генератора и возвращает {prompt_text, characteristics}."""
    if not text:
        return None
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.strip("`")
        if clean.lower().startswith("json"):
            clean = clean[4:].lstrip()
    data = None
    try:
        data = json.loads(clean)
    except ValueError:
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(clean[start:end + 1])
            except ValueError:
                data = None
    if not isinstance(data, dict):
        return None
    prompt_text = str(data.get("prompt_text") or "").strip()
    if not prompt_text:
        return None
    return {
        "prompt_text": prompt_text,
        "characteristics": data.get("characteristics"),
    }


def _call_deepseek_generate(promt: dict, variables: dict):
    """Отправляет универсальный промпт-генератор и возвращает (parsed, raw, error)."""
    token = _read_deepseek_token()
    if not token:
        return None, None, "Не задан токен DeepSeek."

    prompt_text = promt.get("prompt_text") or ""
    for key, value in variables.items():
        prompt_text = prompt_text.replace("{{" + key + "}}", str(value))

    payload = {
        "model": promt.get("model") or "deepseek-flash",
        "messages": [
            {"role": "system", "content": prompt_text},
            {"role": "user", "content": "Сгенерируй промпт."},
        ],
        "temperature": promt.get("temperature", 0.0),
        "stream": False,
    }
    if (promt.get("response_format") or "json") == "json":
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            DEEPSEEK_CHAT_URL, headers=headers, json=payload, timeout=180
        )
    except requests.RequestException as exc:
        _write_deepseek_debug(payload, f"СЕТЕВАЯ ОШИБКА (генератор): {exc}")
        return None, None, f"Сетевая ошибка DeepSeek: {exc}"

    if response.status_code != 200:
        _write_deepseek_debug(
            payload, f"HTTP {response.status_code}: {response.text[:2000]}"
        )
        return None, None, f"DeepSeek HTTP {response.status_code}: {response.text[:400]}"

    try:
        data = response.json()
    except ValueError:
        _write_deepseek_debug(payload, f"НЕ-JSON ОТВЕТ: {response.text[:2000]}")
        return None, None, "DeepSeek вернул не-JSON ответ."

    raw_pretty = json.dumps(data, ensure_ascii=False, indent=2)
    _write_deepseek_debug(payload, raw_pretty)

    content_text = ""
    try:
        content_text = (data["choices"][0]["message"]["content"]) or ""
    except (KeyError, IndexError, TypeError):
        pass

    parsed = _parse_generated_prompt(content_text)
    if parsed is None:
        return content_text, raw_pretty, "Не удалось разобрать ответ генератора."
    return parsed, raw_pretty, None


def _resolve_category_prompt(subject_id, category_name, category_charcs, category_promt):
    """Возвращает (promt_dict, system_prompt, note) для распознавания фото категории.

    Использует сохранённый индивидуальный промпт категории, либо генерирует его
    через универсальный генератор (extract:generator) и сохраняет в БД. При сбоях
    — fallback на базовый промт extract:standard.
    """
    fallback_promt = DBase.get_promt(DEEPSEEK_EXTRACT_PROMT)

    if category_promt is not None:
        system_prompt = _fill_extract_prompt(
            category_promt.get("prompt_text") or "", category_name, category_charcs
        )
        return category_promt, system_prompt, "промпт категории (сохранённый)"

    if subject_id is not None:
        generator = DBase.get_promt(DEEPSEEK_GENERATOR_PROMT)
        if generator is not None:
            variables = _build_generator_variables(subject_id, category_name, category_charcs)
            parsed, _raw, error = _call_deepseek_generate(generator, variables)
            if error is None and parsed:
                code = _category_prompt_code(subject_id)
                try:
                    DBase.save_custom_promt(
                        code=code,
                        name=f"Извлечение — {category_name}",
                        prompt_text=parsed["prompt_text"],
                    )
                except Exception as exc:  # noqa: BLE001 — не роняем поток анализа
                    _logger.warning("Не удалось сохранить промпт категории: %s", exc)
                saved = DBase.get_promt(code)
                if saved is not None:
                    system_prompt = _fill_extract_prompt(
                        saved.get("prompt_text") or "", category_name, category_charcs
                    )
                    return saved, system_prompt, "промпт категории (сгенерирован и сохранён)"
            _logger.warning("Не удалось сгенерировать промпт категории: %s", error)

    if fallback_promt is not None:
        system_prompt = _fill_extract_prompt(
            fallback_promt.get("prompt_text") or "", category_name, category_charcs
        )
        return fallback_promt, system_prompt, "базовый промпт extract:standard (fallback)"

    return None, "", "промт не найден в БД"


def _call_deepseek_description(promt: dict, characteristics_text: str):
    """Генерирует описание по готовому тексту (без фото). Оставлено для совместимости."""
    token = _read_deepseek_token()
    if not token:
        return None, "Не задан токен DeepSeek."

    prompt_text = (promt.get("prompt_text") or "").replace(
        "{{characteristics}}", characteristics_text
    )

    payload = {
        "model": promt.get("model") or "deepseek-flash",
        "messages": [
            {"role": "system", "content": prompt_text},
            {"role": "user", "content": "Составь описание товара."},
        ],
        "temperature": promt.get("temperature", 0.7),
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            DEEPSEEK_CHAT_URL, headers=headers, json=payload, timeout=180
        )
    except requests.RequestException as exc:
        _write_deepseek_debug(payload, f"СЕТЕВАЯ ОШИБКА (описание): {exc}")
        return None, f"Сетевая ошибка DeepSeek: {exc}"

    if response.status_code != 200:
        _write_deepseek_debug(
            payload, f"HTTP {response.status_code}: {response.text[:2000]}"
        )
        return None, f"DeepSeek HTTP {response.status_code}: {response.text[:400]}"

    try:
        data = response.json()
    except ValueError:
        _write_deepseek_debug(payload, f"НЕ-JSON ОТВЕТ: {response.text[:2000]}")
        return None, "DeepSeek вернул не-JSON ответ."

    raw_pretty = json.dumps(data, ensure_ascii=False, indent=2)
    _write_deepseek_debug(payload, raw_pretty)

    content = ""
    try:
        content = (data["choices"][0]["message"]["content"]) or ""
    except (KeyError, IndexError, TypeError):
        pass
    return content.strip(), None


def _call_deepseek_description_from_photo(promt: dict, image_paths: list):
    """Генерирует описание сразу по фото (vision), без промежуточной аннотации."""
    token = _read_deepseek_token()
    if not token:
        return None, "Не задан токен DeepSeek."

    content = [{"type": "text", "text": "Составь описание товара по фотографиям."}]
    for path in image_paths:
        content.append(
            {"type": "image_url", "image_url": {"url": _image_to_data_url(path)}}
        )

    payload = {
        "model": promt.get("model") or "deepseek-flash",
        "messages": [
            {"role": "system", "content": promt.get("prompt_text") or ""},
            {"role": "user", "content": content},
        ],
        "temperature": promt.get("temperature", 0.5),
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            DEEPSEEK_CHAT_URL, headers=headers, json=payload, timeout=180
        )
    except requests.RequestException as exc:
        _write_deepseek_debug(payload, f"СЕТЕВАЯ ОШИБКА (описание): {exc}")
        return None, f"Сетевая ошибка DeepSeek: {exc}"

    if response.status_code != 200:
        _write_deepseek_debug(
            payload, f"HTTP {response.status_code}: {response.text[:2000]}"
        )
        return None, f"DeepSeek HTTP {response.status_code}: {response.text[:400]}"

    try:
        data = response.json()
    except ValueError:
        _write_deepseek_debug(payload, f"НЕ-JSON ОТВЕТ: {response.text[:2000]}")
        return None, "DeepSeek вернул не-JSON ответ."

    raw_pretty = json.dumps(data, ensure_ascii=False, indent=2)
    _write_deepseek_debug(payload, raw_pretty)

    content_text = ""
    try:
        content_text = (data["choices"][0]["message"]["content"]) or ""
    except (KeyError, IndexError, TypeError):
        pass
    return content_text.strip(), None


# ---------------------------------------------------------------------------
# Восстановление удалённых карточек: чтение полных данных из БД.
# ---------------------------------------------------------------------------
def _load_card_details(art, sup):
    """Возвращает полные данные удалённой карточки WB из БД или None.

    Словарь включает корневые параметры wb_products и список характеристик
    wb_product_values (charcID + value + имя из wb_charcs). Документы и фото не
    читаются — при восстановлении выбираются заново/захардкожены.
    """
    if not art or not os.path.exists(DB_PATH):
        return None

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if sup is not None:
            row = conn.execute(
                "SELECT * FROM wb_products WHERE ART = ? AND SUP = ?", (art, sup)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM wb_products WHERE ART = ? AND SUP IS NULL", (art,)
            ).fetchone()

        if row is None:
            return None

        details = dict(row)
        details["ART"] = art
        details["SUP"] = sup

        if sup is not None:
            char_rows = conn.execute(
                "SELECT charcID, value FROM wb_product_values "
                "WHERE ART = ? AND SUP = ? AND charcID IS NOT NULL "
                "ORDER BY charcID",
                (art, sup),
            ).fetchall()
        else:
            char_rows = conn.execute(
                "SELECT charcID, value FROM wb_product_values "
                "WHERE ART = ? AND SUP IS NULL AND charcID IS NOT NULL "
                "ORDER BY charcID",
                (art,),
            ).fetchall()

        characteristics = []
        for char_row in char_rows:
            characteristics.append(
                {
                    "charcID": char_row["charcID"],
                    "name": _charc_name(conn, char_row["charcID"]),
                    "value": char_row["value"] if char_row["value"] is not None else "",
                }
            )
        details["characteristics"] = characteristics
        return details
    finally:
        conn.close()


def _charc_name(conn, charc_id):
    """Имя характеристики из wb_charcs (пустая строка, если нет в справочнике)."""
    try:
        row = conn.execute(
            "SELECT name_ru FROM wb_charcs WHERE charcID = ?", (charc_id,)
        ).fetchone()
        return (row[0] or "").strip() if row else ""
    except sqlite3.Error:
        return ""


def _load_card_photo_urls(art, sup):
    """Возвращает упорядоченный список URL «big» фото карточки из wb_product_values.

    Фото хранятся в `wb_product_values` как JSON (`field_name = 'photos'`) с полной
    структурой; у каждого кадра есть размеры `big`, `c246x328`, `small`, `tm`,
    `c516x688`. Для загрузки по ссылкам (media/save) берём самый крупный `big`.
    """
    if not art or not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    try:
        if sup is not None:
            row = conn.execute(
                "SELECT value FROM wb_product_values "
                "WHERE ART = ? AND SUP = ? AND field_name = 'photos' LIMIT 1",
                (art, sup),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT value FROM wb_product_values "
                "WHERE ART = ? AND SUP IS NULL AND field_name = 'photos' LIMIT 1",
                (art,),
            ).fetchone()
    except sqlite3.Error as exc:
        _logger.warning("Не удалось прочитать фото карточки: %s", exc)
        return []
    finally:
        conn.close()

    if not row or not row[0]:
        return []
    try:
        data = json.loads(row[0])
    except (TypeError, ValueError):
        return []

    urls = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        url = None
        for key in ("big", "c246x328", "small", "tm", "c516x688"):
            if item.get(key):
                url = item[key]
                break
        if not url:
            for value in item.values():
                if isinstance(value, str) and value.startswith("http"):
                    url = value
                    break
        if url:
            urls.append(url)
    return urls


def _load_required_charcs(subject_id):
    """Возвращает обязательные характеристики категории из wb_charcs.

    Список словарей {charcID, name, value} для полей с is_required = 1,
    относящихся к указанной категории (subj_ID). Паспортные (корневые)
    характеристики исключаются — они представлены отдельными полями формы.
    Значения всегда пустые: фактические значения читаются из
    wb_product_values отдельно (см. _load_card_details).
    """
    if subject_id is None or not os.path.exists(DB_PATH):
        return []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT charcID, name_ru, subj_ID FROM wb_charcs WHERE is_required = 1"
        ).fetchall()
    except sqlite3.Error as exc:
        _logger.warning("Не удалось прочитать обязательные характеристики: %s", exc)
        rows = []
    finally:
        conn.close()

    result = []
    subject_str = str(subject_id)
    for row in rows:
        charc_id = row["charcID"]
        if charc_id in DBase.PASSPORT_CHARC_IDS:
            continue
        subj_ids = {s.strip() for s in (row["subj_ID"] or "").split(",") if s.strip()}
        if subject_str not in subj_ids:
            continue
        result.append(
            {
                "charcID": charc_id,
                "name": (row["name_ru"] or "").strip(),
                "value": "",
            }
        )
    return result


def _load_category_charcs(subject_id):
    """Возвращает ВСЕ характеристики категории из wb_charcs (для анализа DeepSeek).

    Список словарей {charcID, name, is_required} для характеристик, относящихся
    к указанной категории (subj_ID). Паспортные (корневые) характеристики
    исключаются — они представлены отдельными полями формы.
    """
    if subject_id is None or not os.path.exists(DB_PATH):
        return []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT charcID, name_ru, is_required, subj_ID FROM wb_charcs"
        ).fetchall()
    except sqlite3.Error as exc:
        _logger.warning("Не удалось прочитать характеристики категории: %s", exc)
        rows = []
    finally:
        conn.close()

    result = []
    subject_str = str(subject_id)
    for row in rows:
        charc_id = row["charcID"]
        if charc_id in DBase.PASSPORT_CHARC_IDS:
            continue
        subj_ids = {s.strip() for s in (row["subj_ID"] or "").split(",") if s.strip()}
        if subject_str not in subj_ids:
            continue
        result.append(
            {
                "charcID": charc_id,
                "name": (row["name_ru"] or "").strip(),
                "is_required": bool(row["is_required"]),
            }
        )
    return result


def _load_all_category_fields(subject_id):
    """Возвращает полный список полей категории для конструктора.

    Поля: паспортные (общие параметры wb_products из SEED_CHARCS), характеристики
    категории из wb_charcs и ручные поля (Цена/Стоимость). Каждое поле — словарь
    {key, charcID, name, is_required, kind}, где kind ∈ {'passport','category','manual'}.
    """
    fields = []
    for charc_id, name_ru, json_key in DBase.SEED_CHARCS:
        # Баркоды (skus) генерируются автоматически — не показываем их в конструкторе.
        if json_key == "skus":
            continue
        fields.append(
            {
                "key": f"charc:{charc_id}",
                "charcID": charc_id,
                "name": name_ru,
                "is_required": False,
                "kind": "passport",
            }
        )
    for c in _load_category_charcs(subject_id):
        fields.append(
            {
                "key": f"charc:{c['charcID']}",
                "charcID": c["charcID"],
                "name": c["name"],
                "is_required": bool(c.get("is_required")),
                "kind": "category",
            }
        )
    fields.append(
        {
            "key": "field:price",
            "charcID": None,
            "name": "Цена",
            "is_required": False,
            "kind": "manual",
        }
    )
    fields.append(
        {
            "key": "field:cost",
            "charcID": None,
            "name": "Стоимость",
            "is_required": False,
            "kind": "manual",
        }
    )
    # Размер и КИЗ-маркировка нужны для тела cards/upload (значения по умолчанию —
    # как в форме восстановления). wbSize опционален, techSize по умолчанию "0".
    fields.append(
        {
            "key": "field:techSize",
            "charcID": None,
            "name": "Размер (techSize)",
            "is_required": False,
            "kind": "manual",
            "default": "0",
        }
    )
    fields.append(
        {
            "key": "field:wbSize",
            "charcID": None,
            "name": "Рос. размер (wbSize)",
            "is_required": False,
            "kind": "manual",
            "default": "",
        }
    )
    return fields


# ---------------------------------------------------------------------------
# Пул сгенерированных баркодов (файл data/wb_barcode_pool.json).
# ---------------------------------------------------------------------------
def _load_barcode_pool() -> list:
    try:
        if not os.path.exists(BARCODE_POOL_PATH):
            return []
        with open(BARCODE_POOL_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_barcode_pool(pool: list) -> None:
    try:
        with open(BARCODE_POOL_PATH, "w", encoding="utf-8") as file:
            json.dump(pool, file, ensure_ascii=False, indent=2)
    except OSError as exc:
        _logger.warning("Не удалось сохранить пул баркодов: %s", exc)


def _fetch_barcodes(api_key, count: int) -> list:
    """Запрашивает у WB `count` новых баркодов через /content/v2/barcodes."""
    response = _wb_http("POST", BARCODES_URL, api_key, json_body={"count": count})
    data = _safe_json(response).get("data")
    if not isinstance(data, list):
        return []
    return [str(item).strip() for item in data if str(item).strip()]


def _take_barcode(api_key) -> str:
    """Возвращает один баркод из пула, догенерируя пачку при необходимости."""
    pool = _load_barcode_pool()
    if not pool:
        pool = _fetch_barcodes(api_key, BARCODE_BATCH_SIZE)
        if not pool:
            raise RuntimeError("Не удалось сгенерировать баркод (пустой ответ API).")
    barcode = pool.pop(0)
    _save_barcode_pool(pool)
    return barcode


# ---------------------------------------------------------------------------
# Временное хранение цены/себестоимости для карточек, которых ещё нет в БД.
# ---------------------------------------------------------------------------
def _load_pending_costs() -> list:
    try:
        if not os.path.exists(PENDING_COSTS_PATH):
            return []
        with open(PENDING_COSTS_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_pending_costs(records: list) -> None:
    try:
        with open(PENDING_COSTS_PATH, "w", encoding="utf-8") as file:
            json.dump(records, file, ensure_ascii=False, indent=2)
    except OSError as exc:
        _logger.warning("Не удалось сохранить отложенные цены/себестоимость: %s", exc)


def _store_pending_cost(vendor_code, art, sup, price, cost) -> None:
    """Сохраняет цену/себестоимость новой карточки до её появления в БД."""
    if price is None and cost is None:
        return
    records = _load_pending_costs()
    records = [r for r in records if (r.get("vendorCode") or "") != vendor_code]
    records.append(
        {
            "vendorCode": vendor_code,
            "art": art,
            "sup": sup,
            "price": price,
            "cost": cost,
        }
    )
    _save_pending_costs(records)


def _flush_pending_costs() -> None:
    """Переносит отложенные цену/себестоимость в wb_products по появившимся карточкам."""
    records = _load_pending_costs()
    if not records or not os.path.exists(DB_PATH):
        return
    remaining = []
    conn = sqlite3.connect(DB_PATH)
    try:
        for rec in records:
            vendor_code = (rec.get("vendorCode") or "").strip()
            if not vendor_code:
                remaining.append(rec)
                continue
            cur = conn.execute(
                "UPDATE wb_products SET price = ?, cost = ? WHERE vendorCode = ?",
                (rec.get("price"), rec.get("cost"), vendor_code),
            )
            if cur.rowcount == 0:
                remaining.append(rec)
        conn.commit()
    except sqlite3.Error as exc:
        _logger.warning("Не удалось перенести отложенные цены: %s", exc)
        return
    finally:
        conn.close()
    _save_pending_costs(remaining)


# ---------------------------------------------------------------------------
# HTTP-запросы к Wildberries (синхронные, с rate-limit через DBase.LIMITER).
# ---------------------------------------------------------------------------
def _safe_json(response):
    try:
        return response.json()
    except ValueError:
        return {}


def _wb_http(method, url, api_key, *, json_body=None, headers=None, files=None,
             rate_category="CONTENT", timeout=60):
    """Синхронный запрос к WB с ретраями при 429 и сетевых ошибках."""
    merged_headers = {"Authorization": api_key}
    if headers:
        merged_headers.update(headers)

    last_exc = None
    for attempt in range(1, 4):
        DBase.LIMITER.wait_for_token(rate_category)
        try:
            response = requests.request(
                method,
                url,
                headers=merged_headers,
                json=json_body,
                files=files,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_exc = exc
            _logger.warning("Сетевая ошибка %s %s: %s. Ретрай %d/3", method, url, exc, attempt)
            time.sleep(5)
            continue

        if response.status_code == 429:
            if attempt >= 3:
                response.raise_for_status()
            _logger.warning("HTTP 429 для %s. Ретрай %d/3", url, attempt)
            time.sleep(5)
            continue

        if response.status_code >= 400:
            _logger.error("HTTP %s для %s: %s", response.status_code, url, response.text[:300])
            response.raise_for_status()

        return response

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"Не удалось выполнить {method} {url}")


# ---------------------------------------------------------------------------
# Поиск созданной карточки и ошибок черновиков.
# ---------------------------------------------------------------------------
class _CardDraftError(Exception):
    """Карточка попала в черновик с ошибками (атрибут errors — список текстов)."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("\n".join(str(e) for e in self.errors))


def _sleep_or_timeout(delay: float, deadline: float) -> bool:
    """Спит delay секунд; возвращает False, если таймаут ожидания истёк."""
    time.sleep(delay)
    return time.monotonic() <= deadline


def _find_card_in_list(api_key, vendor_code):
    """Ищет карточку по vendorCode через cards/list (filter textSearch)."""
    payload = {
        "settings": {
            "sort": {"ascending": False},
            "filter": {"withPhoto": -1, "textSearch": vendor_code},
        }
    }
    response = _wb_http("POST", CARDS_LIST_URL, api_key, json_body=payload)
    data = _safe_json(response)
    cards = data.get("cards") or []
    for card in cards:
        if (card.get("vendorCode") or "").strip() == vendor_code.strip():
            return card
    return None


def _get_card_by_nmid(api_key, nm_id):
    """Возвращает карточку по nmID через cards/list (filter nmIDs)."""
    payload = {
        "settings": {
            "sort": {"ascending": False},
            "filter": {"withPhoto": -1, "nmIDs": [int(nm_id)]},
        }
    }
    response = _wb_http("POST", CARDS_LIST_URL, api_key, json_body=payload)
    data = _safe_json(response)
    cards = data.get("cards") or []
    return cards[0] if cards else None


def _check_card_errors(api_key, vendor_code):
    """Проверяет, попала ли карточка в черновик с ошибками (cards/error/list).

    Возвращает список текстов ошибок для vendorCode или None, если ошибок нет.
    """
    payload = {
        "cursor": {"limit": 100},
        "order": {"ascending": True},
    }
    response = _wb_http("POST", CARDS_ERROR_LIST_URL, api_key, json_body=payload)
    data = _safe_json(response).get("data") or {}
    for item in data.get("items") or []:
        vendor_codes = item.get("vendorCodes") or []
        if vendor_code in [str(v).strip() for v in vendor_codes]:
            errors = (item.get("errors") or {}).get(vendor_code) or []
            return errors if errors else ["Карточка отклонена (без описания ошибки)."]
    return None


def _fetch_charc_names(api_key, subject_id):
    """Возвращает словарь {charcID: name} из справочника категории WB."""
    try:
        subject_id = int(subject_id)
    except (TypeError, ValueError):
        return {}
    url = f"{WB_CONTENT_API}/content/v2/object/charcs/{subject_id}"
    try:
        response = _wb_http("GET", url, api_key)
    except requests.RequestException as exc:
        _logger.warning("Не удалось получить справочник характеристик: %s", exc)
        return {}
    data = _safe_json(response)
    if isinstance(data, dict):
        data = data.get("data") or []
    result = {}
    if isinstance(data, list):
        for charc in data:
            if isinstance(charc, dict) and charc.get("id") is not None:
                result[charc.get("id")] = (charc.get("name") or "").strip()
    return result


def _first_chrt_id(card):
    """Извлекает первый chrtID из массива sizes карточки WB."""
    sizes = card.get("sizes") or []
    for size in sizes:
        if isinstance(size, dict) and size.get("chrtID") is not None:
            try:
                return int(size["chrtID"])
            except (TypeError, ValueError):
                continue
    return None


def _content_type(path):
    """Возвращает MIME-тип файла по расширению (для загрузки фото)."""
    ext = os.path.splitext(path or "")[1].lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".gif": "image/gif",
    }.get(ext, "image/jpeg")


def _save_media_by_urls(api_key, nm_id, urls):
    """Загружает медиа в карточку по ссылкам (POST /content/v3/media/save).

    Полностью заменяет медиа карточки набором из `urls` (порядок массива = порядок
    в карточке). Требует прямых ссылок без авторизации, заканчивающихся именем файла.
    """
    response = _wb_http(
        "POST",
        MEDIA_SAVE_URL,
        api_key,
        json_body={"nmId": int(nm_id), "data": list(urls)},
    )
    data = _safe_json(response)
    if data.get("error"):
        raise RuntimeError(data.get("errorText") or "Ошибка загрузки медиа по ссылкам.")


# ---------------------------------------------------------------------------
# Разбор значений характеристик для тела cards/upload.
# ---------------------------------------------------------------------------
def _to_number(text):
    """Преобразует строку в int/float или возвращает None для пустого значения."""
    if text is None:
        return None
    text = str(text).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _to_int(text):
    """Преобразует строку в целое число (отбрасывает дробную часть) или None."""
    value = _to_number(text)
    return None if value is None else int(value)


def _compose_vendor_code(art, sup) -> str:
    """Формирует vendorCode из артикула и опционального SUP (как в восстановлении)."""
    code = str(art or "").strip()
    if sup:
        code += "-" + str(sup).strip()
    return code


def _fmt_dimension(value):
    """Форматирует размер без дробной части у целых значений (22.0 → '22')."""
    if value is None:
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if num.is_integer():
        return str(int(num))
    return str(value)


# Характеристики, которые всегда отправляются строкой, даже если их значение
# состоит только из цифр (год выпуска, ТНВЭД, код ТН ВЭД).
STRING_CHARC_IDS = frozenset({189099, 15000001, 15004139})


def _parse_char_value(raw, type_hint):
    """Преобразует строковое значение характеристики в значение для запроса.

    type_hint:
        "auto" — эвристика: число → число, булево слово → bool, иначе список;
        "str"  — строка как есть;
        "num"  — число (int/float);
        "list" — список строк (разбиение по запятой);
        "bool" — булево значение.
    """
    raw = (raw or "").strip()
    if type_hint == "num":
        value = _to_number(raw)
        return value if value is not None else raw
    if type_hint == "bool":
        return raw.lower() in ("true", "да", "1", "yes", "on")
    if type_hint == "str":
        return raw
    if type_hint == "list":
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return parts if parts else []

    # auto
    if raw == "":
        return None
    try:
        float(raw.replace(",", "."))
        return _to_number(raw)
    except ValueError:
        pass
    if raw.lower() in ("true", "да", "1", "false", "нет", "0", "yes", "no", "on", "off"):
        return raw.lower() in ("true", "да", "1", "yes", "on")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts if parts else [raw]


# ---------------------------------------------------------------------------
# Графическое окно модуля (создаётся на главном потоке).
# ---------------------------------------------------------------------------
class CardsCreatorWindow:
    """Окно пошагового создания/редактирования карточки товара."""

    def __init__(self, root, on_close=None):
        self.root = root
        self.on_close = on_close

        self._current = None        # {"art": ..., "sup": ...} текущего ввода
        self._photos = []           # [{"src","name","main","analysis"}, ...]
        self._main_var = None       # tk.StringVar — индекс главного фото
        self._thumb_refs = []       # ссылки на CTkImage миниатюр (защита от GC)
        self._photo_screen_active = False  # True, когда открыт экран подготовки фото

        # Состояние восстановления удалённой карточки.
        self._restore = None        # dict: details/entries/charc_rows и т.д.
        self._restore_photos = []   # [{"src","name","main"}, ...] для восстановления

        # Очередь фоновой отправки карточек. Задачи обрабатываются строго по одной
        # в отдельном потоке-воркере — одновременная загрузка двух карточек исключена,
        # а интерфейс при этом не блокируется (можно сразу сканировать следующий ART).
        self._job_queue = queue.Queue()
        self._worker_thread = None      # поток-воркер (создаётся лениво)
        self._queue_status_label = None # постоянная строка статуса очереди
        self._queue_status_msg = ""     # последнее сообщение очереди

        self.window = ctk.CTkToplevel(root)
        self.window.title("Cards Creator — создание карточек WB / Ozon")
        self.window.minsize(620, 560)
        _center_window(self.window, 760, 720)

        self.content = ctk.CTkFrame(self.window, corner_radius=0, fg_color="transparent")
        self.content.pack(fill="both", expand=True)

        self._show_barcode_screen()

        self.window.protocol("WM_DELETE_WINDOW", self._on_close)

    # --------------------------- Утилиты ---------------------------
    def _clear(self) -> None:
        for child in self.content.winfo_children():
            child.destroy()
        # Сбрасываем ссылки на виджеты, уничтоженные вместе с экраном, чтобы
        # _set_status/_focus_barcode не обращались к «мёртвым» виджетам.
        self.status_label = None
        self.barcode_entry = None
        self.category_status = None
        self._queue_status_label = None
        self._sup_entry = None
        self.analyze_btn = None
        self._photo_screen_active = False

    def _reset_card_state(self) -> None:
        """Сбрасывает данные текущей карточки перед возвратом к вводу штрихкода.

        Очищает фото, характеристики и прочее состояние, чтобы данные предыдущей
        карточки не попадали в черновик следующей: первый _save_draft() после
        сканирования нового артикула собирает черновик именно из этих полей.
        """
        self._current = None
        self._photos = []
        self._main_var = None
        self._thumb_refs = []
        self._reconcile_fields = []
        self._reconcile_rows = []
        self._reconcile_show_all = False
        self._reconcile_show_all_var = None
        self._reconcile_exclude_docs_var = None
        self._reconcile_kiz_var = None
        self._reconcile_scroll = None
        self._restore = None
        self._restore_photos = []
        self._restore_main_var = None
        self._restore_photos_scroll = None
        self._restore_photo_status = None

    def _set_status(self, text: str) -> None:
        label = getattr(self, "status_label", None)
        if label is None:
            return
        try:
            label.configure(text=text)
        except tk.TclError:  # noqa: BLE001 — виджет мог быть уже уничтожен
            pass

    def _focus_barcode(self) -> None:
        try:
            self.barcode_entry.focus_set()
        except Exception:  # noqa: BLE001
            pass

    def _on_close(self) -> None:
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            pass
        if self.on_close is not None:
            self.on_close()

    # --------------------------- Экран 1: ввод штрихкода ---------------------------
    def _show_barcode_screen(self) -> None:
        self._clear()
        self._reset_card_state()

        top = ctk.CTkFrame(self.content, corner_radius=0, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 0))

        header = ctk.CTkLabel(
            top,
            text="Создание карточки товара",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        header.pack(side="left")

        ctk.CTkButton(
            top, text="⚙️ DeepSeek", width=130, command=self._open_deepseek_settings
        ).pack(side="right")

        hint = ctk.CTkLabel(
            self.content,
            text=(
                "Отсканируйте штрихкод или вставьте артикул (ART) из буфера обмена.\n"
                "Вставка: Ctrl+V / Ctrl+М (любая раскладка) или кнопка «Вставить».\n"
                "Поиск: Enter или кнопка «Далее»."
            ),
            justify="left",
            text_color=("gray40", "gray70"),
        )
        hint.pack(anchor="w", padx=20, pady=(8, 12))

        row = ctk.CTkFrame(self.content, corner_radius=0, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 10))

        self.barcode_entry = ctk.CTkEntry(
            row,
            placeholder_text="Штрихкод / артикул (ART)",
            font=ctk.CTkFont(size=16),
            height=42,
        )
        self.barcode_entry.pack(side="left", fill="x", expand=True)
        self.barcode_entry.bind("<Return>", lambda _event: self._submit_barcode())
        self.barcode_entry.bind("<KeyPress>", self._on_key_press)

        ctk.CTkButton(row, text="Вставить", width=110, command=self._on_paste).pack(
            side="left", padx=(10, 0)
        )
        ctk.CTkButton(row, text="Далее", width=110, command=self._submit_barcode).pack(
            side="left", padx=(10, 0)
        )

        self.status_label = ctk.CTkLabel(
            self.content,
            text="Ожидание ввода…",
            anchor="w",
            text_color=("gray40", "gray70"),
        )
        self.status_label.pack(fill="x", padx=20, pady=(0, 4))

        # Постоянная строка состояния фоновой очереди отправки карточек.
        self._queue_status_label = ctk.CTkLabel(
            self.content,
            text="",
            anchor="w",
            text_color=("gray30", "gray80"),
        )
        self._queue_status_label.pack(fill="x", padx=20, pady=(0, 16))
        self._refresh_queue_status()

        self.window.after(120, self._focus_barcode)

    def _on_key_press(self, event):
        """Обрабатывает Ctrl+V / Ctrl+М (обе раскладки) — вставка из буфера."""
        if event.state & 0x0004:  # Control
            keysym = (event.keysym or "").lower()
            if keysym in ("v", "m", "cyrillic_em"):
                return self._on_paste()
        return None

    def _on_paste(self, event=None):
        """Вставляет значение из буфера обмена в поле ввода (без авто-поиска)."""
        try:
            text = self.window.clipboard_get()
        except Exception:  # noqa: BLE001 — буфер может быть пуст/недоступен
            return "break"
        text = (text or "").strip()
        if not text:
            return "break"
        self.barcode_entry.delete(0, "end")
        self.barcode_entry.insert(0, text)
        return "break"

    def _submit_barcode(self) -> None:
        raw = (self.barcode_entry.get() or "").strip()
        if not raw:
            self._set_status("Введите или отсканируйте штрихкод / артикул.")
            return

        art, sup = DBase.extract_art_sup(raw)
        if art is None:
            messagebox.showerror(
                "Некорректное значение",
                "Не удалось распознать артикул (ART).\n"
                "Артикул должен начинаться с цифр.",
                parent=self.window,
            )
            return

        # Если по этому ART/SUP уже есть черновик — предлагаем восстановить
        # прерванную работу (до того, как он будет перезаписан).
        if self._offer_draft_restore(art, sup):
            return

        # Черновик создаётся сразу после сканирования и далее обновляется
        # при каждом изменении карточки.
        self._current = {"art": art, "sup": sup}
        self._save_draft()

        self._set_status(f"Поиск товара по ART={art}…")
        results = _search_by_art(art)
        if results:
            self._show_selection_screen(art, sup, results)
        else:
            self._show_category_screen(art, sup)

    def _offer_draft_restore(self, art, sup) -> bool:
        """Предлагает восстановить черновик, если он есть. True — восстановлено."""
        vendor_code = _compose_vendor_code(art, sup)
        draft = _get_draft(vendor_code) or _get_draft(str(art).strip())
        if draft is None:
            return False
        # Черновик только с фактом сканирования (без фото/характеристик) —
        # восстанавливать нечего.
        if not draft.get("photos") and not draft.get("characteristics"):
            return False

        photo_count = len(draft.get("photos") or [])
        char_count = len(draft.get("characteristics") or [])
        if not messagebox.askyesno(
            "Черновик найден",
            f"Найден черновик для ART={art} (SUP={draft.get('sup') or '—'}).\n"
            f"Фото: {photo_count}, характеристики: {char_count}.\n\n"
            "Восстановить прерванную работу?",
            parent=self.window,
        ):
            return False

        self._restore_from_draft(draft)
        return True

    def _restore_from_draft(self, draft: dict) -> None:
        """Восстанавливает состояние карточки из черновика."""
        art = draft.get("art")
        sup = draft.get("sup")
        category = draft.get("category")

        # Восстанавливаем только фото, файлы которых ещё существуют на диске.
        photos = []
        for p in draft.get("photos") or []:
            src = (p.get("src") or "").strip()
            if src and os.path.isfile(src):
                photos.append(
                    {
                        "src": src,
                        "name": p.get("name") or os.path.basename(src),
                        "main": bool(p.get("main")),
                        "analysis": bool(p.get("analysis")),
                    }
                )

        self._current = {"art": art, "sup": sup}
        if category:
            self._current["category"] = category
        self._photos = photos

        characteristics = draft.get("characteristics") or []
        if category and characteristics:
            # Есть распознанные характеристики — восстанавливаем конструктор полей.
            subject_id = category.get("subject_id")
            category_name = category.get("name") or "—"
            category_charcs = _load_category_charcs(subject_id)
            promt = (
                DBase.get_promt(_category_prompt_code(subject_id))
                if subject_id is not None
                else None
            )
            found = [
                {"name": c.get("name"), "value": c.get("value"), "source": "photo"}
                for c in characteristics
            ]
            self._show_reconciliation_screen(
                category_charcs, found, "", "", promt, category_name, "", ""
            )
        else:
            self._render_photo_screen()

    # --------------------------- Экран: выбор категории (новый товар) ---------------------------
    def _show_category_screen(self, art: str, sup) -> None:
        self._clear()
        self._current = {"art": art, "sup": sup}
        self._categories = _load_categories()
        self._category_sort = "count"

        sup_text = sup if sup else "—"

        header = ctk.CTkLabel(
            self.content,
            text="Новый товар — выбор категории",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        header.pack(anchor="w", padx=20, pady=(20, 4))

        info = ctk.CTkLabel(
            self.content,
            text=(
                f"ART: {art}   ·   SUP: {sup_text}\n"
                "Товар не найден в базе — выберите категорию для новой карточки."
            ),
            justify="left",
            text_color=("gray40", "gray70"),
        )
        info.pack(anchor="w", padx=20, pady=(0, 10))

        # Переключатель сортировки: по количеству товаров / по алфавиту.
        sort_row = ctk.CTkFrame(self.content, corner_radius=0, fg_color="transparent")
        sort_row.pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkLabel(sort_row, text="Сортировка:", anchor="w").pack(side="left")
        self.category_sort_btn = ctk.CTkSegmentedButton(
            sort_row,
            values=["По количеству", "По алфавиту"],
            command=self._on_category_sort_change,
        )
        self.category_sort_btn.pack(side="left", padx=(10, 0))
        self.category_sort_btn.set("По количеству")

        self.category_status = ctk.CTkLabel(
            self.content, text="", anchor="w", text_color=("gray40", "gray70")
        )
        self.category_status.pack(fill="x", padx=20, pady=(0, 4))

        self._category_scroll = ctk.CTkScrollableFrame(
            self.content, label_text="Категории"
        )
        self._category_scroll.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        self._category_list = ctk.CTkFrame(
            self._category_scroll, corner_radius=0, fg_color="transparent"
        )
        self._category_list.pack(fill="both", expand=True)
        self._rebuild_category_list()

        footer = ctk.CTkFrame(self.content, corner_radius=0, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(0, 20))
        ctk.CTkButton(
            footer, text="Назад", width=100, command=self._show_barcode_screen
        ).pack(side="right")

    def _on_category_sort_change(self, value: str) -> None:
        """Переключает сортировку категорий и перерисовывает список."""
        self._category_sort = "alpha" if value == "По алфавиту" else "count"
        self._rebuild_category_list()

    def _rebuild_category_list(self) -> None:
        """Перерисовывает список категорий согласно текущему режиму сортировки."""
        for child in self._category_list.winfo_children():
            child.destroy()

        categories = list(self._categories)
        if self._category_sort == "alpha":
            categories.sort(key=lambda c: (c["name"].lower(), -c["count"]))
        else:
            categories.sort(key=lambda c: (-c["count"], c["name"].lower()))

        if not categories:
            ctk.CTkLabel(
                self._category_list,
                text="Категорий в базе нет. Сначала выполните выгрузку DBase.",
                text_color=("gray40", "gray70"),
            ).pack(anchor="w", pady=10)
            return

        prompted_ids = _load_prompted_subject_ids()

        for cat in categories:
            row = ctk.CTkFrame(
                self._category_list, corner_radius=0, fg_color="transparent"
            )
            row.pack(fill="x", pady=3)

            text = f"{cat['name']}   ·   {cat['count']} шт."
            btn = ctk.CTkButton(
                row,
                text=text,
                anchor="w",
                height=40,
                command=lambda c=cat: self._on_select_category(c),
            )
            btn.pack(side="left", fill="x", expand=True)

            if cat.get("subject_id") in prompted_ids:
                ctk.CTkButton(
                    row,
                    text="⚙️",
                    width=44,
                    height=40,
                    command=lambda c=cat: self._on_edit_category_prompt(c),
                ).pack(side="right", padx=(6, 0))

    def _on_select_category(self, category: dict) -> None:
        """Выбирает категорию: при наличии промпта — к фото, иначе генерирует его."""
        if getattr(self, "_category_generating", False):
            return
        art = (self._current or {}).get("art")
        sup = (self._current or {}).get("sup")
        subject_id = category.get("subject_id")
        code = _category_prompt_code(subject_id) if subject_id is not None else None
        existing = DBase.get_promt(code) if code else None
        if existing is not None:
            self._show_new_product_screen(art, sup, category=category)
            return
        self._generate_and_edit_prompt(art, sup, category)

    def _generate_and_edit_prompt(self, art, sup, category: dict) -> None:
        """Генерирует промпт категории через LLM и открывает его в редакторе."""
        subject_id = category.get("subject_id")
        category_name = category.get("name") or "—"

        if not _read_deepseek_token():
            self._open_deepseek_settings()
            return

        generator = DBase.get_promt(DEEPSEEK_GENERATOR_PROMT)
        if generator is None:
            messagebox.showerror(
                "Нет генератора",
                "Универсальный промпт-генератор extract:generator не найден.",
                parent=self.window,
            )
            return

        category_charcs = _load_category_charcs(subject_id)
        self._category_generating = True
        status = getattr(self, "category_status", None)
        if status is not None:
            status.configure(text=f"Генерация промпта для категории «{category_name}»…")

        def work() -> None:
            variables = _build_generator_variables(subject_id, category_name, category_charcs)
            parsed, _raw, error = _call_deepseek_generate(generator, variables)
            self.window.after(
                0,
                lambda: self._on_prompt_generated(art, sup, category, parsed, error),
            )

        threading.Thread(target=work, daemon=True).start()

    def _on_prompt_generated(self, art, sup, category, parsed, error) -> None:
        self._category_generating = False
        status = getattr(self, "category_status", None)
        if status is not None:
            status.configure(text="")
        if error is not None or not parsed:
            messagebox.showerror(
                "Ошибка генерации",
                error or "Не удалось сгенерировать промпт категории.",
                parent=self.window,
            )
            self._show_new_product_screen(art, sup, category=category)
            return
        self._open_prompt_editor(
            category,
            parsed["prompt_text"],
            on_done=lambda: self._show_new_product_screen(art, sup, category=category),
        )

    def _on_edit_category_prompt(self, category: dict) -> None:
        """Ручное редактирование промпта категории (кнопка-шестерёнка)."""
        subject_id = category.get("subject_id")
        code = _category_prompt_code(subject_id) if subject_id is not None else None
        promt = DBase.get_promt(code) if code else None
        if promt is None:
            messagebox.showinfo(
                "Нет промпта",
                "Для этой категории промпт ещё не создан.",
                parent=self.window,
            )
            return
        self._open_prompt_editor(category, promt.get("prompt_text") or "", on_done=None)

    def _open_prompt_editor(self, category: dict, prompt_text: str, on_done=None) -> None:
        """Окно ручного редактирования системного промпта категории."""
        subject_id = category.get("subject_id")
        category_name = category.get("name") or "—"
        code = _category_prompt_code(subject_id) if subject_id is not None else None

        dialog = ctk.CTkToplevel(self.window)
        dialog.title(f"Промпт категории — {category_name}")
        dialog.minsize(520, 420)
        dialog.transient(self.window)
        dialog.grab_set()
        _center_window(dialog, 760, 600)

        ctk.CTkLabel(
            dialog,
            text=f"Системный промпт: {category_name} (subjectID {subject_id})",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=20, pady=(20, 4))

        ctk.CTkLabel(
            dialog,
            text=(
                "Подправьте текст промпта и сохраните. Он будет использоваться "
                "при распознавании фотографий этой категории."
            ),
            justify="left",
            text_color=("gray40", "gray70"),
        ).pack(anchor="w", padx=20, pady=(0, 8))

        box = ctk.CTkTextbox(dialog, wrap="word")
        box.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        box.insert("1.0", prompt_text or "")

        def _save() -> None:
            text = box.get("1.0", "end").strip()
            if not text:
                messagebox.showwarning("Пустой промпт", "Текст промпта пуст.", parent=dialog)
                return
            if code:
                try:
                    DBase.save_custom_promt(
                        code=code,
                        name=f"Извлечение — {category_name}",
                        prompt_text=text,
                    )
                except Exception as exc:  # noqa: BLE001
                    messagebox.showerror(
                        "Ошибка", f"Не удалось сохранить промпт: {exc}", parent=dialog
                    )
                    return
            dialog.destroy()
            if on_done is not None:
                on_done()

        def _cancel() -> None:
            dialog.destroy()
            if on_done is not None:
                on_done()

        row = ctk.CTkFrame(dialog, corner_radius=0, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkButton(row, text="Сохранить", width=130, command=_save).pack(side="right")
        ctk.CTkButton(row, text="Отмена", width=100, command=_cancel).pack(
            side="right", padx=(0, 10)
        )

    # --------------------------- Экран 2: выбор товара ---------------------------
    def _show_selection_screen(self, art: str, entered_sup, results: list) -> None:
        self._clear()
        self._current = {"art": art, "sup": entered_sup}

        header = ctk.CTkLabel(
            self.content,
            text=f"Найдено товаров: {len(results)} (ART {art})",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        header.pack(anchor="w", padx=20, pady=(20, 4))

        hint = ctk.CTkLabel(
            self.content,
            text=(
                "Выберите товар для редактирования карточки или создайте карточку\n"
                "с новым поставщиком (SUP)."
            ),
            justify="left",
            text_color=("gray40", "gray70"),
        )
        hint.pack(anchor="w", padx=20, pady=(0, 12))

        scroll = ctk.CTkScrollableFrame(self.content, label_text="Товары")
        scroll.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        for item in results:
            btn = ctk.CTkButton(
                scroll,
                text=_format_product(item),
                anchor="w",
                height=44,
                command=lambda i=item: self._on_select_product(i),
            )
            btn.pack(fill="x", pady=3)

        actions = ctk.CTkFrame(self.content, corner_radius=0, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(0, 20))
        ctk.CTkButton(
            actions,
            text="Новый поставщик",
            width=180,
            command=lambda: self._on_new_supplier_click(art, entered_sup, results),
        ).pack(side="left")
        ctk.CTkButton(
            actions, text="Назад", width=100, command=self._show_barcode_screen
        ).pack(side="right")

    def _on_select_product(self, item: dict) -> None:
        if item.get("is_deleted"):
            self._show_deleted_screen(item)
        else:
            self._show_edit_screen(item)

    def _on_new_supplier_click(self, art, entered_sup, results) -> None:
        """Копия карточки с новым SUP: шаблон — первая активная WB-карточка ART."""
        template = None
        for r in results:
            if r.get("platform") == "WB" and not r.get("is_deleted"):
                template = r
                break
        if template is None:
            template = next((r for r in results if r.get("platform") == "WB"), None)
        if template is None:
            messagebox.showwarning(
                "Нет шаблона", "Не найдена WB-карточка для копирования.", parent=self.window
            )
            return

        api_key = DBase.get_wb_token("CONTENT")
        if not api_key:
            messagebox.showerror(
                "Нет токена",
                "Заполните токен Wildberries (категория CONTENT) в настройках лаунчера.",
                parent=self.window,
            )
            return

        details = _load_card_details(template.get("ART"), template.get("SUP"))
        if details is None:
            messagebox.showerror(
                "Ошибка", "Не удалось прочитать данные карточки из базы.", parent=self.window
            )
            return

        self._show_restore_form(template, details, api_key, copy_mode=True)

    # --------------------------- Экран: редактирование ---------------------------
    def _show_edit_screen(self, product: dict) -> None:
        self._clear()

        header = ctk.CTkLabel(
            self.content,
            text="Редактирование карточки",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        header.pack(anchor="w", padx=20, pady=(20, 4))

        info = (
            f"Площадка: {product.get('platform')}\n"
            f"Артикул:  {product.get('vendorCode') or product.get('ART')}\n"
            f"SUP:      {product.get('SUP') or '—'}\n"
            f"Название: {product.get('title') or '—'}\n\n"
            "Режим редактирования карточки будет реализован на следующем этапе."
        )
        ctk.CTkLabel(
            self.content,
            text=info,
            justify="left",
            text_color=("gray30", "gray80"),
        ).pack(anchor="w", padx=20, pady=(0, 12))

        ctk.CTkButton(
            self.content,
            text="Назад к вводу штрихкода",
            width=220,
            command=self._show_barcode_screen,
        ).pack(anchor="w", padx=20, pady=(0, 20))

    # --------------------- Экран: восстановление удалённой карточки ---------------------
    def _show_deleted_screen(self, product: dict) -> None:
        self._clear()

        header = ctk.CTkLabel(
            self.content,
            text="Карточка удалена",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=("firebrick3", "indianred1"),
        )
        header.pack(anchor="w", padx=20, pady=(20, 4))

        info = (
            f"Площадка: {product.get('platform')}\n"
            f"Артикул:  {product.get('vendorCode') or product.get('ART')}\n"
            f"SUP:      {product.get('SUP') or '—'}\n"
            f"Название: {product.get('title') or '—'}\n\n"
            "Карточка отсутствует на площадке, но её данные сохранены в базе.\n"
            "При восстановлении будет создана новая карточка с новым nmID."
        )
        ctk.CTkLabel(
            self.content,
            text=info,
            justify="left",
            text_color=("gray30", "gray80"),
        ).pack(anchor="w", padx=20, pady=(0, 12))

        actions = ctk.CTkFrame(self.content, corner_radius=0, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(0, 20))
        ctk.CTkButton(
            actions,
            text="Восстановить",
            width=160,
            command=lambda p=product: self._on_restore_click(p),
        ).pack(side="left")
        ctk.CTkButton(
            actions,
            text="Назад",
            width=100,
            command=self._show_barcode_screen,
        ).pack(side="left", padx=(10, 0))

    def _on_restore_click(self, product: dict) -> None:
        self._set_status("Загрузка данных карточки…")
        api_key = DBase.get_wb_token("CONTENT")
        if not api_key:
            messagebox.showerror(
                "Нет токена",
                "Заполните токен Wildberries (категория CONTENT) в настройках лаунчера.",
                parent=self.window,
            )
            return

        details = _load_card_details(product.get("ART"), product.get("SUP"))
        if details is None:
            messagebox.showerror(
                "Ошибка", "Не удалось прочитать данные карточки из базы.", parent=self.window
            )
            return

        self._show_restore_form(product, details, api_key)

    # --------------------------- Вспомогательные элементы формы ---------------------------
    def _make_section(self, parent, title, collapsed=False):
        """Создаёт сворачиваемую секцию; возвращает её контентный фрейм."""
        state = {"collapsed": collapsed}

        header = ctk.CTkFrame(parent, corner_radius=6)
        header.pack(fill="x", pady=(6, 0))

        btn = ctk.CTkButton(
            header,
            text=("▸ " if collapsed else "▾ ") + title,
            anchor="w",
            height=32,
            fg_color="transparent",
            hover_color=("gray85", "gray28"),
            text_color=("gray20", "gray85"),
            command=lambda: _toggle(),
        )
        btn.pack(fill="x", padx=2, pady=2)

        body = ctk.CTkFrame(parent, corner_radius=0, fg_color="transparent")

        def _toggle():
            if state["collapsed"]:
                body.pack(fill="x", padx=8, pady=(0, 6), after=header)
                btn.configure(text="▾ " + title)
                state["collapsed"] = False
            else:
                body.pack_forget()
                btn.configure(text="▸ " + title)
                state["collapsed"] = True

        if not collapsed:
            body.pack(fill="x", padx=8, pady=(0, 6))
        return body

    def _field(self, parent, key, label, value=""):
        """Создаёт строку «подпись + поле ввода» и регистрирует поле."""
        row = ctk.CTkFrame(parent, corner_radius=0, fg_color="transparent")
        row.pack(fill="x", pady=2)
        ctk.CTkLabel(row, text=label, width=170, anchor="w").pack(side="left")
        entry = ctk.CTkEntry(row)
        entry.pack(side="left", fill="x", expand=True)
        entry.insert(0, str(value if value is not None else ""))
        self._restore["entries"][key] = entry
        return entry

    # --------------------------- Экран: форма восстановления ---------------------------
    def _show_restore_form(self, product, details, api_key, copy_mode=False) -> None:
        self._clear()
        self._restore = {
            "product": product,
            "details": details,
            "api_key": api_key,
            "entries": {},
            "charc_rows": [],
            "new_nm_id": None,
            "copy_mode": copy_mode,
        }
        self._restore_photos = []

        title = "Копия карточки (новый SUP)" if copy_mode else "Восстановление карточки"
        ctk.CTkLabel(
            self.content,
            text=title,
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(anchor="w", padx=20, pady=(20, 4))

        ctk.CTkLabel(
            self.content,
            text=(
                f"Площадка: {product.get('platform')}   ·   ART: {details.get('ART') or '—'}\n"
                "Проверьте и при необходимости отредактируйте данные. "
                "Стрелка сворачивает/разворачивает секцию."
            ),
            justify="left",
            text_color=("gray40", "gray70"),
        ).pack(anchor="w", padx=20, pady=(0, 10))

        scroll = ctk.CTkScrollableFrame(self.content, label_text="Данные карточки")
        scroll.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        main = self._make_section(scroll, "Основные", collapsed=False)
        if copy_mode:
            self._field(main, "sup", "SUP (новый)", "")
            vendor_code = str(details.get("ART") or "")
        else:
            vendor_code = details.get("vendorCode") or (
                f"{details.get('ART') or ''}"
                + (f"-{details.get('SUP')}" if details.get("SUP") else "")
            )
        self._field(main, "vendorCode", "Артикул (vendorCode)", vendor_code)
        self._field(main, "title", "Наименование", details.get("title"))
        self._field(main, "brand", "Бренд", details.get("brand"))
        self._field(main, "description", "Описание", details.get("description"))
        self._field(main, "length", "Длина, см", _fmt_dimension(details.get("length")))
        self._field(main, "width", "Ширина, см", _fmt_dimension(details.get("width")))
        self._field(main, "height", "Высота, см", _fmt_dimension(details.get("height")))
        self._field(main, "weightBrutto", "Вес брутто, кг", details.get("weightBrutto"))
        self._field(main, "price", "Цена (price)", details.get("price"))
        self._field(main, "cost", "Себестоимость (cost)", details.get("cost"))

        cat = self._make_section(scroll, "Категория и размер", collapsed=True)
        ctk.CTkLabel(
            cat,
            text=f"Предмет: {details.get('subjectID')} ({details.get('subjectName') or ''})",
            anchor="w",
            text_color=("gray30", "gray80"),
        ).pack(fill="x", pady=2)
        self._field(cat, "techSize", "Размер (techSize)", details.get("techSize") or "0")
        self._field(cat, "wbSize", "Рос. размер (wbSize)", details.get("wbSize"))

        mark = self._make_section(scroll, "Маркировка", collapsed=True)
        kiz_var = tk.BooleanVar(value=bool(details.get("kizMarked")))
        ctk.CTkCheckBox(
            mark,
            text="КИЗ-маркировка (kizMarked)",
            variable=kiz_var,
            onvalue=True,
            offvalue=False,
        ).pack(anchor="w", pady=2)
        self._restore["kiz_var"] = kiz_var

        docs = self._make_section(scroll, "Документы", collapsed=True)
        exclude_docs_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            docs,
            text="Документы не нужны",
            variable=exclude_docs_var,
            onvalue=True,
            offvalue=False,
        ).pack(anchor="w", pady=2)
        self._restore["exclude_documents_var"] = exclude_docs_var

        chars = self._make_section(scroll, "Характеристики", collapsed=False)
        charc_map = {c["charcID"]: c for c in (details.get("characteristics") or [])}
        for req in _load_required_charcs(details.get("subjectID")):
            existing = charc_map.get(req["charcID"])
            if existing is None:
                charc_map[req["charcID"]] = req
            elif not existing.get("name"):
                existing["name"] = req.get("name") or ""
        if charc_map:
            for charc in charc_map.values():
                self._restore_charc_row(chars, charc)
        else:
            ctk.CTkLabel(
                chars,
                text="Динамических характеристик нет.",
                text_color=("gray40", "gray70"),
            ).pack(anchor="w", pady=2)

        if not copy_mode:
            photos_sec = self._make_section(scroll, "Фотографии", collapsed=False)
            ctk.CTkButton(
                photos_sec, text="Выбрать фото…", width=150, command=self._restore_pick_photos
            ).pack(anchor="w", pady=(0, 4))
            self._restore_photos_scroll = ctk.CTkScrollableFrame(photos_sec, height=120)
            self._restore_photos_scroll.pack(fill="x", pady=(0, 4))
            self._restore_photo_status = ctk.CTkLabel(
                photos_sec, text="Выбрано фото: 0", anchor="w", text_color=("gray40", "gray70")
            )
            self._restore_photo_status.pack(fill="x")
            self._restore_rebuild_photos()

        self.status_label = ctk.CTkLabel(
            self.content,
            text="Отредактируйте данные и нажмите «Далее».",
            anchor="w",
            text_color=("gray40", "gray70"),
        )
        self.status_label.pack(fill="x", padx=20, pady=(0, 8))

        footer = ctk.CTkFrame(self.content, corner_radius=0, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(0, 20))
        ctk.CTkButton(
            footer, text="Далее", width=160,
            command=self._on_copy_submit if copy_mode else self._on_restore_submit,
        ).pack(side="left")
        ctk.CTkButton(
            footer, text="Назад", width=100, command=self._show_barcode_screen
        ).pack(side="left", padx=(10, 0))

        threading.Thread(
            target=self._load_charc_names_bg,
            args=(api_key, details.get("subjectID")),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._prefetch_barcodes_bg, args=(api_key,), daemon=True
        ).start()

    def _restore_charc_row(self, parent, charc) -> None:
        """Строка характеристики: имя + поле значения + тип значения."""
        row = ctk.CTkFrame(parent, corner_radius=0, fg_color="transparent")
        row.pack(fill="x", pady=2)

        label_text = charc["name"] or f"ID {charc['charcID']}"
        label = ctk.CTkLabel(row, text=label_text, width=170, anchor="w")
        label.pack(side="left")

        entry = ctk.CTkEntry(row)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        entry.insert(0, str(charc["value"]))

        default_type = "Строка" if charc["charcID"] in STRING_CHARC_IDS else "Авто"
        type_var = tk.StringVar(value=default_type)
        ctk.CTkOptionMenu(
            row,
            values=["Авто", "Строка", "Число", "Список", "Флаг"],
            variable=type_var,
            width=90,
        ).pack(side="right")

        self._restore["charc_rows"].append(
            {
                "charcID": charc["charcID"],
                "entry": entry,
                "type_var": type_var,
                "label": label,
            }
        )

    def _load_charc_names_bg(self, api_key, subject_id) -> None:
        names = _fetch_charc_names(api_key, subject_id)
        if not names:
            return

        def apply_names():
            if not self._restore:
                return
            for row in self._restore.get("charc_rows", []):
                name = names.get(row["charcID"])
                if name:
                    row["label"].configure(text=name)

        self.window.after(0, apply_names)

    # --------------------------- Отправка карточки на восстановление ---------------------------
    def _on_restore_submit(self) -> None:
        entries = self._restore["entries"]
        price_text = entries["price"].get().strip() if "price" in entries else ""
        cost_text = entries["cost"].get().strip() if "cost" in entries else ""
        vendor_code = entries["vendorCode"].get().strip() if "vendorCode" in entries else ""

        if not self._restore_photos:
            messagebox.showwarning("Нет фото", "Выберите хотя бы одно фото.", parent=self.window)
            return
        if not any(p["main"] for p in self._restore_photos):
            messagebox.showwarning("Главное фото", "Отметьте главное фото.", parent=self.window)
            return

        api_key = self._restore["api_key"]
        try:
            barcode = _take_barcode(api_key)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                "Ошибка баркода", f"Не удалось получить баркод: {exc}", parent=self.window
            )
            return
        self._restore["barcode"] = barcode

        try:
            payload = self._build_restore_payload()
        except ValueError as exc:
            messagebox.showerror("Проверьте данные", str(exc), parent=self.window)
            return

        if (
            not self._restore.get("exclude_documents_var")
            or not self._restore["exclude_documents_var"].get()
        ):
            messagebox.showinfo(
                "Документы", "Добавьте документы на сайте", parent=self.window
            )

        photos = [dict(p) for p in self._restore_photos]
        photos.sort(key=lambda p: 0 if p["main"] else 1)

        job = {
            "mode": "restore",
            "api_key": api_key,
            "vendor_code": vendor_code,
            "barcode": barcode,
            "payload": payload,
            "price_text": price_text,
            "cost_text": cost_text,
            "details": self._restore["details"],
            "photos": photos,
            "photo_urls": [],
            "stock": None,
            "oz": False,
            "new_nm_id": None,
            "new_chrt_id": None,
        }
        self._ask_stock_and_enqueue(job)

    def _on_copy_submit(self) -> None:
        """Отправка копии карточки с новым SUP (фото берутся по ссылкам из БД)."""
        entries = self._restore["entries"]
        details = self._restore["details"] or {}
        art = details.get("ART") or ""
        sup = entries["sup"].get().strip() if "sup" in entries else ""
        vendor_code = _compose_vendor_code(art, sup)
        if not vendor_code:
            messagebox.showerror(
                "Ошибка", "Укажите SUP для нового поставщика.", parent=self.window
            )
            return

        # Обновляем vendorCode в форме, чтобы _build_restore_payload собрал корректное тело.
        entries["vendorCode"].delete(0, "end")
        entries["vendorCode"].insert(0, vendor_code)

        price_text = entries["price"].get().strip() if "price" in entries else ""
        cost_text = entries["cost"].get().strip() if "cost" in entries else ""

        api_key = self._restore["api_key"]
        try:
            barcode = _take_barcode(api_key)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                "Ошибка баркода", f"Не удалось получить баркод: {exc}", parent=self.window
            )
            return
        self._restore["barcode"] = barcode

        try:
            payload = self._build_restore_payload()
        except ValueError as exc:
            messagebox.showerror("Проверьте данные", str(exc), parent=self.window)
            return

        if (
            not self._restore.get("exclude_documents_var")
            or not self._restore["exclude_documents_var"].get()
        ):
            messagebox.showinfo(
                "Документы", "Добавьте документы на сайте", parent=self.window
            )

        photo_urls = _load_card_photo_urls(art, details.get("SUP"))
        if not photo_urls:
            messagebox.showwarning(
                "Нет фото",
                "У карточки-шаблона не найдены ссылки на фото в БД.",
                parent=self.window,
            )
            return

        job = {
            "mode": "copy",
            "api_key": api_key,
            "vendor_code": vendor_code,
            "barcode": barcode,
            "payload": payload,
            "price_text": price_text,
            "cost_text": cost_text,
            "details": {"art": art, "sup": sup},
            "photos": [],
            "photo_urls": photo_urls,
            "stock": None,
            "oz": False,
            "new_nm_id": None,
            "new_chrt_id": None,
        }
        self._ask_stock_and_enqueue(job)

    def _build_restore_payload(self) -> list:
        """Собирает тело POST /content/v2/cards/upload из данных формы."""
        entries = self._restore["entries"]
        details = self._restore["details"]

        def get(name):
            return (entries[name].get() if name in entries else "").strip()

        vendor_code = get("vendorCode")
        if not vendor_code:
            raise ValueError("Укажите артикул (vendorCode).")
        if not get("title"):
            raise ValueError("Укажите наименование товара.")

        subject_id = details.get("subjectID")
        if subject_id is None:
            raise ValueError("Не задан subjectID карточки.")

        dimensions = {
            "length": _to_int(get("length")),
            "width": _to_int(get("width")),
            "height": _to_int(get("height")),
            "weightBrutto": _to_number(get("weightBrutto")),
        }

        type_map = {
            "Авто": "auto",
            "Строка": "str",
            "Число": "num",
            "Список": "list",
            "Флаг": "bool",
        }
        characteristics = []
        for row in self._restore.get("charc_rows", []):
            raw = row["entry"].get().strip()
            if raw == "":
                continue
            charc_id = row["charcID"]
            if charc_id in STRING_CHARC_IDS:
                type_hint = "str"
            else:
                type_hint = type_map.get(row["type_var"].get(), "auto")
            characteristics.append(
                {"id": charc_id, "value": _parse_char_value(raw, type_hint)}
            )

        # Документы: пока что захардкожено. Галочка «Документы не нужны» по
        # умолчанию включена → excludeDocuments = true. Если снять галочку —
        # excludeDocuments = false (документы требуются, добавить на сайте).
        exclude_documents = bool(
            self._restore.get("exclude_documents_var")
            and self._restore["exclude_documents_var"].get()
        )
        documents = {"items": [], "excludeDocuments": exclude_documents}

        price = _to_number(get("price")) or 0
        size = {"techSize": get("techSize") or "0"}
        wb_size = get("wbSize")
        if wb_size:
            size["wbSize"] = wb_size
        size["price"] = price
        size["skus"] = [self._restore.get("barcode")]

        variant = {
            "vendorCode": vendor_code,
            "kizMarked": bool(
                self._restore.get("kiz_var") and self._restore["kiz_var"].get()
            ),
            "title": get("title"),
            "description": get("description"),
            "brand": get("brand"),
            "dimensions": dimensions,
            "documents": documents,
            "characteristics": characteristics,
            "sizes": [size],
        }

        return [{"subjectID": int(subject_id), "variants": [variant]}]

    def _do_upload_card(self, job) -> None:
        """Синхронно отправляет карточку на создание (выполняется в воркере)."""
        response = _wb_http(
            "POST", CARDS_UPLOAD_URL, job["api_key"], json_body=job["payload"]
        )
        data = _safe_json(response)
        if data.get("error"):
            raise RuntimeError(data.get("errorText") or "Ошибка создания карточки.")

    def _prefetch_barcodes_bg(self, api_key) -> None:
        try:
            if not _load_barcode_pool():
                pool = _fetch_barcodes(api_key, BARCODE_BATCH_SIZE)
                if pool:
                    _save_barcode_pool(pool)
        except Exception:  # noqa: BLE001
            _logger.warning("Не удалось предзагрузить пул баркодов", exc_info=True)

    # --------------------------- Экран: выбор фото (восстановление) ---------------------------
    def _restore_pick_photos(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Выберите фотографии товара",
            parent=self.window,
            initialdir=_ensure_photos_drop_dir(),
            filetypes=PHOTO_FILETYPES,
        )
        if not paths:
            return
        existing = {p["src"] for p in self._restore_photos}
        for path in paths:
            path = os.path.abspath(path)
            if path in existing:
                continue
            self._restore_photos.append({"src": path, "name": os.path.basename(path), "main": False})
            existing.add(path)
        if self._restore_photos and not any(p["main"] for p in self._restore_photos):
            self._restore_photos[0]["main"] = True
        self._restore_rebuild_photos()

    def _restore_rebuild_photos(self) -> None:
        for child in self._restore_photos_scroll.winfo_children():
            child.destroy()

        main_idx = "0"
        for i, p in enumerate(self._restore_photos):
            if p["main"]:
                main_idx = str(i)
                break
        self._restore_main_var = tk.StringVar(value=main_idx if self._restore_photos else "")

        for i, p in enumerate(self._restore_photos):
            row = ctk.CTkFrame(self._restore_photos_scroll)
            row.pack(fill="x", pady=2)
            ctk.CTkRadioButton(
                row,
                text="Главная",
                variable=self._restore_main_var,
                value=str(i),
                width=30,
                command=lambda idx=i: self._restore_set_main(idx),
            ).pack(side="left", padx=(4, 8))
            ctk.CTkLabel(
                row, text=p["name"], anchor="w", text_color=("gray30", "gray80")
            ).pack(side="left", fill="x", expand=True)

        self._restore_update_photo_status()

    def _restore_set_main(self, idx: int) -> None:
        for i, p in enumerate(self._restore_photos):
            p["main"] = (i == idx)

    def _restore_update_photo_status(self) -> None:
        if hasattr(self, "_restore_photo_status"):
            self._restore_photo_status.configure(
                text=f"Выбрано фото: {len(self._restore_photos)}"
            )

    # --------------------------- Фоновая очередь отправки карточек ---------------------------
    def _post_job_status(self, msg: str) -> None:
        """Запоминает сообщение и обновляет строку статуса очереди (потокобезопасно)."""
        self._queue_status_msg = msg
        try:
            self.window.after(0, self._refresh_queue_status)
        except tk.TclError:  # noqa: BLE001 — окно могло быть закрыто
            pass

    def _refresh_queue_status(self) -> None:
        """Перерисовывает постоянную строку статуса очереди на экране штрихкода."""
        label = getattr(self, "_queue_status_label", None)
        if label is None:
            return
        try:
            qsize = self._job_queue.qsize()
            msg = self._queue_status_msg or ""
            if qsize:
                text = f"В очереди: {qsize}" + (f"  ·  {msg}" if msg else "")
            else:
                text = msg or "Очередь пуста"
            label.configure(text=text)
        except tk.TclError:  # noqa: BLE001 — окно могло быть закрыто
            pass

    def _start_worker(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

    def _worker_loop(self) -> None:
        while True:
            job = self._job_queue.get()
            try:
                self._process_job(job)
            finally:
                self._job_queue.task_done()
                try:
                    self.window.after(0, self._refresh_queue_status)
                except tk.TclError:  # noqa: BLE001 — окно могло быть закрыто
                    pass

    def _process_job(self, job) -> None:
        """Прогоняет одну задачу через весь конвейер (выполняется в потоке-воркере)."""
        vendor = job.get("vendor_code") or "?"
        try:
            self._post_job_status(f"Отправка карточки {vendor}…")
            self._do_upload_card(job)

            self._post_job_status(f"{vendor}: ожидание создания…")
            card = self._poll_card_created(job["api_key"], vendor)
            job["new_nm_id"] = card.get("nmID")
            job["new_chrt_id"] = _first_chrt_id(card)

            self._post_job_status(f"{vendor}: загрузка медиа…")
            self._do_upload_photos(job)

            if job.get("stock") is not None:
                self._post_job_status(f"{vendor}: установка остатков…")
                self._do_set_stock(job)

            self._post_job_status(f"{vendor}: готово.")
            self._finalize_card(job)
        except _CardDraftError as exc:
            self._report_job_error(vendor, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._report_job_error(vendor, str(exc))

    def _finalize_card(self, job) -> None:
        """После успешной отправки актуализирует БД и удаляет черновик с фото."""
        vendor = job.get("vendor_code") or "?"
        self._post_job_status(f"{vendor}: обновление базы данных…")
        try:
            DBase.run()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Не удалось обновить БД после %s: %s", vendor, exc)

        if not _card_in_db(vendor):
            _logger.warning(
                "Карточка %s не найдена в БД после обновления — черновик не удалён.",
                vendor,
            )
            self._post_job_status(f"{vendor}: карточка не найдена в БД")
            return

        _delete_draft(vendor)
        # Черновик мог быть создан на этапе сканирования с ключом только по ART
        # (если SUP ещё не был введён) — подчищаем и такой вариант.
        details = job.get("details") or {}
        art = (details.get("art") or details.get("ART") or "").strip()
        if art:
            _delete_draft(art)
        _delete_card_photos(job.get("photos") or [])
        self._post_job_status(f"{vendor}: черновик и исходные фото удалены.")

    def _report_job_error(self, vendor: str, msg: str) -> None:
        _logger.warning("Ошибка обработки карточки %s: %s", vendor, msg)
        self._post_job_status(f"{vendor}: ошибка")
        def _show():
            messagebox.showerror("Ошибка отправки карточки", msg, parent=self.window)
        try:
            self.window.after(0, _show)
        except tk.TclError:  # noqa: BLE001 — окно могло быть закрыто
            pass

    def _enqueue(self, job) -> None:
        """Ставит задачу в очередь и сразу возвращает пользователя на ввод штрихкода."""
        self._job_queue.put(job)
        self._start_worker()
        self._post_job_status("Поставлено в очередь")
        self._show_barcode_screen()

    def _do_upload_photos(self, job) -> None:
        """Загружает медиа карточки: по ссылкам (media/save) либо локальными файлами."""
        api_key = job["api_key"]
        nm_id = job.get("new_nm_id")
        if nm_id is None:
            raise RuntimeError("Карточка создана, но nmID не получен.")

        urls = job.get("photo_urls") or []
        if urls:
            self._post_job_status(f"Загрузка {len(urls)} фото по ссылкам…")
            self._save_media_by_urls(api_key, nm_id, urls)
            expected = len(urls)
        else:
            photos = job.get("photos") or []
            total = len(photos)
            for idx, photo in enumerate(photos, start=1):
                self._post_job_status(f"Загрузка фото {idx}/{total}…")
                self._upload_photo(api_key, nm_id, idx, photo["src"])
            expected = total

        self._post_job_status("Проверка загруженных фото…")
        self._wait_photos_uploaded(api_key, nm_id, expected)
        self._persist_price_cost(job)

    def _poll_card_created(self, api_key, vendor_code):
        """Опрашивает cards/list и cards/error/list по разрежённому расписанию.

        Расписание (экономия токенов): первый запрос карточки через 15 с, затем
        два запроса с шагом 5 с, далее карточка и черновик чередуются с шагом 3 с;
        после минуты ожидания шаг снова 5 с. Общий таймаут CARD_CREATE_TIMEOUT.

        Возвращает карточку при успехе; при ошибках черновика бросает
        _CardDraftError, по таймауту — RuntimeError.
        """
        start = time.monotonic()
        deadline = start + CARD_CREATE_TIMEOUT

        # Фаза 1: три проверки карточки — через 15 с, затем дважды через 5 с.
        for delay in (CARD_FIRST_DELAY, *CARD_EARLY_DELAYS):
            if not _sleep_or_timeout(delay, deadline):
                raise RuntimeError("Карточка не создана за отведённое время.")
            card = _find_card_in_list(api_key, vendor_code)
            if card:
                return card

        # Фаза 2: чередование «черновик / карточка». Первым идёт черновик.
        check_draft = True
        while True:
            elapsed = time.monotonic() - start
            interval = (
                CARD_FAST_INTERVAL if elapsed < CARD_SLOW_AFTER else CARD_SLOW_INTERVAL
            )
            if not _sleep_or_timeout(interval, deadline):
                raise RuntimeError("Карточка не создана за отведённое время.")
            if check_draft:
                errors = _check_card_errors(api_key, vendor_code)
                if errors:
                    raise _CardDraftError(errors)
            else:
                card = _find_card_in_list(api_key, vendor_code)
                if card:
                    return card
            check_draft = not check_draft

    def _upload_photo(self, api_key, nm_id, photo_number, src) -> None:
        headers = {"X-Nm-Id": str(nm_id), "X-Photo-Number": str(photo_number)}
        with open(src, "rb") as file_handle:
            files = {
                "uploadfile": (os.path.basename(src), file_handle, _content_type(src))
            }
            response = _wb_http(
                "POST", MEDIA_FILE_URL, api_key, headers=headers, files=files
            )
        data = _safe_json(response)
        if data.get("error"):
            raise RuntimeError(data.get("errorText") or f"Ошибка загрузки фото {photo_number}.")

    def _wait_photos_uploaded(self, api_key, nm_id, expected) -> None:
        """Ждёт, пока карточка вернёт expected фото: первый запрос через 20 с, затем каждые 5 с."""
        deadline = time.monotonic() + PHOTO_VERIFY_TIMEOUT
        delay = PHOTO_FIRST_DELAY
        while True:
            if not _sleep_or_timeout(delay, deadline):
                raise RuntimeError("Не удалось подтвердить загрузку фото.")
            delay = PHOTO_VERIFY_INTERVAL
            card = _get_card_by_nmid(api_key, nm_id)
            if card:
                photos = card.get("photos") or []
                if len(photos) >= expected:
                    return

    def _persist_price_cost(self, job) -> None:
        """Сохраняет цену/себестоимость локально.

        Восстановление: обновляет существующую строку wb_products (ART+SUP).
        Создание/копия: карточки ещё нет в БД, поэтому цена/себестоимость откладываются
        во временный файл и переносятся в БД, когда карточка появится (см.
        _flush_pending_costs).
        """
        price = _to_number(job.get("price_text"))
        cost = _to_number(job.get("cost_text"))
        if price is None and cost is None:
            return

        if job.get("mode") == "restore":
            art = (job.get("details") or {}).get("ART")
            sup = (job.get("details") or {}).get("SUP")
            try:
                conn = sqlite3.connect(DB_PATH)
                try:
                    if sup is not None:
                        conn.execute(
                            "UPDATE wb_products SET price = ?, cost = ? WHERE ART = ? AND SUP = ?",
                            (price, cost, art, sup),
                        )
                    else:
                        conn.execute(
                            "UPDATE wb_products SET price = ?, cost = ? WHERE ART = ? AND SUP IS NULL",
                            (price, cost, art),
                        )
                    conn.commit()
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                _logger.warning("Не удалось сохранить цену/себестоимость: %s", exc)
            return

        details = job.get("details") or {}
        _store_pending_cost(
            job.get("vendor_code"),
            details.get("art"),
            details.get("sup"),
            price,
            cost,
        )

    # --------------------------- Ввод остатка/Ozon и установка остатков ---------------------------
    def _ask_stock_and_enqueue(self, job) -> None:
        """Запрашивает остаток и флаг Ozon, затем ставит задачу в фоновую очередь."""
        dialog = ctk.CTkToplevel(self.window)
        dialog.title("Остаток и Ozon")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()
        _center_window(dialog, 440, 220)

        ctk.CTkLabel(dialog, text="Остаток (шт) для карточки:").pack(
            anchor="w", padx=20, pady=(20, 4)
        )
        entry = ctk.CTkEntry(dialog)
        entry.pack(fill="x", padx=20, pady=(0, 8))
        entry.insert(0, "0")
        entry.focus_set()

        oz_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            dialog, text="Добавить на Ozon", variable=oz_var, onvalue=True, offvalue=False
        ).pack(anchor="w", padx=20, pady=(0, 8))

        def on_ok():
            try:
                amount = int(entry.get().strip())
            except ValueError:
                messagebox.showerror("Ошибка", "Введите целое число.", parent=dialog)
                return
            dialog.destroy()
            job["stock"] = amount
            job["oz"] = bool(oz_var.get())
            self._enqueue(job)

        def on_skip():
            dialog.destroy()
            job["stock"] = None
            job["oz"] = False
            self._enqueue(job)

        row = ctk.CTkFrame(dialog, corner_radius=0, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkButton(row, text="ОК", width=120, command=on_ok).pack(side="left")
        ctk.CTkButton(row, text="Пропустить", width=120, command=on_skip).pack(
            side="left", padx=(10, 0)
        )

    def _do_set_stock(self, job) -> None:
        """Устанавливает остаток (не фатально: ошибка логируется, но не роняет задачу)."""
        chrt_id = job.get("new_chrt_id")
        vendor = job.get("vendor_code") or "?"
        master = DBase.get_wb_token("MASTER")
        warehouse = (DBase.get_wb_warehouse_ids() or [None])[0]
        try:
            if not chrt_id or not warehouse or not master:
                raise RuntimeError("Нет chrtID/склада/токена для установки остатков.")
            url = f"{WB_MARKETPLACE_API}/api/v3/stocks/{warehouse}"
            payload = {"stocks": [{"chrtId": chrt_id, "amount": job["stock"]}]}
            response = _wb_http("PUT", url, master, json_body=payload, rate_category="STOCKS")
            data = _safe_json(response)
            if data.get("error"):
                raise RuntimeError(data.get("errorText") or "Ошибка обновления остатков.")
            self._post_job_status(f"{vendor}: остатки установлены.")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Не удалось установить остатки для %s: %s", vendor, exc)
            self._post_job_status(f"{vendor}: остатки не установлены")

    # --------------------------- Экран 3: новый товар / фото ---------------------------
    def _show_new_product_screen(self, art: str, sup, category: dict | None = None) -> None:
        self._current = {"art": art, "sup": sup}
        if category:
            self._current["category"] = category
        self._photos = []
        self._render_photo_screen()
        self._save_draft()

    def _render_photo_screen(self) -> None:
        self._clear()
        art = (self._current or {}).get("art") or "—"
        sup = (self._current or {}).get("sup")
        category = (self._current or {}).get("category")

        photos_dir = _ensure_photos_drop_dir()

        header = ctk.CTkLabel(
            self.content,
            text="Новый товар — подготовка фото",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        header.pack(anchor="w", padx=20, pady=(20, 4))

        info_lines = [
            f"ART: {art}",
        ]
        if category:
            info_lines.append(
                f"Категория: {category['name']} (ID {category['subject_id']})"
            )
        info_lines.append(f"Исходные фото: {photos_dir}")
        info_lines.append("Фото будут закреплены за ART как <ART>_1, <ART>_2, …")

        info = ctk.CTkLabel(
            self.content,
            text="\n".join(info_lines),
            justify="left",
            text_color=("gray30", "gray80"),
        )
        info.pack(anchor="w", padx=20, pady=(0, 6))

        # Поле SUP: заполняется вручную (или подставляется, если SUP уже известен).
        sup_row = ctk.CTkFrame(self.content, corner_radius=0, fg_color="transparent")
        sup_row.pack(fill="x", padx=20, pady=(0, 10))
        ctk.CTkLabel(sup_row, text="SUP (поставщик):", width=150, anchor="w").pack(side="left")
        self._sup_entry = ctk.CTkEntry(sup_row, width=260)
        self._sup_entry.pack(side="left", padx=(8, 0))
        self._sup_entry.insert(0, sup if sup else "")
        self._sup_entry.bind("<KeyRelease>", lambda _e: self._sync_sup_to_current())

        buttons = ctk.CTkFrame(self.content, corner_radius=0, fg_color="transparent")
        buttons.pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkButton(
            buttons,
            text="Открыть папку PhotosDrop",
            width=210,
            command=lambda: _open_folder(photos_dir),
        ).pack(side="left")
        ctk.CTkButton(
            buttons, text="Выбрать фото…", width=140, command=self._pick_photos
        ).pack(side="left", padx=(10, 0))

        # Нижние управляющие элементы размещаем в отдельном контейнере, который
        # «прижимаем» к низу окна (side="bottom"), а прокручиваемую сетку фото
        # упаковываем ПОСЛЕДНЕЙ с expand=True. Тогда при нехватке высоты первым
        # сжимается именно список фото, а кнопки «Все на анализ» / «Снять анализ» /
        # «Очистить» / «Анализ DeepSeek» / «Назад» всегда остаются видимыми.
        bottom = ctk.CTkFrame(self.content, corner_radius=0, fg_color="transparent")
        bottom.pack(side="bottom", fill="x")

        quick = ctk.CTkFrame(bottom, corner_radius=0, fg_color="transparent")
        quick.pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkButton(
            quick, text="Все на анализ", width=130, command=self._mark_all_analysis
        ).pack(side="left")
        ctk.CTkButton(
            quick, text="Снять анализ", width=130, command=self._unmark_analysis
        ).pack(side="left", padx=(6, 0))
        ctk.CTkButton(
            quick, text="Очистить", width=100, command=self._clear_photos
        ).pack(side="left", padx=(6, 0))

        self.photo_status = ctk.CTkLabel(
            bottom,
            text=f"Выбрано фото: {len(self._photos)}",
            anchor="w",
            text_color=("gray40", "gray70"),
        )
        self.photo_status.pack(fill="x", padx=20, pady=(0, 4))

        footer = ctk.CTkFrame(bottom, corner_radius=0, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(0, 20))
        self.analyze_btn = ctk.CTkButton(
            footer, text="Анализ DeepSeek", width=150, command=self._analyze_photos
        )
        self.analyze_btn.pack(side="left")
        ctk.CTkButton(
            footer, text="Назад", width=100, command=self._show_barcode_screen
        ).pack(side="right")

        # Сетка фото создаётся и заполняется после нижних кнопок, чтобы любая
        # ошибка построения миниатюр не лишала пользователя управления экраном.
        self._photos_scroll = ctk.CTkScrollableFrame(self.content, label_text="Фотографии")
        self._photos_scroll.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        try:
            self._rebuild_photo_list()
        except Exception:  # noqa: BLE001 — не роняем экран из-за ошибки миниатюр
            _logger.exception("Не удалось построить сетку фото")

        # Enter на экране подготовки фото запускает анализ DeepSeek.
        self._photo_screen_active = True
        self._bind_enter_to_analyze()

    # --------------------------- Enter на экране фото ---------------------------
    def _bind_enter_to_analyze(self) -> None:
        """Привязывает Enter на всех виджетах экрана фото к запуску анализа DeepSeek.

        Привязка выполняется рекурсивно по всем потомкам self.content, чтобы Enter
        срабатывал независимо от того, какой элемент экрана сейчас в фокусе.
        """

        def _bind_recursive(widget) -> None:
            try:
                widget.bind("<Return>", self._on_photo_enter)
            except tk.TclError:  # noqa: BLE001 — виджет мог быть уже уничтожен
                return
            for child in widget.winfo_children():
                _bind_recursive(child)

        _bind_recursive(self.content)

    def _on_photo_enter(self, event):
        """Обработчик Enter на экране фото: запускает анализ DeepSeek."""
        if not getattr(self, "_photo_screen_active", False):
            return None
        self._analyze_photos()
        return "break"

    # --------------------------- Разметка фотографий ---------------------------
    def _rebuild_photo_list(self) -> None:
        """Перерисовывает сетку фото (3 в ряд) с превью и «Главная»/«Анализ»."""
        for child in self._photos_scroll.winfo_children():
            child.destroy()
        self._thumb_refs = []

        main_idx = "0"
        for i, p in enumerate(self._photos):
            if p.get("main"):
                main_idx = str(i)
                break
        self._main_var = tk.StringVar(value=main_idx if self._photos else "")

        columns = 3
        for col in range(columns):
            self._photos_scroll.grid_columnconfigure(col, weight=1, uniform="photo_col")

        for i, p in enumerate(self._photos):
            row = i // columns
            col = i % columns

            card = ctk.CTkFrame(self._photos_scroll)
            card.grid(row=row, column=col, padx=4, pady=4, sticky="nsew")

            # Миниатюра-предпросмотр (letterbox 80×80).
            thumb = _load_thumbnail(p["src"], size=(80, 80))
            if thumb is not None:
                thumb_img = ctk.CTkImage(
                    light_image=thumb, dark_image=thumb, size=(80, 80)
                )
                self._thumb_refs.append(thumb_img)
                ctk.CTkLabel(card, image=thumb_img, text="", width=80, height=80).pack(
                    pady=(8, 4)
                )
            else:
                ctk.CTkLabel(
                    card,
                    text="Нет\nпревью",
                    width=80,
                    height=80,
                    corner_radius=6,
                    fg_color=("gray85", "gray30"),
                    text_color=("gray40", "gray70"),
                ).pack(pady=(8, 4))

            ctk.CTkLabel(
                card, text=p["name"], anchor="w", text_color=("gray30", "gray80")
            ).pack(fill="x", padx=6)

            controls = ctk.CTkFrame(card, corner_radius=0, fg_color="transparent")
            controls.pack(fill="x", padx=6, pady=(2, 8))

            ctk.CTkRadioButton(
                controls,
                text="Главная",
                variable=self._main_var,
                value=str(i),
                width=30,
                command=lambda idx=i: self._set_main(idx),
            ).pack(side="left", padx=(0, 4))

            var = tk.BooleanVar(value=bool(p.get("analysis", False)))
            ctk.CTkCheckBox(
                controls,
                text="Анализ",
                variable=var,
                onvalue=True,
                offvalue=False,
                width=30,
                command=lambda idx=i, v=var: self._set_analysis(idx, bool(v.get())),
            ).pack(side="left")

        self._update_photo_status()
        self._bind_enter_to_analyze()

    def _update_photo_status(self) -> None:
        if hasattr(self, "photo_status"):
            self.photo_status.configure(text=f"Выбрано фото: {len(self._photos)}")

    def _read_sup(self):
        """Возвращает текущий SUP из поля ввода (или из _current, если поля нет)."""
        entry = getattr(self, "_sup_entry", None)
        if entry is not None:
            try:
                value = entry.get().strip()
            except tk.TclError:  # noqa: BLE001 — виджет мог быть уничтожен
                value = ""
            return value or None
        return (self._current or {}).get("sup")

    def _sync_sup_to_current(self) -> None:
        """Переносит SUP из поля ввода в self._current (перед уходом с экрана фото)."""
        if self._current is not None:
            self._current["sup"] = self._read_sup()
            self._save_draft()

    def _set_main(self, idx: int) -> None:
        for i, p in enumerate(self._photos):
            p["main"] = (i == idx)
        self._save_draft()

    def _set_analysis(self, idx: int, value: bool) -> None:
        if 0 <= idx < len(self._photos):
            self._photos[idx]["analysis"] = value
        self._save_draft()

    def _mark_all_analysis(self) -> None:
        for p in self._photos:
            p["analysis"] = True
        self._rebuild_photo_list()
        self._save_draft()

    def _unmark_analysis(self) -> None:
        for p in self._photos:
            p["analysis"] = False
        self._rebuild_photo_list()
        self._save_draft()

    def _clear_photos(self) -> None:
        self._photos = []
        self._rebuild_photo_list()
        self._save_draft()

    def _pick_photos(self) -> None:
        initial = _ensure_photos_drop_dir()
        paths = filedialog.askopenfilenames(
            title="Выберите фотографии товара",
            parent=self.window,
            initialdir=initial,
            filetypes=PHOTO_FILETYPES,
        )
        if not paths:
            return

        existing = {p["src"] for p in self._photos}
        added = []
        for path in paths:
            path = os.path.abspath(path)
            if path in existing:
                continue
            p = {"src": path, "name": os.path.basename(path),
                 "main": False, "analysis": False}
            self._photos.append(p)
            existing.add(path)
            added.append(p)

        # Если главного фото ещё нет — назначаем первое.
        if added and not any(p.get("main") for p in self._photos):
            self._photos[0]["main"] = True

        self._rebuild_photo_list()
        self._save_draft()

    # --------------------------- Сохранение черновика ---------------------------
    def _save_draft(self) -> None:
        """Автоматически записывает текущее состояние карточки в черновик.

        Вызывается после каждого изменения (сканирование, SUP, категория, фото,
        характеристики, описание), чтобы в случае сбоя можно было восстановить
        всю проделанную работу. Удаляется только после успешного создания карточки.
        """
        if self._current is None:
            return
        art = self._current.get("art")
        sup = self._current.get("sup")
        category = self._current.get("category")
        vendor_code = _compose_vendor_code(art, sup)

        photos = []
        for p in getattr(self, "_photos", []) or []:
            photos.append(
                {
                    "name": p.get("name"),
                    "src": p.get("src"),
                    "main": bool(p.get("main")),
                    "analysis": bool(p.get("analysis")),
                }
            )

        characteristics = []
        description = ""
        for f in getattr(self, "_reconcile_fields", []) or []:
            name = (f.get("name") or "").strip()
            value = f.get("value")
            if name.lower() == "описание":
                description = (value or "").strip()
            var = f.get("var")
            selected = True
            if var is not None:
                try:
                    selected = bool(var.get())
                except tk.TclError:  # noqa: BLE001 — виджет мог быть уничтожен
                    selected = False
            if selected and name:
                characteristics.append({"name": name, "value": value})

        _upsert_draft(
            {
                "vendor_code": vendor_code,
                "art": art,
                "sup": sup,
                "category": category,
                "mode": "create",
                "photos": photos,
                "characteristics": characteristics,
                "description": description,
            }
        )

    # --------------------------- Анализ фото через DeepSeek ---------------------------
    def _analyze_photos(self) -> None:
        if getattr(self, "_analysis_busy", False):
            return
        self._sync_sup_to_current()

        analysis = [p for p in self._photos if p.get("analysis")]
        if not analysis:
            messagebox.showwarning(
                "Нет фото для анализа",
                "Отметьте галочкой «Анализ» хотя бы одно фото.",
                parent=self.window,
            )
            return

        if not _read_deepseek_token():
            self._open_deepseek_settings()
            return

        # Категория товара и характеристики этой категории из БД (wb_charcs).
        category = (self._current or {}).get("category")
        subject_id = category.get("subject_id") if category else None
        category_name = (category.get("name") if category else "") or "—"
        category_charcs = _load_category_charcs(subject_id)

        # Индивидуальный промпт категории (если уже был сгенерирован ранее).
        category_promt = None
        if subject_id is not None:
            category_promt = DBase.get_promt(_category_prompt_code(subject_id))

        self._analysis_busy = True
        if getattr(self, "analyze_btn", None) is not None:
            self.analyze_btn.configure(state="disabled", text="Анализ…")
        if getattr(self, "photo_status", None) is not None:
            self.photo_status.configure(text="Подготовка промпта категории…")

        image_paths = [p["src"] for p in analysis]

        def work() -> None:
            promt, system_prompt, note = _resolve_category_prompt(
                subject_id, category_name, category_charcs, category_promt
            )
            if promt is None:
                self.window.after(
                    0,
                    lambda: self._on_analysis_done(
                        None, None, "Не найден промпт для анализа.", None,
                        category_charcs, category_name, "", "", "", None,
                    ),
                )
                return
            if getattr(self, "photo_status", None) is not None:
                self.window.after(
                    0,
                    lambda: self.photo_status.configure(
                        text=f"Отправка {len(analysis)} фото в DeepSeek…"
                    ),
                )

            desc_promt = (
                DBase.get_promt("description:photo")
                or DBase.get_promt("description:standard")
            )

            def run_extract():
                return _call_deepseek_extract(promt, image_paths, system_prompt)

            def run_description():
                if desc_promt is None:
                    return None, "Промпт описания не найден в таблице promts."
                return _call_deepseek_description_from_photo(desc_promt, image_paths)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                extract_future = executor.submit(run_extract)
                description_future = executor.submit(run_description)
                content_text, raw_pretty, error = extract_future.result()
                description_text, desc_error = description_future.result()

            self.window.after(
                0,
                lambda: self._on_analysis_done(
                    content_text, raw_pretty, error, promt,
                    category_charcs, category_name, system_prompt, note,
                    description_text, desc_error,
                ),
            )

        threading.Thread(target=work, daemon=True).start()

    def _on_analysis_done(
        self, content_text, raw_pretty, error, promt,
        category_charcs, category_name, system_prompt, note="",
        description_text="", desc_error=None,
    ) -> None:
        self._analysis_busy = False
        if getattr(self, "analyze_btn", None) is not None:
            self.analyze_btn.configure(state="normal", text="Анализ DeepSeek")

        if error:
            if getattr(self, "photo_status", None) is not None:
                self.photo_status.configure(text="Ошибка DeepSeek")
            messagebox.showerror("Ошибка DeepSeek", error, parent=self.window)
            return

        if desc_error:
            _logger.warning("Описание не сгенерировано при анализе: %s", desc_error)

        found = _parse_characteristics(content_text)
        self._show_reconciliation_screen(
            category_charcs, found, content_text, raw_pretty,
            promt, category_name, system_prompt, note, description_text,
        )

    # --------------------------- Справочник брендов (brands) ---------------------------
    def _find_brand_field(self):
        """Возвращает поле «Бренд» (passport, json_key == 'brand') или None."""
        for f in getattr(self, "_reconcile_fields", []) or []:
            if f["kind"] == "passport" and DBase.PASSPORT_CHARC_MAP.get(f["charcID"]) == "brand":
                return f
        return None

    def _apply_resolved_brand(self, brand_row):
        """Подставляет точное имя WB из справочника в поле «Бренд» (и в его виджет)."""
        field = self._find_brand_field()
        if brand_row is None:
            return
        self._matched_brand = brand_row
        wb = (brand_row.get("wb_name") or "").strip()
        if field is None or not wb:
            return
        field["value"] = wb
        entry = field.get("value_entry")
        if entry is not None:
            try:
                entry.delete(0, "end")
                entry.insert(0, wb)
            except tk.TclError:  # noqa: BLE001 — виджет мог быть уничтожен
                pass

    def _resolve_brand_for_next(self, api_key, subject_id):
        """Проверка бренда перед отправкой карточки (вызывается по кнопке «Далее»).

        Берёт значение из поля «Бренд»; если бренд есть в справочнике — подставляет
        точное имя WB и сразу продолжает. Если нет — открывает окно создания нового
        бренда, после чего продолжает с подставленным значением.
        """
        field = self._find_brand_field()
        raw_brand = (field.get("value") or "").strip() if field is not None else ""
        if not raw_brand:
            self._finalize_reconcile(api_key, subject_id)
            return

        try:
            brand_row = search_brand_by_alias(raw_brand)
        except sqlite3.Error as exc:
            _logger.warning("Не удалось найти бренд в справочнике: %s", exc)
            brand_row = None

        if brand_row is not None:
            self._apply_resolved_brand(brand_row)
            self._finalize_reconcile(api_key, subject_id)
        else:
            self._prompt_new_brand(
                raw_brand,
                lambda row: self._on_brand_resolved_for_next(row, api_key, subject_id),
            )

    def _on_brand_resolved_for_next(self, brand_row, api_key, subject_id):
        """Продолжает отправку после разрешения бренда (создан/найден либо отменён)."""
        if brand_row is not None:
            self._apply_resolved_brand(brand_row)
        self._finalize_reconcile(api_key, subject_id)

    def _prompt_new_brand(self, deepseek_value, on_done):
        """Окно создания нового бренда: юр. название, Бренд ВБ, Бренд ОЗ, Страна.

        Неблокирующее: строку бренда (dict) или None передаёт в on_done после
        сохранения/отмены. Распознанное DeepSeek значение автоматически попадает
        в search_aliases; если введённые Бренд ВБ / Бренд ОЗ уже существуют —
        алиас дозаписывается к существующей строке вместо создания дубликата.
        """
        dialog = ctk.CTkToplevel(self.window)
        dialog.title("Новый бренд")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        dialog.grab_set()
        _center_window(dialog, 480, 440)

        ctk.CTkLabel(
            dialog,
            text="Бренд не найден в справочнике. Укажите данные нового бренда.",
            anchor="w",
        ).pack(fill="x", padx=20, pady=(16, 0))
        ctk.CTkLabel(
            dialog,
            text=(
                "Распознанное значение (попадёт в поисковые алиасы): "
                f"{deepseek_value or '—'}"
            ),
            anchor="w",
            text_color=("gray40", "gray70"),
        ).pack(fill="x", padx=20, pady=(0, 8))

        entries = {}
        for key, label in (
            ("legal_name", "Юридическое название"),
            ("wb_name", "Бренд ВБ"),
            ("oz_name", "Бренд ОЗ"),
            ("country", "Страна"),
        ):
            ctk.CTkLabel(dialog, text=label, anchor="w").pack(
                fill="x", padx=20, pady=(8, 0)
            )
            entry = ctk.CTkEntry(dialog)
            entry.pack(fill="x", padx=20, pady=(2, 0))
            entries[key] = entry

        def on_save() -> None:
            legal = (entries["legal_name"].get() or "").strip()
            wb = (entries["wb_name"].get() or "").strip()
            oz = (entries["oz_name"].get() or "").strip()
            country = (entries["country"].get() or "").strip()
            if not wb and not oz:
                messagebox.showerror(
                    "Проверьте данные",
                    "Укажите хотя бы «Бренд ВБ» или «Бренд ОЗ».",
                    parent=dialog,
                )
                return
            alias = (deepseek_value or "").strip()
            new_aliases = [alias] if alias else []
            try:
                existing = find_brand_by_names(wb, oz)
                if existing is not None:
                    brand = append_brand_aliases(existing["id"], new_aliases)
                    brand = brand or existing
                else:
                    brand = add_brand(legal, wb, oz, country, new_aliases)
            except sqlite3.Error as exc:
                _logger.warning("Не удалось сохранить бренд: %s", exc)
                messagebox.showerror(
                    "Ошибка",
                    "Не удалось сохранить бренд в базу данных.",
                    parent=dialog,
                )
                return
            dialog.destroy()
            on_done(brand)

        def on_cancel() -> None:
            dialog.destroy()
            on_done(None)

        buttons = ctk.CTkFrame(dialog, corner_radius=0, fg_color="transparent")
        buttons.pack(fill="x", padx=20, pady=(16, 20))
        ctk.CTkButton(buttons, text="Сохранить", width=120, command=on_save).pack(side="left")
        ctk.CTkButton(
            buttons, text="Отмена", width=120, command=on_cancel
        ).pack(side="left", padx=(10, 0))

    # --------------------------- Окно сверки характеристик ---------------------------
    def _show_reconciliation_screen(
        self, category_charcs, found, content_text, raw_pretty,
        promt, category_name, system_prompt, note="", description_text="",
    ) -> None:
        self._clear()
        self._reconcile_rows = []

        art = (self._current or {}).get("art") or "—"
        sup = (self._current or {}).get("sup")
        subject_id = ((self._current or {}).get("category") or {}).get("subject_id")

        # Сопоставляем найденное по имени.
        found_by_name = {}
        for item in found:
            key = str(item.get("name") or "").strip().lower()
            if key:
                found_by_name[key] = item

        # Полный список полей категории + сохранённый выбор.
        selection = DBase.get_category_fields(subject_id) if subject_id is not None else None

        self._reconcile_fields = []
        for f in _load_all_category_fields(subject_id):
            item = found_by_name.get((f["name"] or "").strip().lower())
            value = (item.get("value") or "") if item else (f.get("default") or "")
            source = (item.get("source") or "photo") if item else "photo"
            if f["key"] == "charc:14177452" and description_text:
                value = description_text
                source = "photo"
            default_checked = (
                bool(f["is_required"] or value) if selection is None else (f["key"] in selection)
            )
            f["value"] = value
            f["source"] = source
            f["var"] = tk.BooleanVar(value=default_checked)
            f["type"] = None
            f["value_entry"] = None
            f["type_var"] = None
            self._reconcile_fields.append(f)

        # Бренд здесь НЕ проверяем: поле «Бренд» заполняется ответом DeepSeek как
        # раньше, а поиск по справочнику brands запускается отдельно уже после
        # полного открытия окна (см. self.window.after(...) в конце этого метода).
        self._matched_brand = None

        # Первый раз (нет сохранённого выбора) — показываем все поля.
        self._reconcile_show_all = selection is None

        ctk.CTkLabel(
            self.content,
            text=f"Конструктор полей — {category_name}",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(anchor="w", padx=20, pady=(20, 4))

        note_text = f"   ·   {note}" if note else ""
        ctk.CTkLabel(
            self.content,
            text=(
                f"ART: {art}   ·   SUP: {sup or '—'}   ·   Категория: {category_name}{note_text}\n"
                "Отметьте галочками поля, которые нужны для этой категории. "
                "Выбор сохранится и применится к следующим карточкам."
            ),
            justify="left",
            text_color=("gray40", "gray70"),
        ).pack(anchor="w", padx=20, pady=(0, 8))

        toggle_row = ctk.CTkFrame(self.content, corner_radius=0, fg_color="transparent")
        toggle_row.pack(fill="x", padx=20, pady=(0, 6))
        self._reconcile_show_all_var = tk.BooleanVar(value=self._reconcile_show_all)
        ctk.CTkCheckBox(
            toggle_row,
            text="Показать все поля",
            variable=self._reconcile_show_all_var,
            command=self._on_reconcile_toggle_all,
        ).pack(side="left")
        self._reconcile_exclude_docs_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            toggle_row,
            text="Документы не нужны",
            variable=self._reconcile_exclude_docs_var,
            onvalue=True,
            offvalue=False,
        ).pack(side="left", padx=(16, 0))
        self._reconcile_kiz_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            toggle_row,
            text="КИЗ-маркировка (kizMarked)",
            variable=self._reconcile_kiz_var,
            onvalue=True,
            offvalue=False,
        ).pack(side="left", padx=(16, 0))

        self._reconcile_scroll = ctk.CTkScrollableFrame(self.content, label_text="Поля")
        self._reconcile_scroll.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        self._render_reconcile_fields()
        self._save_draft()

        dev = self._make_section(
            self.content, "Отладка — промт и ответ DeepSeek", collapsed=True
        )
        self._dev_text(dev, "Промт (system_prompt, заполненный)", system_prompt or "")
        self._dev_text(dev, "Ответ DeepSeek (содержимое)", content_text or "")
        self._dev_text(dev, "Ответ DeepSeek (raw JSON)", raw_pretty or "")
        ctk.CTkLabel(
            dev,
            text=f"Запрос и ответ также сохранены в файл: {DEEPSEEK_DEBUG_PATH}",
            anchor="w",
            text_color=("gray40", "gray70"),
        ).pack(fill="x", pady=(0, 6))

        self.status_label = ctk.CTkLabel(
            self.content, text="", anchor="w", text_color=("gray40", "gray70")
        )
        self.status_label.pack(fill="x", padx=20, pady=(0, 8))

        footer = ctk.CTkFrame(self.content, corner_radius=0, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(0, 20))
        ctk.CTkButton(
            footer, text="Далее", width=140, command=self._on_reconcile_next
        ).pack(side="left")
        ctk.CTkButton(
            footer, text="Назад к фото", width=140, command=self._back_to_photos
        ).pack(side="left", padx=(10, 0))

    def _on_reconcile_toggle_all(self) -> None:
        self._reconcile_show_all = bool(self._reconcile_show_all_var.get())
        self._render_reconcile_fields()

    def _flush_reconcile_values(self) -> None:
        """Сохраняет текущие значения полей из виджетов в field-дикты."""
        for f in self._reconcile_fields:
            entry = f.get("value_entry")
            if entry is None:
                continue
            try:
                f["value"] = entry.get()
                f["type"] = f["type_var"].get()
            except tk.TclError:  # noqa: BLE001 — виджет мог быть уничтожен
                pass

    def _render_reconcile_fields(self) -> None:
        self._flush_reconcile_values()
        for f in self._reconcile_fields:
            f["value_entry"] = None
            f["type_var"] = None

        for child in self._reconcile_scroll.winfo_children():
            child.destroy()
        self._reconcile_rows = []

        visible = 0
        for f in self._reconcile_fields:
            if not self._reconcile_show_all and not f["var"].get():
                continue
            self._reconcile_field_row(self._reconcile_scroll, f)
            visible += 1

        if visible == 0:
            ctk.CTkLabel(
                self._reconcile_scroll,
                text="Нет выбранных полей. Включите «Показать все поля».",
                text_color=("gray40", "gray70"),
            ).pack(anchor="w", pady=2)

    def _reconcile_field_row(self, parent, field) -> None:
        """Строка поля конструктора: галочка + имя + значение + тип."""
        row = ctk.CTkFrame(parent, corner_radius=0, fg_color="transparent")
        row.pack(fill="x", pady=2)

        ctk.CTkCheckBox(
            row,
            text="",
            variable=field["var"],
            width=28,
            command=self._on_reconcile_field_toggle,
        ).pack(side="left")

        ctk.CTkLabel(row, text=field["name"], width=150, anchor="w").pack(
            side="left", padx=(2, 0)
        )

        value_entry = ctk.CTkEntry(row)
        value_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        value_entry.insert(0, str(field.get("value") or ""))

        if field.get("source") == "inferred":
            ctk.CTkLabel(
                row,
                text="вывод",
                width=50,
                anchor="w",
                text_color=("gray45", "gray65"),
            ).pack(side="left", padx=(0, 4))

        default_type = "Строка" if field["charcID"] in STRING_CHARC_IDS else "Авто"
        type_var = tk.StringVar(value=field.get("type") or default_type)
        ctk.CTkOptionMenu(
            row,
            values=["Авто", "Строка", "Число", "Список", "Флаг"],
            variable=type_var,
            width=90,
        ).pack(side="right")

        if field["key"] == "charc:14177452":  # поле «Описание»
            ctk.CTkButton(
                row,
                text="✨",
                width=34,
                height=28,
                command=lambda: self._generate_description(),
            ).pack(side="right", padx=(4, 0))

        field["value_entry"] = value_entry
        field["type_var"] = type_var

    def _on_reconcile_field_toggle(self) -> None:
        if not self._reconcile_show_all:
            self._render_reconcile_fields()

    def _on_reconcile_next(self) -> None:
        """Проверяет бренд и формирует задачу создания новой карточки.

        По кнопке «Далее» запрос на WB сразу не уходит: сначала значение поля
        «Бренд» сверяется со справочником brands (при необходимости создаётся
        новый бренд), затем собирается payload с точным именем бренда и открывается
        окно остатка.
        """
        self._flush_reconcile_values()
        self._save_draft()

        subject_id = ((self._current or {}).get("category") or {}).get("subject_id")
        if subject_id is None:
            messagebox.showerror(
                "Нет категории", "Не задана категория (subjectID) карточки.", parent=self.window
            )
            return

        api_key = DBase.get_wb_token("CONTENT")
        if not api_key:
            messagebox.showerror(
                "Нет токена",
                "Заполните токен Wildberries (категория CONTENT) в настройках лаунчера.",
                parent=self.window,
            )
            return

        try:
            self._resolve_brand_for_next(api_key, subject_id)
        except Exception as exc:  # noqa: BLE001 — показываем любую ошибку пользователю
            _logger.exception("Ошибка при проверке бренда")
            messagebox.showerror("Ошибка", str(exc), parent=self.window)

    def _finalize_reconcile(self, api_key, subject_id) -> None:
        """Собирает задачу создания карточки и спрашивает остаток (после проверки бренда)."""
        # Сохраняем выбор полей категории для следующих карточек.
        enabled_keys = [f["key"] for f in self._reconcile_fields if f["var"].get()]
        try:
            DBase.save_category_fields(subject_id, enabled_keys)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("Не удалось сохранить выбор полей: %s", exc)

        # Цена/себестоимость для локальной БД (цена также уходит в запрос).
        price_text = cost_text = ""
        for f in self._reconcile_fields:
            if not f["var"].get():
                continue
            if f["key"] == "field:price":
                price_text = (f.get("value") or "").strip()
            elif f["key"] == "field:cost":
                cost_text = (f.get("value") or "").strip()

        art = (self._current or {}).get("art")
        sup = (self._current or {}).get("sup")
        vendor_code = _compose_vendor_code(art, sup)

        try:
            barcode = _take_barcode(api_key)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                "Ошибка баркода", f"Не удалось получить баркод: {exc}", parent=self.window
            )
            return

        try:
            payload = self._build_create_payload(vendor_code, barcode)
        except ValueError as exc:
            messagebox.showerror("Проверьте данные", str(exc), parent=self.window)
            return

        if not self._reconcile_exclude_docs_var.get():
            messagebox.showinfo(
                "Документы", "Добавьте документы на сайте", parent=self.window
            )

        photos = sorted(self._photos, key=lambda p: 0 if p.get("main") else 1)
        job = {
            "mode": "create",
            "api_key": api_key,
            "vendor_code": vendor_code,
            "barcode": barcode,
            "payload": payload,
            "price_text": price_text,
            "cost_text": cost_text,
            "details": {"art": art, "sup": sup},
            "photos": photos,
            "photo_urls": [],
            "stock": None,
            "oz": False,
            "new_nm_id": None,
            "new_chrt_id": None,
        }
        self._ask_stock_and_enqueue(job)

    def _build_create_payload(self, vendor_code, barcode) -> list:
        """Собирает тело POST /content/v2/cards/upload из полей конструктора."""
        subject_id = ((self._current or {}).get("category") or {}).get("subject_id")
        if subject_id is None:
            raise ValueError("Не задана категория (subjectID).")

        if not vendor_code:
            raise ValueError("Укажите артикул (vendorCode).")

        # Корневые (паспортные) поля: маппинг charcID -> ключ JSON из SEED_CHARCS.
        root = {}
        for f in self._reconcile_fields:
            if not f["var"].get() or f["kind"] != "passport":
                continue
            value = (f.get("value") or "").strip()
            json_key = DBase.PASSPORT_CHARC_MAP.get(f["charcID"])
            if value and json_key and json_key != "skus":
                root[json_key] = value

        title = (root.get("title") or "").strip()
        if not title:
            raise ValueError("Укажите наименование товара.")

        dimensions = {
            "length": _to_int(root.get("length")),
            "width": _to_int(root.get("width")),
            "height": _to_int(root.get("height")),
            "weightBrutto": _to_number(root.get("weightBrutto")),
        }

        type_map = {
            "Авто": "auto",
            "Строка": "str",
            "Число": "num",
            "Список": "list",
            "Флаг": "bool",
        }
        characteristics = []
        price = 0
        tech_size = "0"
        wb_size = ""
        for f in self._reconcile_fields:
            if not f["var"].get():
                continue
            raw = (f.get("value") or "").strip()
            if f["kind"] == "category" and raw:
                charc_id = f["charcID"]
                type_hint = (
                    "str" if charc_id in STRING_CHARC_IDS
                    else type_map.get(f.get("type") or "Авто", "auto")
                )
                characteristics.append(
                    {"id": charc_id, "value": _parse_char_value(raw, type_hint)}
                )
            elif f["key"] == "field:price":
                price = _to_number(raw) or 0
            elif f["key"] == "field:techSize":
                tech_size = raw or "0"
            elif f["key"] == "field:wbSize":
                wb_size = raw

        exclude_documents = bool(
            getattr(self, "_reconcile_exclude_docs_var", None)
            and self._reconcile_exclude_docs_var.get()
        )
        documents = {"items": [], "excludeDocuments": exclude_documents}
        kiz_marked = bool(
            getattr(self, "_reconcile_kiz_var", None) and self._reconcile_kiz_var.get()
        )

        size = {"techSize": tech_size, "price": price, "skus": [barcode]}
        if wb_size:
            size["wbSize"] = wb_size

        variant = {
            "vendorCode": vendor_code,
            "kizMarked": kiz_marked,
            "title": title,
            "description": root.get("description", ""),
            "brand": root.get("brand", ""),
            "dimensions": dimensions,
            "documents": documents,
            "characteristics": characteristics,
            "sizes": [size],
        }
        return [{"subjectID": int(subject_id), "variants": [variant]}]

    def _generate_description(self) -> None:
        """Перегенерирует описание сразу по фото (без промежуточной аннотации)."""
        if getattr(self, "_desc_busy", False):
            return
        self._flush_reconcile_values()

        description_field = None
        for f in self._reconcile_fields:
            if f["key"] == "charc:14177452":
                description_field = f
                break

        analysis = [p for p in self._photos if p.get("analysis")]
        if not analysis:
            messagebox.showwarning(
                "Нет фото для анализа",
                "Отметьте галочкой «Анализ» хотя бы одно фото.",
                parent=self.window,
            )
            return

        promt = DBase.get_promt("description:photo") or DBase.get_promt("description:standard")
        if not promt:
            messagebox.showerror(
                "Нет промпта",
                "Промпт описания не найден в таблице promts.",
                parent=self.window,
            )
            return

        image_paths = [p["src"] for p in analysis]

        self._desc_busy = True
        self._set_status("Генерация описания…")

        def work() -> None:
            text, error = _call_deepseek_description_from_photo(promt, image_paths)
            self.window.after(
                0, lambda: self._on_description_generated(description_field, text, error)
            )

        threading.Thread(target=work, daemon=True).start()

    def _on_description_generated(self, description_field, text, error) -> None:
        self._desc_busy = False
        self._set_status("")
        if error is not None or not text:
            messagebox.showerror(
                "Ошибка",
                error or "Не удалось сгенерировать описание.",
                parent=self.window,
            )
            return
        if description_field is not None:
            description_field["value"] = text
            entry = description_field.get("value_entry")
            if entry is not None:
                try:
                    entry.delete(0, "end")
                    entry.insert(0, text)
                except tk.TclError:  # noqa: BLE001
                    pass
        self._save_draft()
        self._set_status("Описание сгенерировано.")

    def _dev_text(self, parent, title, text) -> None:
        ctk.CTkLabel(
            parent, text=title, anchor="w", text_color=("gray30", "gray80")
        ).pack(fill="x", pady=(6, 2))
        box = ctk.CTkTextbox(parent, height=140, wrap="word")
        box.pack(fill="x", pady=(0, 6))
        box.insert("1.0", text or "")
        box.configure(state="disabled")

    def _back_to_photos(self) -> None:
        self._render_photo_screen()

    # --------------------------- Настройки DeepSeek ---------------------------
    def _open_deepseek_settings(self) -> None:
        dialog = ctk.CTkToplevel(self.window)
        dialog.title("Токен DeepSeek")
        dialog.resizable(False, False)
        dialog.transient(self.window)
        _center_window(dialog, 540, 230)

        ctk.CTkLabel(
            dialog,
            text="Токен DeepSeek (для распознавания фото)",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(anchor="w", padx=20, pady=(20, 4))

        ctk.CTkLabel(
            dialog,
            text="Хранится в .env (ключ DEEPSEEK_TOKEN). Отправка фото — на следующем этапе.",
            text_color=("gray40", "gray70"),
        ).pack(anchor="w", padx=20, pady=(0, 8))

        entry = ctk.CTkEntry(dialog, show="•", placeholder_text="Вставьте токен")
        entry.pack(fill="x", padx=20, pady=(0, 10))
        entry.insert(0, _read_deepseek_token())

        def _save() -> None:
            _save_deepseek_token(entry.get())
            dialog.destroy()
            self._set_status("Токен DeepSeek сохранён.")

        row = ctk.CTkFrame(dialog, corner_radius=0, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkButton(row, text="Сохранить", width=120, command=_save).pack(side="left")
        ctk.CTkButton(row, text="Отмена", width=100, command=dialog.destroy).pack(
            side="left", padx=(10, 0)
        )


# ---------------------------------------------------------------------------
# Вспомогательная функция форматирования строки товара в списке выбора.
# ---------------------------------------------------------------------------
def _format_product(item: dict) -> str:
    platform = item.get("platform") or "?"
    code = item.get("vendorCode") or item.get("ART") or "?"
    sup = item.get("SUP") or ""
    title = item.get("title") or ""
    if len(title) > 60:
        title = title[:57] + "…"
    suffix = "  ·  (Удалено)" if item.get("is_deleted") else ""
    return f"[{platform}] {code}  ·  SUP: {sup or '—'}  ·  {title}{suffix}"


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
            window_ref["win"] = CardsCreatorWindow(root, on_close=done.set)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            _logger.exception("Ошибка создания окна CardsCreator")
            done.set()

    root.after(0, launch)
    done.wait()

    if errors:
        print(f"[CardsCreator] Ошибка интерфейса: {errors[0]}")


def _run_standalone() -> None:
    """Прямой запуск без лаунчера: создаём собственное корневое окно."""
    ctk.set_appearance_mode("System")
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()
    root.withdraw()

    done = threading.Event()
    CardsCreatorWindow(root, on_close=done.set)

    def _watch() -> None:
        if done.is_set():
            root.destroy()
        else:
            root.after(100, _watch)

    root.after(100, _watch)
    root.mainloop()


def run() -> None:
    """Главная точка входа, вызываемая CustomTkinter-лаунчером."""
    print("[CardsCreator] Started execution...")
    _configure_logging()
    _logger.info("Модуль CardsCreator запущен.")

    # Переносим отложенные цены/себестоимость в БД для карточек, которые уже
    # появились в wb_products (например, после синхронизации DBase).
    _flush_pending_costs()

    root = _get_default_root()
    if root is not None:
        _run_with_root(root)
    else:
        _run_standalone()

    print("[CardsCreator] Finished successfully.")


if __name__ == "__main__":
    # Позволяет запускать модуль и напрямую: python apps/CardsCreator.py
    run()








