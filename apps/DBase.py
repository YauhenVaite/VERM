"""Модуль DBase — единая точка управления схемой базы данных проекта.

Это ЕДИНСТВЕННЫЙ модуль во всём проекте, которому разрешено создавать,
изменять или удалять таблицы и колонки (структуру) SQLite-базы данных.
Все остальные модули могут только читать, добавлять или обновлять строки.

При запуске модуль выполняет монолитный алгоритм первой инициализации:
  1) получает токен Wildberries для категории CONTENT через get_wb_token() из .env
     (при отсутствии специализированного токена используется мастер-токен);
  2) создаёт/обновляет схему БД;
  3) выгружает все карточки товаров (пагинация курсором) и заполняет
     wb_products (корневые параметры) и wb_product_values (динамические
     характеристики);
  4) опрашивает справочник характеристик Wildberries по уникальным
     категориям и заполняет таблицу wb_charcs шаблонами полей.

Схема построена под реальный формат ответов Wildberries API v2:
  * POST /content/v2/get/cards/list — список карточек (пагинация курсором);
  * GET  /content/v2/object/charcs/{subjectId} — метаданные характеристик.

База данных хранится в data/inventory.db.
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time

import requests

# ---------------------------------------------------------------------------
# Пути проекта
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "inventory.db")
ENV_PATH = os.path.join(BASE_DIR, ".env")

# ---------------------------------------------------------------------------
# Секреты (.env). Файл создаётся автоматически, если его нет.
# ---------------------------------------------------------------------------
# Заголовок-комментарий, который записывается в начало файла .env.
ENV_HEADER = (
    "# Секретные ключи Wildberries и Ozon.\n"
    "# Wildberries: 1 мастер-токен (резервный для всего) и 7 специализированных.\n"
    "# Если специализированный токен WB пуст, система автоматически использует\n"
    "# WB_MASTER_TOKEN. Для обратной совместимости старый ключ WB_API_KEY также\n"
    "# воспринимается как мастер-токен.\n"
    "# Ozon: пока используется только OZ_MASTER_TOKEN.\n"
    "# Прочие параметры: WB_WAREHOUSE_ID / OZON_WAREHOUSE_ID (массивы ID складов),\n"
    "# OZON_CLIENT_ID (Client-Id Ozon).\n"
)

# Соответствие «категория метода → ключ в .env».
WB_TOKEN_KEYS = {
    "MASTER": "WB_MASTER_TOKEN",
    "CONTENT": "WB_TOKEN_CONTENT",
    "PRICES": "WB_TOKEN_PRICES",
    "STOCKS": "WB_TOKEN_STOCKS",
    "MARKETPLACE": "WB_TOKEN_MARKETPLACE",
    "STATISTICS": "WB_TOKEN_STATISTICS",
    "PROMOTION": "WB_TOKEN_PROMOTION",
    "FEEDBACKS": "WB_TOKEN_FEEDBACKS",
}

# Порядок отображения полей в настройках: (ключ .env, русская подпись).
WB_TOKEN_LABELS = [
    ("WB_MASTER_TOKEN", "Главный (Мастер)"),
    ("WB_TOKEN_CONTENT", "Контент"),
    ("WB_TOKEN_PRICES", "Цены"),
    ("WB_TOKEN_STOCKS", "Склады/Остатки"),
    ("WB_TOKEN_MARKETPLACE", "Маркетплейс (FBS)"),
    ("WB_TOKEN_STATISTICS", "Статистика"),
    ("WB_TOKEN_PROMOTION", "Реклама"),
    ("WB_TOKEN_FEEDBACKS", "Отзывы"),
]

# Соответствие «категория метода Ozon → ключ в .env». Пока только мастер-токен.
OZ_TOKEN_KEYS = {
    "MASTER": "OZ_MASTER_TOKEN",
}

# Порядок отображения полей Ozon в настройках: (ключ .env, русская подпись).
OZ_TOKEN_LABELS = [
    ("OZ_MASTER_TOKEN", "Ozon (Мастер)"),
]

# Ключи мастер-токенов, для которых в шаблоне .env подставляется заглушка.
_MASTER_TOKEN_KEYS = {"WB_MASTER_TOKEN", "OZ_MASTER_TOKEN"}

# Старый ключ, оставшийся для обратной совместимости (трактуется как мастер).
LEGACY_MASTER_KEY = "WB_API_KEY"

# Прочие параметры (не токены), хранящиеся в .env.
# WB_WAREHOUSE_ID и OZON_WAREHOUSE_ID хранят массивы ID складов (JSON-строкой).
ENV_PARAM_LABELS = [
    ("WB_WAREHOUSE_ID", "ID складов Wildberries (массив)"),
    ("OZON_WAREHOUSE_ID", "ID складов Ozon (массив)"),
    ("OZON_CLIENT_ID", "Client-Id Ozon"),
]


def _build_env_template() -> str:
    """Собирает аккуратный шаблон .env из токенов и параметров API."""
    parts = [ENV_HEADER]
    for key, _label in WB_TOKEN_LABELS + OZ_TOKEN_LABELS:
        placeholder = "ВАШ_ТОКЕН_ЗДЕСЬ" if key in _MASTER_TOKEN_KEYS else ""
        parts.append(f'{key}="{placeholder}"\n')
    for key, _label in ENV_PARAM_LABELS:
        parts.append(f'{key}=""\n')
    return "".join(parts)


# Шаблон .env, создаваемый при первой инициализации.
ENV_TEMPLATE = _build_env_template()

# ---------------------------------------------------------------------------
# Извлечение ART и SUP из vendorCode.
#
# ART — ведущая цифровая часть vendorCode (артикул).
# SUP — буквенный остаток поставщика после артикула (может быть NULL).
# ---------------------------------------------------------------------------
_ART_RE = re.compile(r"^(\d+)")
_SUP_LETTERS_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")


def extract_art_sup(vendor_code):
    """Возвращает кортеж (ART, SUP), извлечённый из строки vendorCode.

    ART — ведущие цифры артикула, SUP — буквенный остаток поставщика.
    Если vendorCode пуст или не начинается с цифр, возвращает (None, None).
    """
    if vendor_code is None:
        return None, None

    code = str(vendor_code).strip()
    art_match = _ART_RE.match(code)
    if not art_match:
        return None, None

    art = art_match.group(1)
    tail = code[art_match.end():]
    sup_match = _SUP_LETTERS_RE.search(tail)
    sup = sup_match.group(0) if sup_match else None
    return art, sup


# ---------------------------------------------------------------------------
# Справочник характеристик wb_charcs: первичный маппинг (Seed).
# Каждый кортеж: (charcID, name_ru, json_mapping_key). existNamedField = 1.
# ---------------------------------------------------------------------------
SEED_CHARCS = [
    (14177453, "Баркоды", "skus"),
    (14177452, "Описание", "description"),
    (15000000, "Наименование", "title"),
    (14177446, "Бренд", "brand"),
    (90745, "Ширина упаковки", "width"),
    (90846, "Высота упаковки", "height"),
    (90849, "Длина упаковки", "length"),
    (88952, "Вес с упаковкой", "weightBrutto"),
]

# ---------------------------------------------------------------------------
# Паспортные (корневые) характеристики карточки: их значения пишутся в
# wb_products, а не в wb_product_values. Маппинг строится из SEED_CHARCS.
# ---------------------------------------------------------------------------
PASSPORT_CHARC_MAP = {charc_id: json_key for charc_id, _, json_key in SEED_CHARCS}
PASSPORT_CHARC_IDS = frozenset(PASSPORT_CHARC_MAP)

# ---------------------------------------------------------------------------
# Колонки таблицы wb_products (порядок в словаре = порядок колонок в таблице).
# Первые колонки таблицы всегда: id, ART, SUP, затем колонки из этого словаря
# в указанном порядке. Словарь используется и для миграции: если в нём появится
# новая колонка или изменится порядок, модуль пересоздаст таблицу.
# ---------------------------------------------------------------------------
PRODUCT_ROOT_COLUMNS = {
    "title": "TEXT",
    "brand": "TEXT",
    "price": "REAL",
    "cost": "REAL",
    "width": "REAL",
    "length": "REAL",
    "height": "REAL",
    "weightBrutto": "REAL",
    "vendorCode": "TEXT",
    "description": "TEXT",
    "nmID": "INTEGER",
    "imtID": "INTEGER",
    "nmUUID": "TEXT",
    "subjectID": "INTEGER",
    "subjectName": "TEXT",
    "needKiz": "INTEGER",
    "kizMarked": "INTEGER",
    "chrtID": "TEXT",
    "skus": "TEXT",
    "techSize": "TEXT",
    "wbSize": "TEXT",
    "createdAt": "TEXT",
    "updatedAt": "TEXT",
}

# Колонки, которые не приходят в списке карточек WB (cards/list) и заполняются
# другими модулями (например, ценами). При выгрузке карточек DBase их не трогает,
# чтобы не затирать уже загруженные значения.
NON_CARD_COLUMNS = {"price", "cost"}

# Колонки, заполняемые из ответа cards/list (все, кроме price/cost).
CARD_ROOT_COLUMNS = [col for col in PRODUCT_ROOT_COLUMNS if col not in NON_CARD_COLUMNS]

# Колонки wb_products, выведенные из схемы и подлежащие удалению из уже
# существующих баз (обычная миграция колонки только добавляет, но не удаляет).
OBSOLETE_PRODUCT_COLUMNS = frozenset({"Photos"})

# Ключи верхнего уровня карточки, значения которых уже раскладываются по
# корневым колонкам или EAV-характеристикам отдельной логикой. Они не попадают
# в универсальную автозапись «неизвестных» полей.
HANDLED_TOP_KEYS = frozenset({"sizes", "dimensions", "characteristics"})

# Допустимый формат имени SQL-колонки. Используется при автосоздании колонок
# под новые поля API — защищает от инъекций и невалидных идентификаторов.
SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ---------------------------------------------------------------------------
# Интеграция с Wildberries Content API v2.
# ---------------------------------------------------------------------------
WB_API_BASE_URL = "https://content-api.wildberries.ru"
CARDS_PAGE_SIZE = 100
CARDS_LIST_URL = f"{WB_API_BASE_URL}/content/v2/get/cards/list"
CHARCS_URL_TEMPLATE = f"{WB_API_BASE_URL}/content/v2/object/charcs/{{subject_id}}"

# Путь к сохранённому курсору выгрузки карточек (для инкрементального обновления).
CARDS_CURSOR_PATH = os.path.join(DATA_DIR, "wb_cards_cursor.json")

# Значения токена, которые считаются заглушкой (невалидными).
PLACEHOLDER_API_KEYS = {"", "ВАШ_ТОКЕН_ЗДЕСЬ", "YOUR_TOKEN_HERE", "CHANGE_ME", "changeme"}

# Дополнительные колонки wb_charcs: связь «характеристика ↔ категории».
CHARC_EXTRA_COLUMNS = {
    "subj_ID": "TEXT",
    "subj_NM": "TEXT",
}

# Таблицы с составным ключом UNIQUE(ART, SUP).
_ART_SUP_TABLES = ("status", "wb_products")

# Таблицы значений с составным ключом UNIQUE(ART, SUP, charcID).
_EAV_TABLES = ("wb_product_values",)

_logger = logging.getLogger("DBase")


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------
def _configure_logging() -> None:
    """Настраивает логгер модуля, чтобы сообщения были видны в консоли."""
    if not _logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        )
        _logger.addHandler(handler)
        _logger.setLevel(logging.INFO)
        _logger.propagate = False


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------
def _ensure_env_file() -> None:
    """Создаёт .env в корне проекта, если его ещё нет."""
    if os.path.exists(ENV_PATH):
        print("[DBase] Файл .env уже существует — пропускаю создание.")
        _logger.info("Файл .env уже существует: %s", ENV_PATH)
        return

    with open(ENV_PATH, "w", encoding="utf-8") as file:
        file.write(ENV_TEMPLATE)
    print(f"[DBase] Файл .env создан: {ENV_PATH}")
    _logger.info("Файл .env создан: %s", ENV_PATH)


# ---------------------------------------------------------------------------
# Схема
# ---------------------------------------------------------------------------
def _products_columns_sql() -> str:
    """SQL-фрагмент корневых колонок таблицы wb_products."""
    return ", ".join(
        f"{name} {col_type}" for name, col_type in PRODUCT_ROOT_COLUMNS.items()
    )


def _create_tables(connection: sqlite3.Connection) -> None:
    """Создаёт все таблицы схемы (CREATE TABLE IF NOT EXISTS)."""
    cursor = connection.cursor()

    # Бренды: юр. название, названия на WB/Ozon, альтернативные имена для поиска.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS brands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            legal_name TEXT,
            wb_name TEXT,
            oz_name TEXT,
            search_aliases TEXT,
            country TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ART TEXT NOT NULL,
            SUP TEXT,
            UNIQUE(ART, SUP)
        )
        """
    )

    # Справочник характеристик из метода /content/v2/object/charcs/{subjectId}.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS wb_charcs (
            charcID INTEGER PRIMARY KEY,
            name_ru TEXT,
            is_required INTEGER,
            existNamedField INTEGER DEFAULT 0,
            json_mapping_key TEXT DEFAULT NULL,
            subj_ID TEXT DEFAULT NULL,
            subj_NM TEXT DEFAULT NULL
        )
        """
    )

    # Паспорт товара: общие корневые параметры карточки WB.
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS wb_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ART TEXT NOT NULL,
            SUP TEXT,
            {_products_columns_sql()},
            UNIQUE(ART, SUP)
        )
        """
    )

    # Динамические значения (EAV-паттерн): строки для числовых характеристик.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS wb_product_values (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ART TEXT NOT NULL,
            SUP TEXT,
            charcID INTEGER,
            field_name TEXT,
            value TEXT,
            FOREIGN KEY(charcID) REFERENCES wb_charcs(charcID),
            UNIQUE(ART, SUP, charcID)
        )
        """
    )

    connection.commit()


def _create_unique_indexes(connection: sqlite3.Connection) -> None:
    """Добавляет частичные уникальные индексы для корректной работы NULL SUP.

    В SQLite ограничение UNIQUE считает NULL отличным от любого другого
    значения (включая другое NULL), поэтому UNIQUE(ART, SUP) само по себе
    не блокирует дубликаты ART при SUP IS NULL. Частичный индекс закрывает
    эту лазейку: в рамках одного ART допускается не более одной строки с
    SUP IS NULL.
    """
    cursor = connection.cursor()

    for table in _ART_SUP_TABLES:
        cursor.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_art_null_sup "
            f"ON {table} (ART) WHERE SUP IS NULL"
        )

    for table in _EAV_TABLES:
        cursor.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_art_charcid_null_sup "
            f"ON {table} (ART, charcID) WHERE SUP IS NULL"
        )

    connection.commit()


def _migrate_product_columns(connection: sqlite3.Connection) -> list:
    """Добавляет в wb_products недостающие эталонные колонки (ALTER TABLE).

    Используется ALTER TABLE ADD COLUMN, а не пересоздание таблицы, чтобы:
      * сохранить все данные и не трогать колонки, которые были автоматически
        добавлены под новые поля API (их порядок и значения остаются на месте);
      * корректно работать с уже существующими базами.

    Возвращает список добавленных колонок.
    """
    cursor = connection.cursor()
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(wb_products)")}

    added = []
    for name, col_type in PRODUCT_ROOT_COLUMNS.items():
        if name not in existing:
            cursor.execute(f"ALTER TABLE wb_products ADD COLUMN {name} {col_type}")
            added.append(name)

    if added:
        connection.commit()

    return added


def _migrate_charcs_columns(connection: sqlite3.Connection) -> list:
    """Добавляет в wb_charcs колонки subj_ID/subj_NM, если их ещё нет."""
    cursor = connection.cursor()
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(wb_charcs)")}

    added = []
    for name, col_type in CHARC_EXTRA_COLUMNS.items():
        if name not in existing:
            cursor.execute(f"ALTER TABLE wb_charcs ADD COLUMN {name} {col_type}")
            added.append(name)

    if added:
        connection.commit()

    return added


def _migrate_product_values_columns(connection: sqlite3.Connection) -> list:
    """Добавляет в wb_product_values колонку field_name, если её ещё нет.

    field_name используется для хранения «универсальных» вложенных полей
    (например, documents), которые не являются характеристиками WB.
    """
    cursor = connection.cursor()
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(wb_product_values)")}

    added = []
    if "field_name" not in existing:
        cursor.execute("ALTER TABLE wb_product_values ADD COLUMN field_name TEXT")
        added.append("field_name")
        connection.commit()

    return added


def _drop_obsolete_product_columns(connection: sqlite3.Connection) -> list:
    """Удаляет устаревшие колонки wb_products (например, старую `Photos`).

    Обычная миграция только ДОБАВЛЯЕТ колонки, чтобы не потерять данные.
    Здесь точечно удаляются колонки, выведенные из схемы (перечислены в
    OBSOLETE_PRODUCT_COLUMNS). Колонки, автоматически добавленные под новые
    поля API (например, `video`), не трогаются.
    """
    cursor = connection.cursor()
    existing = {row[1] for row in cursor.execute("PRAGMA table_info(wb_products)")}

    dropped = []
    for name in OBSOLETE_PRODUCT_COLUMNS:
        if name in existing:
            cursor.execute(f"ALTER TABLE wb_products DROP COLUMN {name}")
            dropped.append(name)

    if dropped:
        connection.commit()

    return dropped


def _seed_charcs(connection: sqlite3.Connection) -> list:
    """Наполняет wb_charcs базовым маппингом, если нужные ID отсутствуют.

    Возвращает список добавленных записей в виде (charcID, name_ru, json_key).
    """
    cursor = connection.cursor()
    seeded = []

    for charc_id, name_ru, json_key in SEED_CHARCS:
        row = cursor.execute(
            "SELECT 1 FROM wb_charcs WHERE charcID = ?", (charc_id,)
        ).fetchone()
        if row is None:
            cursor.execute(
                """
                INSERT INTO wb_charcs (charcID, name_ru, existNamedField, json_mapping_key)
                VALUES (?, ?, 1, ?)
                """,
                (charc_id, name_ru, json_key),
            )
            seeded.append((charc_id, name_ru, json_key))

    connection.commit()
    return seeded


# ---------------------------------------------------------------------------
# Работа с .env
# ---------------------------------------------------------------------------
def read_env_value(key: str):
    """Читает значение переменной из .env (простые строки вида KEY="value").

    Возвращает None, если файл отсутствует или переменная не найдена.
    """
    if not os.path.exists(ENV_PATH):
        return None
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as file:
            for raw_line in file:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                if name.strip() != key:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                return value
    except OSError as exc:
        _logger.warning("Не удалось прочитать .env: %s", exc)
    return None


def _is_valid_token(token) -> bool:
    """True, если токен заполнен и не является заглушкой."""
    return bool(token) and token.strip() not in PLACEHOLDER_API_KEYS


def _load_master_token():
    """Возвращает мастер-токен (WB_MASTER_TOKEN), учитывая старый WB_API_KEY."""
    for key in (WB_TOKEN_KEYS["MASTER"], LEGACY_MASTER_KEY):
        token = read_env_value(key)
        if _is_valid_token(token):
            return token.strip()
    return None


def get_wb_token(category: str):
    """Возвращает токен Wildberries для категории методов.

    Логика отката (fallback):
      1. Если заполнен специализированный токен категории (например,
         WB_TOKEN_CONTENT) — возвращает его.
      2. Иначе возвращает WB_MASTER_TOKEN (а при его отсутствии — старый
         WB_API_KEY для обратной совместимости).
    Возвращает None, если ни одного валидного токена нет.
    """
    key = WB_TOKEN_KEYS.get((category or "").upper())
    if key and key != WB_TOKEN_KEYS["MASTER"]:
        token = read_env_value(key)
        if _is_valid_token(token):
            return token.strip()
    return _load_master_token()


def get_oz_token(category: str = "MASTER"):
    """Возвращает токен Ozon для категории методов (пока только MASTER).

    Возвращает None, если токен не заполнен или является заглушкой.
    """
    key = OZ_TOKEN_KEYS.get((category or "").upper())
    if not key:
        return None
    token = read_env_value(key)
    if _is_valid_token(token):
        return token.strip()
    return None


def _parse_env_array(value):
    """Разбирает значение .env как список ID складов.

    Поддерживает JSON-массив (например "[37356, 12345]"), одиночное число
    и перечисление через запятую. Пустое значение возвращает [].
    """
    if not value:
        return []
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, (int, float)):
        return [parsed]
    # Фолбэк: перечисление через запятую, например "37356, 12345".
    result = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            result.append(part)
    return result


def get_wb_warehouse_ids():
    """Возвращает список ID складов Wildberries из .env (WB_WAREHOUSE_ID)."""
    return _parse_env_array(read_env_value("WB_WAREHOUSE_ID"))


def get_oz_warehouse_ids():
    """Возвращает список ID складов Ozon из .env (OZON_WAREHOUSE_ID)."""
    return _parse_env_array(read_env_value("OZON_WAREHOUSE_ID"))


def get_oz_client_id():
    """Возвращает Client-Id Ozon из .env (OZON_CLIENT_ID) или None."""
    value = read_env_value("OZON_CLIENT_ID")
    if value is None:
        return None
    value = value.strip()
    return value or None


def write_env_file(token_values: dict) -> None:
    """Перезаписывает .env аккуратным шаблоном из токенов и параметров API.

    token_values — словарь {ключ .env: значение}. Отсутствующие ключи
    записываются пустыми строками.
    """
    os.makedirs(os.path.dirname(ENV_PATH), exist_ok=True)
    with open(ENV_PATH, "w", encoding="utf-8") as file:
        file.write(ENV_HEADER)
        for key, _label in WB_TOKEN_LABELS + OZ_TOKEN_LABELS + ENV_PARAM_LABELS:
            value = str(token_values.get(key) or "").strip()
            # Экранируем символы, способные сломать формат .env.
            value = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            file.write(f'{key}="{value}"\n')


# ---------------------------------------------------------------------------
# Rate Limiter (Token Bucket)
# ---------------------------------------------------------------------------
class WBRateLimiter:
    """Многопоточный Rate Limiter на базе алгоритма Token Bucket.

    Для каждой категории API Wildberries держится независимая «корзина»
    токенов. Перед каждым HTTP-запросом поток вызывает wait_for_token();
    если свободного токена нет, поток блокируется (time.sleep) до момента,
    когда запрос станет безопасным. Это защищает от HTTP 429.
    """

    # Лимиты по умолчанию согласно документации Wildberries.
    DEFAULT_LIMITS = {
        # 100 запросов/мин (~600 мс между запросами), burst = 5.
        "CONTENT": {"max_tokens": 5, "refill_rate": 100 / 60.0},
        # 3 запроса/сек (~333 мс), burst = 3.
        "PRICES": {"max_tokens": 3, "refill_rate": 3.0},
        # 5 запросов/сек (~200 мс), burst = 5.
        "STOCKS": {"max_tokens": 5, "refill_rate": 5.0},
        # Базовый безопасный лимит для остальных категорий.
        "MARKETPLACE": {"max_tokens": 2, "refill_rate": 2.0},
        "STATISTICS": {"max_tokens": 2, "refill_rate": 2.0},
        "PROMOTION": {"max_tokens": 2, "refill_rate": 2.0},
        "FEEDBACKS": {"max_tokens": 2, "refill_rate": 2.0},
    }

    _DEFAULT_FALLBACK = {"max_tokens": 2, "refill_rate": 2.0}

    def __init__(self, limits=None) -> None:
        merged = dict(self.DEFAULT_LIMITS)
        if limits:
            merged.update(limits)
        self._limits = {
            category: {
                "max_tokens": float(params["max_tokens"]),
                "refill_rate": float(params["refill_rate"]),
            }
            for category, params in merged.items()
        }
        # Корзины создаются лениво, при первом обращении к категории.
        self._buckets = {}
        self._buckets_lock = threading.Lock()

    def _bucket_for(self, category: str) -> dict:
        key = (category or "").upper()
        with self._buckets_lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                params = self._limits.get(key, self._DEFAULT_FALLBACK)
                bucket = {
                    "lock": threading.Lock(),
                    "max_tokens": params["max_tokens"],
                    "refill_rate": params["refill_rate"],
                    "tokens": params["max_tokens"],  # стартуем с полной корзиной
                    "last_refill": time.monotonic(),
                }
                self._buckets[key] = bucket
            return bucket

    @staticmethod
    def _refill(bucket: dict) -> None:
        now = time.monotonic()
        elapsed = now - bucket["last_refill"]
        if elapsed > 0:
            bucket["tokens"] = min(
                bucket["max_tokens"],
                bucket["tokens"] + elapsed * bucket["refill_rate"],
            )
            bucket["last_refill"] = now

    def wait_for_token(self, category: str) -> None:
        """Блокирует поток до появления свободного токена в корзине категории."""
        bucket = self._bucket_for(category)
        while True:
            with bucket["lock"]:
                self._refill(bucket)
                if bucket["tokens"] >= 1.0:
                    bucket["tokens"] -= 1.0
                    return
                wait_time = (1.0 - bucket["tokens"]) / bucket["refill_rate"]
            time.sleep(max(wait_time, 0.0))


# Глобальный экземпляр лимитера — общий для всех модулей приложения.
LIMITER = WBRateLimiter()


def _load_api_key():
    """Возвращает валидный токен категории CONTENT (или мастер-токен)."""
    token = get_wb_token("CONTENT")
    if not _is_valid_token(token):
        return None
    return token.strip()


# ---------------------------------------------------------------------------
# Вспомогательные функции обработки значений
# ---------------------------------------------------------------------------
def _first_or_join(value):
    """Список из одного элемента сводит к скаляру, иначе склеивает через запятую."""
    if isinstance(value, (list, tuple)):
        if len(value) == 1:
            return value[0]
        return ", ".join(str(item) for item in value)
    return value


def _serialize(value):
    """Сериализует значение характеристики в строку для wb_product_values."""
    value = _first_or_join(value)
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _append_list(existing, new_value):
    """Дописывает new_value к строке existing через запятую без дублей."""
    parts = []
    if existing:
        parts = [part.strip() for part in str(existing).split(",") if part.strip()]
    new_value = str(new_value).strip()
    if new_value and new_value not in parts:
        parts.append(new_value)
    return ", ".join(parts)


def _extract_sizes(sizes):
    """Извлекает значения из массива sizes карточки WB.

    Каждый объект размера содержит chrtID, techSize, wbSize и список skus.
    Для карточек с несколькими размерами (например, одежда) значения
    склеиваются через запятую. Возвращает словарь с непустыми ключами
    {chrtID, skus, techSize, wbSize}.
    """
    if not isinstance(sizes, (list, tuple)) or not sizes:
        return {}

    chrt_ids = []
    skus = []
    tech_sizes = []
    wb_sizes = []

    for size in sizes:
        if not isinstance(size, dict):
            continue
        chrt_id = size.get("chrtID")
        if chrt_id is not None:
            chrt_ids.append(str(chrt_id))
        for sku in size.get("skus") or []:
            sku = str(sku).strip()
            if sku and sku not in skus:
                skus.append(sku)
        tech_size = size.get("techSize")
        if tech_size not in (None, ""):
            tech_sizes.append(str(tech_size))
        wb_size = size.get("wbSize")
        if wb_size not in (None, ""):
            wb_sizes.append(str(wb_size))

    result = {}
    if chrt_ids:
        result["chrtID"] = ", ".join(chrt_ids)
    if skus:
        result["skus"] = ", ".join(skus)
    if tech_sizes:
        result["techSize"] = ", ".join(tech_sizes)
    if wb_sizes:
        result["wbSize"] = ", ".join(wb_sizes)
    return result


# ---------------------------------------------------------------------------
# Курсор выгрузки карточек (для инкрементального обновления)
# ---------------------------------------------------------------------------
def _load_cursor():
    """Загружает сохранённый cursor прошлой выгрузки, если он есть.

    Возвращает готовый начальный cursor для запроса (updatedAt, nmID, limit)
    или None, если курсор не сохранён (тогда будет полная выгрузка).
    """
    try:
        if not os.path.exists(CARDS_CURSOR_PATH):
            return None
        with open(CARDS_CURSOR_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, dict) and (data.get("updatedAt") or data.get("nmID")):
            return {
                "updatedAt": data.get("updatedAt"),
                "nmID": data.get("nmID"),
                "limit": CARDS_PAGE_SIZE,
            }
    except (OSError, ValueError) as exc:
        _logger.warning("Не удалось прочитать сохранённый cursor: %s", exc)
    return None


def _save_cursor(cursor_meta):
    """Сохраняет cursor последнего ответа для следующей инкрементальной выгрузки."""
    if not cursor_meta:
        return
    try:
        with open(CARDS_CURSOR_PATH, "w", encoding="utf-8") as file:
            json.dump(
                {
                    "updatedAt": cursor_meta.get("updatedAt"),
                    "nmID": cursor_meta.get("nmID"),
                },
                file,
                ensure_ascii=False,
                indent=2,
            )
    except OSError as exc:
        _logger.warning("Не удалось сохранить cursor: %s", exc)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _http_request(session, method, url, *, json_body=None, retries=3, retry_delay=5):
    """Синхронный HTTP-запрос с ретраями при 429 и сетевых ошибках."""
    for attempt in range(1, retries + 1):
        # Пропускаем запрос через Rate Limiter перед каждым вызовом API.
        LIMITER.wait_for_token("CONTENT")
        try:
            if method == "POST":
                response = session.post(url, json=json_body, timeout=60)
            else:
                response = session.get(url, timeout=60)
        except requests.RequestException as exc:
            if attempt >= retries:
                raise
            _logger.warning(
                "Сетевая ошибка при запросе %s: %s. Ретрай %d/%d через %d с.",
                method, exc, attempt, retries, retry_delay,
            )
            time.sleep(retry_delay)
            continue

        if response.status_code == 429:
            if attempt >= retries:
                response.raise_for_status()
            _logger.warning(
                "HTTP 429 (слишком много запросов) для %s. Ретрай %d/%d через %d с.",
                url, attempt, retries, retry_delay,
            )
            time.sleep(retry_delay)
            continue

        if response.status_code >= 400:
            _logger.error("HTTP %s для %s: %s", response.status_code, url, response.text[:300])
            response.raise_for_status()

        return response

    raise RuntimeError(f"Не удалось выполнить {method} {url} после {retries} попыток")


def _parse_json(response):
    """Безопасно разбирает JSON-ответ; при ошибке возвращает пустой словарь."""
    try:
        return response.json()
    except ValueError as exc:
        _logger.error("Некорректный JSON в ответе: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Запись данных
# ---------------------------------------------------------------------------
def _upsert_product(db_cursor, art, sup, root_values):
    """Вставляет или обновляет корневые параметры карточки в wb_products.

    Помимо известных колонок CARD_ROOT_COLUMNS учитывает любые новые скалярные
    поля из root_values: под них автоматически создаются колонки.
    """
    columns = list(CARD_ROOT_COLUMNS)
    extra = [
        name for name in root_values
        if name not in columns and SQL_IDENTIFIER_RE.match(name)
    ]
    if extra:
        _ensure_product_columns(db_cursor, {name: root_values[name] for name in extra})
        columns.extend(extra)

    if sup is None:
        existing = db_cursor.execute(
            "SELECT id FROM wb_products WHERE ART = ? AND SUP IS NULL", (art,)
        ).fetchone()
    else:
        existing = db_cursor.execute(
            "SELECT id FROM wb_products WHERE ART = ? AND SUP = ?", (art, sup)
        ).fetchone()

    if existing is None:
        cols = ["ART", "SUP"] + columns
        placeholders = ", ".join(["?"] * len(cols))
        db_cursor.execute(
            f"INSERT INTO wb_products ({', '.join(cols)}) VALUES ({placeholders})",
            [art, sup] + [root_values.get(col) for col in columns],
        )
        return "insert"

    assignments = ", ".join(f"{col} = ?" for col in columns)
    db_cursor.execute(
        f"UPDATE wb_products SET {assignments} WHERE id = ?",
        [root_values.get(col) for col in columns] + [existing[0]],
    )
    return "update"


def _delete_product_values(db_cursor, art, sup):
    """Удаляет все динамические характеристики карточки (по связке ART+SUP).

    Таблица wb_product_values не содержит колонку nmID — она ключуется связкой
    ART + SUP + charcID, поэтому очистка «хвостов» выполняется по ART + SUP.
    """
    if sup is None:
        db_cursor.execute(
            "DELETE FROM wb_product_values WHERE ART = ? AND SUP IS NULL", (art,)
        )
    else:
        db_cursor.execute(
            "DELETE FROM wb_product_values WHERE ART = ? AND SUP = ?", (art, sup)
        )


def _insert_product_value(db_cursor, art, sup, charc_id, field_name, value):
    """Вставляет строку в wb_product_values (характеристика или универсальное поле).

    Для характеристики: charc_id задан, field_name = None.
    Для универсального вложенного поля (documents и т.п.): charc_id = None,
    field_name задан, а value — сериализованный JSON.
    """
    db_cursor.execute(
        "INSERT INTO wb_product_values (ART, SUP, charcID, field_name, value) "
        "VALUES (?, ?, ?, ?, ?)",
        (art, sup, charc_id, field_name, value),
    )


def _json_dumps(value) -> str:
    """Сериализует произвольную структуру в JSON-строку (UTF-8, без экранирования)."""
    return json.dumps(value, ensure_ascii=False)


def _infer_sql_type(value) -> str:
    """Подбирает SQL-тип колонки под скалярное значение."""
    if isinstance(value, bool):
        return "INTEGER"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    return "TEXT"


def _existing_product_columns(db_cursor) -> set:
    """Возвращает множество имён колонок таблицы wb_products."""
    return {row[1] for row in db_cursor.execute("PRAGMA table_info(wb_products)")}


def _ensure_product_columns(db_cursor, values: dict) -> list:
    """Создаёт в wb_products недостающие колонки под новые скалярные поля.

    values — {имя_колонки: значение}. Имя проверяется по SQL_IDENTIFIER_RE,
    чтобы не сломать SQL и не допустить инъекции. Возвращает имена созданных
    колонок.
    """
    existing = _existing_product_columns(db_cursor)
    created = []
    for name, value in values.items():
        if name in existing or not SQL_IDENTIFIER_RE.match(name):
            continue
        col_type = _infer_sql_type(value)
        db_cursor.execute(f"ALTER TABLE wb_products ADD COLUMN {name} {col_type}")
        existing.add(name)
        created.append(name)
    return created


def _fetch_cards_page(session, cursor_state):
    """Загружает одну страницу карточек с безопасным fallback по курсору.

    Сначала запрашивает карточки по связке updatedAt + nmID. Если запрос
    возвращает ошибку (сетевая или HTTP 4xx/5xx), перехватывает её, стирает
    nmID и повторяет запрос только по updatedAt — Wildberries в ряде случаев
    отклоняет курсор с устаревшим nmID.

    Возвращает кортеж (data, cursor_state): распарсенный JSON-ответ и курсор,
    который фактически использовался в запросе (после возможного удаления nmID).
    """
    def _payload(cursor):
        return {
            "settings": {
                "sort": {"ascending": True},
                "filter": {"withPhoto": -1},
                "cursor": cursor,
            }
        }

    try:
        response = _http_request(
            session, "POST", CARDS_LIST_URL, json_body=_payload(cursor_state)
        )
    except requests.RequestException as exc:
        if not cursor_state or cursor_state.get("nmID") is None:
            raise
        _logger.warning(
            "Запрос карточек по updatedAt+nmID не удался (%s). "
            "Стираю nmID и повторяю только по updatedAt.",
            exc,
        )
        cursor_state = {
            key: value
            for key, value in cursor_state.items()
            if key != "nmID"
        }
        response = _http_request(
            session, "POST", CARDS_LIST_URL, json_body=_payload(cursor_state)
        )

    return _parse_json(response), cursor_state


def _upsert_charc(db_cursor, charc, subject_id, subject_name):
    """Записывает характеристику справочника в wb_charcs (с логикой дублей)."""
    charc_id = charc.get("id") if charc.get("id") is not None else charc.get("charcID")
    if charc_id is None:
        return None

    name_ru = charc.get("name") or ""
    is_required = 1 if charc.get("required") else 0
    exist_named = 1 if charc.get("existNamedField") else 0
    subj_id = str(subject_id)
    subj_nm = str(subject_name) if subject_name is not None else ""

    row = db_cursor.execute(
        "SELECT subj_ID, subj_NM FROM wb_charcs WHERE charcID = ?", (charc_id,)
    ).fetchone()

    if row is None:
        db_cursor.execute(
            """
            INSERT INTO wb_charcs
                (charcID, name_ru, is_required, existNamedField, json_mapping_key, subj_ID, subj_NM)
            VALUES (?, ?, ?, ?, NULL, ?, ?)
            """,
            (charc_id, name_ru, is_required, exist_named, subj_id, subj_nm),
        )
        return "insert"

    existing_subj_id, existing_subj_nm = row
    new_subj_id = _append_list(existing_subj_id, subj_id)
    new_subj_nm = _append_list(existing_subj_nm, subj_nm)

    if new_subj_id != (existing_subj_id or ""):
        db_cursor.execute(
            """
            UPDATE wb_charcs
            SET name_ru = ?, is_required = ?, existNamedField = ?, subj_ID = ?, subj_NM = ?
            WHERE charcID = ?
            """,
            (name_ru, is_required, exist_named, new_subj_id, new_subj_nm, charc_id),
        )
        return "update"

    return "exists"


def _process_card(db_cursor, card, categories, counters):
    """Обрабатывает одну карточку: wb_products + wb_product_values + категории."""
    art, sup = extract_art_sup(card.get("vendorCode"))
    if art is None:
        return None

    subject_id = card.get("subjectID")
    if subject_id is not None:
        subject_name = card.get("subjectName")
        categories.setdefault(subject_id, subject_name if subject_name else str(subject_id))

    root_values = {}
    for col in CARD_ROOT_COLUMNS:
        if col in card and card[col] is not None:
            root_values[col] = _first_or_join(card[col])

    dimensions = card.get("dimensions") or {}
    for dim_key in ("width", "height", "length", "weightBrutto"):
        if dimensions.get(dim_key) is not None:
            root_values[dim_key] = dimensions[dim_key]

    # Значения из массива sizes (chrtID, skus, techSize, wbSize) — есть у всех
    # категорий товаров, но лежат не на верхнем уровне, а внутри sizes.
    root_values.update(_extract_sizes(card.get("sizes")))

    # Характеристики: паспортные пишутся в корневые колонки, остальные — в EAV.
    # Универсальные вложенные поля (documents и любые будущие) — тоже в EAV,
    # но с field_name вместо charcID. Собираем всё в один список eav_entries.
    eav_entries = []
    for charc in card.get("characteristics") or []:
        charc_id = charc.get("id") if charc.get("id") is not None else charc.get("charcID")
        if charc_id is None:
            continue
        value = charc.get("value")
        if charc_id in PASSPORT_CHARC_MAP:
            root_values[PASSPORT_CHARC_MAP[charc_id]] = _first_or_join(value)
        else:
            eav_entries.append((charc_id, None, _serialize(value)))

    # Автозапись «неизвестных» полей верхнего уровня: новые скалярные поля
    # создают колонки в wb_products, вложенные объекты/массивы — строки в EAV.
    handled = set(PRODUCT_ROOT_COLUMNS) | HANDLED_TOP_KEYS
    for key, value in card.items():
        if key in handled or value is None:
            continue
        if isinstance(value, (dict, list)):
            eav_entries.append((None, key, _json_dumps(value)))
        else:
            root_values[key] = value

    if eav_entries:
        # Чистим «хвосты» (характеристики и универсальные поля, которых уже нет
        # в ответе), затем пишем актуальные значения чистым INSERT.
        _delete_product_values(db_cursor, art, sup)
        for charc_id, field_name, value in eav_entries:
            _insert_product_value(db_cursor, art, sup, charc_id, field_name, value)
            counters["values_inserted"] += 1

    return _upsert_product(db_cursor, art, sup, root_values)


def run() -> None:
    """Главная функция модуля. Вызывается лаунчером в отдельном потоке."""
    _configure_logging()

    print("[DBase] Started execution...")
    _logger.info("Начало инициализации базы данных и выгрузки карточек.")

    started = time.perf_counter()

    # 1. Проверка токена и зависимостей.
    _ensure_env_file()
    api_key = _load_api_key()
    if api_key is None:
        print("[DBase] Ошибка: Заполните WB_TOKEN_CONTENT или WB_MASTER_TOKEN в файле .env")
        _logger.error("Ошибка: Заполните WB_TOKEN_CONTENT или WB_MASTER_TOKEN в файле .env")
        return

    # 2. Структура базы данных.
    os.makedirs(DATA_DIR, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    session = None

    counters = {
        "cards": 0,
        "products_inserted": 0,
        "products_updated": 0,
        "values_inserted": 0,
        "values_updated": 0,
    }

    try:
        _create_tables(connection)
        _migrate_product_columns(connection)
        _migrate_charcs_columns(connection)
        _migrate_product_values_columns(connection)
        _drop_obsolete_product_columns(connection)
        _create_unique_indexes(connection)
        _seed_charcs(connection)

        print(f"[DBase] База данных создана/открыта: {DB_PATH}")
        _logger.info("База данных создана/открыта: %s", DB_PATH)

        session = requests.Session()
        session.headers.update({
            "Authorization": api_key,
            "Content-Type": "application/json",
        })

        # 3. Выгрузка карточек (Шаг 1 и 2).
        # Сортировка по возрастанию + сохранённый cursor дают инкрементальную
        # выгрузку: при первом запуске выкачиваются все карточки, при следующих —
        # только созданные/обновлённые после предыдущей выгрузки.
        categories = {}
        db_cursor = connection.cursor()
        cursor_state = _load_cursor() or {"limit": CARDS_PAGE_SIZE}

        while True:
            data, cursor_state = _fetch_cards_page(session, cursor_state)
            cards = data.get("cards") or []
            if not cards:
                break

            for card in cards:
                counters["cards"] += 1
                result = _process_card(db_cursor, card, categories, counters)
                if result == "insert":
                    counters["products_inserted"] += 1
                elif result == "update":
                    counters["products_updated"] += 1
            connection.commit()

            print(f"[DBase] Скачано карточек: {counters['cards']}")
            _logger.info("Скачано карточек: %d", counters["cards"])

            cursor_meta = data.get("cursor") or {}
            _save_cursor(cursor_meta)

            # По документации WB: когда total < limit — получены все карточки.
            total = cursor_meta.get("total")
            if total is not None and total < CARDS_PAGE_SIZE:
                break

            next_updated = cursor_meta.get("updatedAt")
            next_nm = cursor_meta.get("nmID")
            if not next_updated and not next_nm:
                break
            if (
                next_updated == cursor_state.get("updatedAt")
                and next_nm == cursor_state.get("nmID")
            ):
                break
            cursor_state = {
                "updatedAt": next_updated,
                "nmID": next_nm,
                "limit": CARDS_PAGE_SIZE,
            }

        _logger.info(
            "Выгрузка карточек завершена. Уникальных категорий: %d", len(categories)
        )

        # 4. Синхронизация справочника характеристик (Шаг 3 и 4).
        charcs_inserted = 0
        charcs_updated = 0
        for subject_id, subject_name in categories.items():
            print(f"[DBase] Опрашиваю категорию: {subject_name} ({subject_id})")
            _logger.info("Опрашиваю категорию: %s (%s)", subject_name, subject_id)

            url = CHARCS_URL_TEMPLATE.format(subject_id=subject_id)
            try:
                response = _http_request(session, "GET", url)
            except requests.RequestException as exc:
                _logger.error(
                    "Не удалось получить характеристики категории %s: %s",
                    subject_id, exc,
                )
                continue

            characteristics = _parse_json(response)
            if not isinstance(characteristics, list):
                continue

            for charc in characteristics:
                result = _upsert_charc(db_cursor, charc, subject_id, subject_name)
                if result == "insert":
                    charcs_inserted += 1
                elif result == "update":
                    charcs_updated += 1
            connection.commit()

        _logger.info(
            "Справочник wb_charcs: добавлено %d, обновлено %d характеристик.",
            charcs_inserted, charcs_updated,
        )

        elapsed = time.perf_counter() - started
        print(f"[DBase] Скачано карточек: {counters['cards']}")
        print(
            f"[DBase] wb_products: вставлено {counters['products_inserted']}, "
            f"обновлено {counters['products_updated']}"
        )
        print(
            f"[DBase] wb_product_values: вставлено {counters['values_inserted']}, "
            f"обновлено {counters['values_updated']}"
        )
        print(f"[DBase] wb_charcs: добавлено {charcs_inserted}, обновлено {charcs_updated}")
        print(f"[DBase] Выполнение завершено за {elapsed:.3f} с.")

        _logger.info(
            "Итоги: карточек=%d, wb_products(+%d/~%d), wb_product_values(+%d/~%d), "
            "wb_charcs(+%d/~%d).",
            counters["cards"], counters["products_inserted"], counters["products_updated"],
            counters["values_inserted"], counters["values_updated"],
            charcs_inserted, charcs_updated,
        )
    finally:
        if session is not None:
            session.close()
        connection.close()

    print("[DBase] Finished successfully.")


if __name__ == "__main__":
    # Позволяет запускать модуль и напрямую: python apps/DBase.py
    run()
