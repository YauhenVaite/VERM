"""Модуль DBase — единая точка управления схемой базы данных проекта.

Это ЕДИНСТВЕННЫЙ модуль во всём проекте, которому разрешено создавать,
изменять или удалять таблицы и колонки (структуру) SQLite-базы данных.
Все остальные модули могут только читать, добавлять или обновлять строки.

При запуске модуль выполняет монолитный алгоритм инициализации:

  Wildberries:
  1) получает токен категории CONTENT через get_wb_token() из .env
     (при отсутствии специализированного токена используется мастер-токен);
  2) создаёт/обновляет схему БД;
  3) выгружает карточки товаров (пагинация курсором) и заполняет
     wb_products (корневые параметры) и wb_product_values (характеристики);
  4) опрашивает справочник характеристик Wildberries и заполняет wb_charcs.

  Ozon (необязательно — пропускается без OZ_MASTER_TOKEN / OZON_CLIENT_ID):
  5) выгружает список товаров (/v3/product/list) и их детали
     (/v3/product/info/list), раскладывая активные карточки в oz_products,
     архивные — в oz_archive, а атрибуты активных — в oz_product_values;
  6) опрашивает справочник атрибутов (/v1/description-category/attribute)
     и заполняет oz_charcs.

Схема построена под реальный формат ответов Wildberries API v2:
  * POST /content/v2/get/cards/list — список карточек (пагинация курсором);
  * GET  /content/v2/object/charcs/{subjectId} — метаданные характеристик;
и Ozon Seller API:
  * POST /v3/product/list — список товаров (пагинация last_id);
  * POST /v3/product/info/list — детали товаров (батчами по product_id);
  * POST /v1/description-category/attribute — справочник атрибутов.

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
# Метка времени последнего успешного обновления БД. Используется лаунчером,
# чтобы не запускать DBase повторно, если он недавно уже обновлялся (например,
# после создания карточки в Cards Creator).
LAST_UPDATE_PATH = os.path.join(DATA_DIR, "dbase_last_update.json")

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
    ("TELEGRAM_BOT_TOKEN", "Токен Telegram-бота"),
    ("YOUR_TELEGRAM_ID", "Telegram ID владельца"),
    ("TELEGRAM_GROUP_ID", "Telegram ID группы уведомлений"),
    ("TELEGRAM_ORDERS_GROUP_ID", "Telegram ID группы заказов"),
    ("TELEGRAM_CANCELS_GROUP_ID", "Telegram ID группы отмен"),
    ("TELEGRAM_ORDER_BUTTON_GROUP_ID", "Telegram ID группы для кнопки «Заказать»"),
    ("OZON_ENABLED", "Обработка Ozon (true/false)"),
    ("NOTIFICATIONS_ENABLED", "Уведомления (true/false)"),
    ("WB_CHECK_INTERVAL", "Интервал проверки WB (мин)"),
    ("OZON_DELAY_AFTER_WB", "Пауза перед Ozon после WB (сек)"),
    ("ORDERS_HISTORY_DAYS", "Дней хранения истории заказов"),
    ("DEEPSEEK_TOKEN", "Токен DeepSeek (распознавание фото)"),
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
# Промты для DeepSeek (Seed). Каждый кортеж:
# (code, name, task_type, variant, marketplace, lang, model,
#  temperature, response_format, prompt_text)
# ---------------------------------------------------------------------------
SEED_PROMTS = [
    (
        "extract:standard",
        "Извлечение характеристик",
        "extract",
        "standard",
        "any",
        "ru",
        "deepseek-flash",
        0.0,
        "json",
        "Ты — ассистент по атрибуции товаров для маркетплейса. "
        "Товар относится к категории: «{{category}}». "
        "Допустимые характеристики этой категории (ищи на фото ТОЛЬКО их):\n"
        "{{charcs}}\n"
        "Извлеки из фотографий только те характеристики из списка выше, которые "
        "реально видны на фото. Имя характеристики пиши точно как в списке. "
        "Если характеристика из списка не видна на фото — не добавляй её. "
        "Запрещено выдумывать характеристики или их значения. "
        "Ответь строго JSON без пояснений в формате: "
        '{"characteristics": [{"name": "...", "value": "..."}]}',
    ),
    (
        "extract:generator",
        "Генератор промпта категории (универсальный)",
        "extract",
        "generator",
        "any",
        "ru",
        "deepseek-flash",
        0.0,
        "json",
        """Ты — промпт-инженер, который создаёт специализированные системные промпты
для извлечения характеристик товаров из фотографий (для маркетплейса Wildberries).

ТЕБЕ ДАНЫ:
1. Категория: «{{category}}» (subjectID = {{subject_id}}).
2. Общие (паспортные) характеристики — есть у карточек всех категорий:
{{common_charcs}}
3. Характеристики, специфичные для этой категории (пометка «обязательная» = критично):
{{charcs}}

ТВОЯ ЗАДАЧА
Составь ГОТОВЫЙ системный промпт (текст инструкции для другой LLM), по которому
та будет извлекать характеристики именно категории «{{category}}» из фотографий
товара и возвращать чистый JSON для базы данных.

ВАЖНО ПРО ПОЛЕ «Описание» (description):
Оно НЕ извлекается с фотографии — это творческое описание, которое генерируется
отдельным промптом с повышенной температурой. Поэтому НЕ включай «Описание» в
список извлекаемых характеристик и не упоминай его в сгенерированном промпте.

ДЛЯ КАЖДОЙ ХАРАКТЕРИСТИКИ ОПРЕДЕЛИ СПОСОБ ПОЛУЧЕНИЯ ЗНАЧЕНИЯ и пропиши его в
сгенерированном промпте. Способ может быть одним из трёх:
1. «ФАКТ» — значение написано прямо на фото (биржа, ярлык, коробка, страница).
   В промпте пиши: ищи на фото, извлекай буквально, ничего не додумывай; если не
   видно — не добавляй характеристику.
2. «СИНОНИМ» — на фото значение есть, но названо иначе (например, у книг вместо
   «Бренд» на фото указано издательство). В промпте пиши: «если на фото <что
   искать>, запиши его значение в характеристику „<точное имя>“».
3. «ВЫВОД» — значение на фото НЕ написано, его надо определить по содержимому и
   здравому смыслу (например, «Повод подарка», «Кому подарок», «Страна
   производства» по издательству). В промпте пиши: определи значение по содержимому
   фото и пометь его как «вывод» (source = "inferred").

СГЕНЕРИРОВАННЫЙ ПРОМПТ ОБЯЗАН:
1. Начинаться с роли: «Ты — ассистент по атрибуции товаров категории
   „{{category}}“ для маркетплейса Wildberries.»
2. Содержать ПОЛНЫЙ перечень характеристик — объедини общие и специфичные — ровно
   с теми же названиями, что в списках выше. Ничего не добавляй и не убирай (кроме
   поля «Описание», которое исключается).
3. Для каждой характеристики указывать способ получения («факт» / «синоним» /
   «вывод») и короткую подсказку, где и как её искать или определять.
4. Для «фактов» жёстко запрещать выдумывать значения, которых нет на фото.
5. Требовать писать имя характеристики ТОЧНО как в перечне (без синонимов, без
   перефразировок, без изменения регистра).
6. Требовать добавлять характеристику в ответ ТОЛЬКО если её значение реально
   определено (видно на фото или обоснованно выведено).
7. Требовать приводить значения к аккуратному текстовому виду: числа и единицы
   измерения — как на бирке, без додумывания.
8. Заканчиваться требованием вернуть СТРОГО JSON без пояснений в формате:
   {"characteristics": [{"name": "...", "value": "...", "source": "photo"|"inferred"}]}
   где "source" = "photo" для значений, взятых с фото (факт/синоним), и
   "inferred" для значений, полученных выводом. Если характеристика не определена —
   не добавляй её.

Сгенерированный промпт должен быть САМОДОСТАТОЧНЫМ: если подставить его как
system_prompt, другая модель должна без дополнительных пояснений вернуть
корректный JSON со списком характеристик.

ОТВЕТЬ СТРОГО JSON без пояснений в формате:
{
  "category": "{{category}}",
  "prompt_text": "<готовый системный промпт целиком>",
  "characteristics": ["<точное название 1>", "<точное название 2>", "..."]
}
где "prompt_text" — готовый системный промпт (одна строка, внутренние кавычки и
переносы строк должны быть экранированы как в обычной JSON-строке), а
"characteristics" — точный список названий характеристик в том же порядке, в
котором они должны искаться.""",
    ),
    (
        "description:standard",
        "Описание — обычный товар",
        "description",
        "standard",
        "any",
        "ru",
        "deepseek-flash",
        0.7,
        "text",
        "Ты — копирайтер, пишущий продающие описания товаров для маркетплейса. "
        "Ниже — аннотация/исходный текст о товаре:\n"
        "{{characteristics}}\n\n"
        "Напиши на его основе живое маркетинговое описание: расскажи, о чём товар "
        "(для книги — сюжет/суть), чем он цепляет и полезен покупателю.\n"
        "Требования:\n"
        "- начни с цепляющего первого предложения;\n"
        "- эмоционально, вовлекающе, естественно — как человек советует другу;\n"
        "- строго правдиво: не выдумывай фактов, которых нет в исходном тексте;\n"
        "- НЕ перечисляй характеристики списком и не дублируй их (автор, бренд, "
        "ISBN, размеры, вес и т.п. — они уже указаны в карточке отдельно);\n"
        "- 2-3 ключевых слова вплети органично для SEO;\n"
        "- объём 300-700 символов.\n"
        "Ответь только готовым текстом описания, без заголовков и пояснений.",
    ),
    (
        "description:photo",
        "Описание — по фото (vision)",
        "description",
        "photo",
        "any",
        "ru",
        "deepseek-flash",
        0.5,
        "text",
        "Ты — копирайтер, пишущий продающие описания товаров для маркетплейса. "
        "Посмотри на фотографии товара и напиши на их основе живое маркетинговое "
        "описание: расскажи, о чём товар (для книги — сюжет/суть), чем он цепляет "
        "и полезен покупателю.\n"
        "Требования:\n"
        "- начни с цепляющего первого предложения;\n"
        "- эмоционально, вовлекающе, естественно — как человек советует другу;\n"
        "- СТРОГО правдиво: не выдумывай фактов, имён, событий и цифр, которых нет "
        "на фото; бери имена, названия и сюжет буквально с фото;\n"
        "- если на фото что-то не читается или неоднозначно — не додумывай, напиши "
        "обобщённо или опусти эту деталь;\n"
        "- НЕ перечисляй характеристики списком и не дублируй их (автор, бренд, "
        "ISBN, размеры, вес и т.п. — они уже указаны в карточке отдельно);\n"
        "- 2-3 ключевых слова вплети органично для SEO;\n"
        "- объём 300-700 символов.\n"
        "Ответь только готовым текстом описания, без заголовков и пояснений.",
    ),
    (
        "description:kit",
        "Описание — комплект",
        "description",
        "kit",
        "any",
        "ru",
        "deepseek-flash",
        0.7,
        "text",
        "Составь описание товара-комплекта на русском языке. Перечисли состав "
        "комплекта по данным {{characteristics}}, подчеркни выгоду покупки "
        "набора целиком. Не выдумывай элементы, которых нет в данных.",
    ),
    (
        "description:foreign",
        "Описание — иностранный товар",
        "description",
        "foreign",
        "any",
        "ru",
        "deepseek-flash",
        0.7,
        "text",
        "Товар может содержать текст на иностранном языке. Переведи или "
        "интерпретируй его и составь описание на русском языке на основе "
        "{{characteristics}}. Сохрани фактические характеристики, не выдумывай.",
    ),
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
_ART_SUP_TABLES = ("status", "wb_products", "oz_products", "oz_archive")

# Карточные таблицы, в которых ведётся мягкое удаление (is_deleted) и
# хранится метка последней полной сверки (last_seen).
_CARD_FLAG_TABLES = ("wb_products", "oz_products", "oz_archive")

# Таблицы значений с составным ключом UNIQUE(ART, SUP, charcID).
_EAV_TABLES = ("wb_product_values",)

# Таблицы значений Ozon: ключ UNIQUE(ART, SUP, attribute_id) (не charcID).
_OZ_EAV_TABLES = ("oz_product_values",)

# ---------------------------------------------------------------------------
# Интеграция с Ozon Seller API.
# ---------------------------------------------------------------------------
OZ_API_BASE_URL = "https://api-seller.ozon.ru"
OZ_PRODUCT_LIST_URL = f"{OZ_API_BASE_URL}/v3/product/list"
OZ_PRODUCT_INFO_LIST_URL = f"{OZ_API_BASE_URL}/v3/product/info/list"
OZ_ATTRIBUTE_URL = f"{OZ_API_BASE_URL}/v1/description-category/attribute"
OZ_PRODUCT_ATTRIBUTES_URL = f"{OZ_API_BASE_URL}/v4/product/info/attributes"

# Размер страницы списка товаров и размер батча деталей (product/info/list).
OZ_PAGE_SIZE = 100
OZ_INFO_BATCH_SIZE = 1000

# Колонки таблиц oz_products / oz_archive (известные скалярные поля ответа
# product/info/list). Остальные скалярные поля добавляются автоматически.
# Поля id/barcodes/images обрабатываются нестандартно (см. OZ_SPECIAL_KEYS).
OZ_PRODUCT_COLUMNS = {
    "offer_id": "TEXT",
    "name": "TEXT",
    "product_id": "INTEGER",      # из поля id
    "sku": "INTEGER",
    "barcode": "TEXT",             # из поля barcodes (склейка)
    "description_category_id": "INTEGER",
    "type_id": "INTEGER",
    "price": "TEXT",
    "old_price": "TEXT",
    "min_price": "TEXT",
    "currency_code": "TEXT",
    "vat": "TEXT",
    "is_archived": "INTEGER",
    "is_autoarchived": "INTEGER",
    "is_discounted": "INTEGER",
    "is_prepayment_allowed": "INTEGER",
    "volume_weight": "REAL",
    "height": "INTEGER",
    "depth": "INTEGER",
    "width": "INTEGER",
    "weight": "INTEGER",
    "dimension_unit": "TEXT",
    "weight_unit": "TEXT",
    "created_at": "TEXT",
    "updated_at": "TEXT",
    "images": "TEXT",              # JSON
}

# Поля product/info/list, раскладываемые нестандартно (не в одноимённую колонку).
OZ_SPECIAL_KEYS = frozenset({"id", "barcodes", "images"})

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


def _oz_products_columns_sql() -> str:
    """SQL-фрагмент корневых колонок таблиц oz_products / oz_archive."""
    return ", ".join(
        f"{name} {col_type}" for name, col_type in OZ_PRODUCT_COLUMNS.items()
    )


def _drop_legacy_oz_tables(cursor) -> None:
    """Удаляет таблицы Ozon старой схемы (они пересоздаются с нуля).

    Ozon-этап — это полный пересбор каталога при каждом запуске, поэтому
    такие таблицы можно безопасно пересоздавать. Признак устаревшей схемы —
    отсутствие колонки field_name в oz_product_values (таблицы создавались
    до актуализации схемы).
    """
    has_values = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='oz_product_values'"
    ).fetchone()
    if not has_values:
        return

    values_cols = {row[1] for row in cursor.execute("PRAGMA table_info(oz_product_values)")}
    if "field_name" in values_cols:
        return

    for table in ("oz_products", "oz_archive", "oz_product_values", "oz_charcs"):
        cursor.execute(f"DROP TABLE IF EXISTS {table}")
    _logger.info("Ozon: удалены таблицы старой схемы (будут пересозданы).")


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

    # Выбранные пользователем поля (конструктор) для категорий: JSON-список ключей
    # полей, которые нужно показывать/собирать для категории subject_id.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS category_fields (
            subject_id INTEGER PRIMARY KEY,
            enabled_keys TEXT NOT NULL
        )
        """
    )

    # Промты для DeepSeek: служебные шаблоны system_prompt.
    # task_type: 'extract' (точное извлечение характеристик) | 'description'
    # (творческое описание). variant уточняет тип товара (standard/kit/foreign).
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS promts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            task_type TEXT NOT NULL,
            variant TEXT NOT NULL DEFAULT 'standard',
            marketplace TEXT NOT NULL DEFAULT 'any',
            lang TEXT NOT NULL DEFAULT 'ru',
            model TEXT NOT NULL DEFAULT 'deepseek-flash',
            temperature REAL NOT NULL DEFAULT 0.0,
            response_format TEXT NOT NULL DEFAULT 'json',
            prompt_text TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
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
            is_deleted INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
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

    # Ozon: удаляем таблицы устаревшей схемы перед (пере)созданием.
    _drop_legacy_oz_tables(cursor)

    # Ozon: справочник атрибутов из /v1/description-category/attribute.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS oz_charcs (
            attribute_id INTEGER PRIMARY KEY,
            name TEXT,
            description TEXT,
            is_required INTEGER,
            is_collection INTEGER,
            is_aspect INTEGER,
            data_type TEXT,
            dictionary_id INTEGER,
            max_value_count INTEGER,
            group_name TEXT
        )
        """
    )

    # Ozon: активные карточки (корневые параметры).
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS oz_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ART TEXT NOT NULL,
            SUP TEXT,
            {_oz_products_columns_sql()},
            is_deleted INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            UNIQUE(ART, SUP)
        )
        """
    )

    # Ozon: архивные карточки (тот же набор корневых колонок, что и oz_products).
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS oz_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ART TEXT NOT NULL,
            SUP TEXT,
            {_oz_products_columns_sql()},
            is_deleted INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            UNIQUE(ART, SUP)
        )
        """
    )

    # Ozon: динамические значения активных карточек (EAV-паттерн).
    # attribute_id — ссылка на oz_charcs (атрибуты); field_name — имя вложенного
    # поля верхнего уровня (sources, commissions, stocks и т.п., хранится JSON).
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS oz_product_values (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ART TEXT NOT NULL,
            SUP TEXT,
            attribute_id INTEGER,
            field_name TEXT,
            value TEXT,
            FOREIGN KEY(attribute_id) REFERENCES oz_charcs(attribute_id),
            UNIQUE(ART, SUP, attribute_id)
        )
        """
    )

    # Заказы Wildberries: активные и история в одной таблице.
    # Локальные флаги (notified_*/is_active/saw_sorted) управляют дедупликацией
    # уведомлений; сырые статусы supplier_status/wb_status пишутся как есть.
    # Неизвестные скалярные поля ответа добавляются колонками автоматически.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS wb_orders (
            id INTEGER PRIMARY KEY,
            article TEXT,
            nmId INTEGER,
            chrtId INTEGER,
            supplier_status TEXT,
            wb_status TEXT,
            is_cancellable INTEGER,
            saw_sorted INTEGER DEFAULT 0,
            notified_new INTEGER DEFAULT 0,
            notified_cancel INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            first_seen_at TEXT,
            last_seen_at TEXT,
            history_at TEXT
        )
        """
    )

    # Заказы Ozon (FBS-отправления).
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS oz_orders (
            posting_number TEXT PRIMARY KEY,
            offer_id TEXT,
            status TEXT,
            notified_new INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            first_seen_at TEXT,
            last_seen_at TEXT,
            history_at TEXT
        )
        """
    )

    _migrate_card_flag_columns(connection)

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

    for table in _OZ_EAV_TABLES:
        cursor.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_art_attributeid_null_sup "
            f"ON {table} (ART, attribute_id) WHERE SUP IS NULL"
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


def _migrate_card_flag_columns(connection: sqlite3.Connection) -> list:
    """Добавляет колонки мягкого удаления в карточные таблицы (если их ещё нет).

    is_deleted — признак «карточка удалена на площадке, но данные сохранены»;
    last_seen — метка последней полной сверки, по которой определяются
    отсутствующие в выгрузке карточки.
    """
    cursor = connection.cursor()
    added = []
    for table in _CARD_FLAG_TABLES:
        existing = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})")}
        if "is_deleted" not in existing:
            cursor.execute(
                f"ALTER TABLE {table} ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0"
            )
            added.append(f"{table}.is_deleted")
        if "last_seen" not in existing:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN last_seen TEXT")
            added.append(f"{table}.last_seen")
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


def _seed_promts(connection: sqlite3.Connection) -> list:
    """Наполняет таблицу promts базовыми промтами DeepSeek, если кодов нет.

    Используется INSERT OR IGNORE по уникальному ключу code, поэтому при
    повторных запусках уже добавленные промты не дублируются, а пользовательские
    правки текста не затираются. Возвращает список кодов добавленных промтов.
    """
    cursor = connection.cursor()
    seeded = []

    for row in SEED_PROMTS:
        (
            code, name, task_type, variant, marketplace, lang, model,
            temperature, response_format, prompt_text,
        ) = row
        cursor.execute(
            """
            INSERT OR IGNORE INTO promts (
                code, name, task_type, variant, marketplace, lang, model,
                temperature, response_format, prompt_text, is_active, sort_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
            """,
            (
                code, name, task_type, variant, marketplace, lang, model,
                temperature, response_format, prompt_text,
            ),
        )
        if cursor.rowcount:
            seeded.append(code)

    connection.commit()
    return seeded


def get_promt(code: str):
    """Возвращает активный промт из таблицы promts по служебному ключу code.

    Возвращает словарь с полями строки или None, если промт не найден или
    отключён (is_active = 0). Используется другими модулями только для чтения.
    """
    connection = sqlite3.connect(DB_PATH)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM promts WHERE code = ? AND is_active = 1",
            (code,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def list_promts(task_type=None):
    """Возвращает список промтов для UI/настроек (только чтение).

    При task_type=None возвращает все промты, иначе — только указанного типа
    ('extract' | 'description'). Сортировка по sort_order, затем по name.
    """
    connection = sqlite3.connect(DB_PATH)
    try:
        connection.row_factory = sqlite3.Row
        if task_type:
            rows = connection.execute(
                "SELECT * FROM promts WHERE task_type = ? ORDER BY sort_order, name",
                (task_type,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM promts ORDER BY task_type, sort_order, name"
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def save_custom_promt(code, name, prompt_text, *, task_type="extract",
                      variant="custom", marketplace="any", lang="ru",
                      model="deepseek-flash", temperature=0.0,
                      response_format="json"):
    """Создаёт или обновляет пользовательский промт по уникальному коду.

    Используется другими модулями для сохранения сгенерированных промтов
    (например, индивидуальных промтов категорий). Не изменяет схему БД —
    только вставляет или обновляет строку в таблице promts.
    """
    connection = sqlite3.connect(DB_PATH)
    try:
        existing = connection.execute(
            "SELECT id FROM promts WHERE code = ?", (code,)
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO promts (
                    code, name, task_type, variant, marketplace, lang, model,
                    temperature, response_format, prompt_text, is_active,
                    sort_order, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0,
                          datetime('now'), datetime('now'))
                """,
                (
                    code, name, task_type, variant, marketplace, lang, model,
                    temperature, response_format, prompt_text,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE promts SET
                    name = ?,
                    task_type = ?,
                    variant = ?,
                    marketplace = ?,
                    lang = ?,
                    model = ?,
                    temperature = ?,
                    response_format = ?,
                    prompt_text = ?,
                    updated_at = datetime('now')
                WHERE code = ?
                """,
                (
                    name, task_type, variant, marketplace, lang, model,
                    temperature, response_format, prompt_text, code,
                ),
            )
        connection.commit()
    finally:
        connection.close()


def get_category_fields(subject_id):
    """Возвращает set включённых ключей полей категории или None, если выбор не сохранён.

    None означает «выбор ещё не делался» (показывать все поля). Пустой set означает
    «пользователь осознанно не выбрал ни одного поля».
    """
    connection = sqlite3.connect(DB_PATH)
    try:
        row = connection.execute(
            "SELECT enabled_keys FROM category_fields WHERE subject_id = ?",
            (subject_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    try:
        keys = json.loads(row[0] or "[]")
    except ValueError:
        return None
    return set(keys) if isinstance(keys, list) else None


def save_category_fields(subject_id, enabled_keys):
    """Сохраняет выбранные поля категории (перезаписывает предыдущий выбор)."""
    data = json.dumps(sorted(enabled_keys), ensure_ascii=False)
    connection = sqlite3.connect(DB_PATH)
    try:
        existing = connection.execute(
            "SELECT 1 FROM category_fields WHERE subject_id = ?", (subject_id,)
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO category_fields (subject_id, enabled_keys) VALUES (?, ?)",
                (subject_id, data),
            )
        else:
            connection.execute(
                "UPDATE category_fields SET enabled_keys = ? WHERE subject_id = ?",
                (data, subject_id),
            )
        connection.commit()
    finally:
        connection.close()


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
        # Сборочные задания/поставки WB: 300 запр/мин (5/сек), всплеск 20.
        "WB_ORDERS": {"max_tokens": 20, "refill_rate": 5.0},
        # Базовый безопасный лимит для остальных категорий.
        "MARKETPLACE": {"max_tokens": 2, "refill_rate": 2.0},
        "STATISTICS": {"max_tokens": 2, "refill_rate": 2.0},
        "PROMOTION": {"max_tokens": 2, "refill_rate": 2.0},
        "FEEDBACKS": {"max_tokens": 2, "refill_rate": 2.0},
        # Ozon Seller API: базовый безопасный лимит (уточняется при необходимости).
        "OZ": {"max_tokens": 2, "refill_rate": 2.0},
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
def _http_request(session, method, url, *, json_body=None, retries=3, retry_delay=5, rate_category="CONTENT"):
    """Синхронный HTTP-запрос с ретраями при 429 и сетевых ошибках."""
    for attempt in range(1, retries + 1):
        # Пропускаем запрос через Rate Limiter перед каждым вызовом API.
        LIMITER.wait_for_token(rate_category)
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
def _upsert_product(db_cursor, art, sup, root_values, sync_ts=None):
    """Вставляет или обновляет корневые параметры карточки в wb_products.

    Сопоставление идёт в два этапа: сначала по текущей связке ART+SUP (это
    актуальная строка), затем по стабильному nmID (карточку переименовали).
    Такой порядок исключает нарушение UNIQUE(ART, SUP) при смене артикула и
    корректно «воскрешает» данные при повторном появлении карточки. Помимо
    известных колонок CARD_ROOT_COLUMNS учитываются новые скалярные поля из
    root_values: под них автоматически создаются колонки.
    """
    columns = list(CARD_ROOT_COLUMNS)
    extra = [
        name for name in root_values
        if name not in columns and SQL_IDENTIFIER_RE.match(name)
    ]
    if extra:
        _ensure_product_columns(db_cursor, {name: root_values[name] for name in extra})
        columns.extend(extra)

    nm_id = root_values.get("nmID")

    # 1) Точное совпадение ART+SUP — актуальная строка.
    if sup is None:
        existing = db_cursor.execute(
            "SELECT id, ART, SUP FROM wb_products WHERE ART = ? AND SUP IS NULL", (art,)
        ).fetchone()
    else:
        existing = db_cursor.execute(
            "SELECT id, ART, SUP FROM wb_products WHERE ART = ? AND SUP = ?", (art, sup)
        ).fetchone()

    # 2) Карточку переименовали: ищем по стабильному nmID.
    if existing is None and nm_id is not None:
        existing = db_cursor.execute(
            "SELECT id, ART, SUP FROM wb_products WHERE nmID = ?", (nm_id,)
        ).fetchone()

    if existing is None:
        cols = ["ART", "SUP"] + columns + ["is_deleted", "last_seen"]
        placeholders = ", ".join(["?"] * len(cols))
        db_cursor.execute(
            f"INSERT INTO wb_products ({', '.join(cols)}) VALUES ({placeholders})",
            [art, sup] + [root_values.get(col) for col in columns] + [0, sync_ts],
        )
        return "insert"

    old_id, old_art, old_sup = existing

    # Если нашли по nmID (артикул/поставщик сменились) — чистим старые
    # EAV-значения, чтобы не оставались «хвосты» под старой связкой ART+SUP.
    if (old_art, old_sup) != (art, sup):
        _delete_product_values(db_cursor, old_art, old_sup)

    assignments = ", ".join(f"{col} = ?" for col in columns)
    db_cursor.execute(
        f"UPDATE wb_products SET ART = ?, SUP = ?, {assignments}, "
        f"is_deleted = 0, last_seen = ? WHERE id = ?",
        [art, sup] + [root_values.get(col) for col in columns] + [sync_ts, old_id],
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


def _process_card(db_cursor, card, categories, counters, sync_ts=None):
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

    return _upsert_product(db_cursor, art, sup, root_values, sync_ts)


# ---------------------------------------------------------------------------
# Ozon Seller API: выгрузка товаров и атрибутов
# ---------------------------------------------------------------------------
def _chunks(items, size):
    """Делит список на последовательные порции размером size."""
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _is_oz_archived(item):
    """Определяет, находится ли товар Ozon в архиве.

    Ozon возвращает признак `is_archived` (bool) в ответе product/info/list.
    """
    return bool(item.get("is_archived"))


def _fetch_oz_product_list(session, last_id=""):
    """Запрашивает одну страницу списка товаров Ozon (пагинация last_id)."""
    payload = {
        "filter": {"visibility": "ALL"},
        "last_id": last_id,
        "limit": OZ_PAGE_SIZE,
    }
    response = _http_request(
        session, "POST", OZ_PRODUCT_LIST_URL, json_body=payload, rate_category="OZ"
    )
    return _parse_json(response)


def _fetch_oz_product_info_list(session, product_ids):
    """Запрашивает детали товаров Ozon батчем по списку product_id."""
    payload = {"product_id": product_ids}
    response = _http_request(
        session, "POST", OZ_PRODUCT_INFO_LIST_URL, json_body=payload, rate_category="OZ"
    )
    return _parse_json(response)


def _fetch_oz_attributes(session, description_category_id, type_id):
    """Запрашивает справочник атрибутов категории Ozon."""
    payload = {
        "description_category_id": description_category_id,
        "type_id": type_id,
        "language": "RU",
    }
    response = _http_request(
        session, "POST", OZ_ATTRIBUTE_URL, json_body=payload, rate_category="OZ"
    )
    data = _parse_json(response)
    result = data.get("result")
    return result if isinstance(result, list) else []
def _serialize_oz_attribute_values(values):
    """Сводит `values` атрибута Ozon (v4) к строке для oz_product_values.

    Ozon отдаёт значения списком объектов {"dictionary_value_id": ..., "value": ...}
    либо строк. Один элемент сводится к скаляру, несколько — склеиваются запятой.
    """
    if not isinstance(values, list) or not values:
        return None

    parts = []
    for item in values:
        if isinstance(item, dict):
            value = item.get("value")
        else:
            value = item
        if value is None:
            continue
        text = str(value).strip()
        if text:
            parts.append(text)

    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts)


def _fetch_oz_product_attributes(session, product_ids):
    """Запрашивает характеристики товаров Ozon (v4, пагинация last_id)."""
    payload = {
        "filter": {"product_id": [str(pid) for pid in product_ids], "visibility": "ALL"},
        "limit": OZ_INFO_BATCH_SIZE,
    }
    items = []
    last_id = ""
    while True:
        body = dict(payload)
        if last_id:
            body["last_id"] = last_id
        response = _http_request(
            session, "POST", OZ_PRODUCT_ATTRIBUTES_URL, json_body=body, rate_category="OZ"
        )
        data = _parse_json(response)
        result = data.get("result") or []
        if isinstance(result, list):
            items.extend(result)
        last_id = data.get("last_id") or ""
        if not last_id:
            break
    return items


def _ensure_oz_product_columns(db_cursor, values: dict) -> None:
    """Добавляет в oz_products и oz_archive недостающие колонки под новые поля."""
    for table in ("oz_products", "oz_archive"):
        existing = {row[1] for row in db_cursor.execute(f"PRAGMA table_info({table})")}
        for name, value in values.items():
            if name in existing or not SQL_IDENTIFIER_RE.match(name):
                continue
            col_type = _infer_sql_type(value)
            db_cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")
            existing.add(name)


def _upsert_oz_row(db_cursor, table, art, sup, root_values, sync_ts=None):
    """Вставляет или обновляет корневые параметры товара в таблице Ozon.

    Сопоставление идёт в два этапа: сначала по текущей связке ART+SUP (это
    актуальная строка), затем по стабильному product_id (offer_id сменился).
    Такой порядок исключает нарушение UNIQUE(ART, SUP). Помимо известных колонок
    OZ_PRODUCT_COLUMNS учитываются новые скалярные поля из root_values: под них
    автоматически создаются колонки.
    `table` — строго "oz_products" либо "oz_archive" (внутренняя константа).
    """
    columns = list(OZ_PRODUCT_COLUMNS)
    extra = [
        name for name in root_values
        if name not in columns and SQL_IDENTIFIER_RE.match(name)
    ]
    if extra:
        _ensure_oz_product_columns(db_cursor, {name: root_values[name] for name in extra})
        columns.extend(extra)

    product_id = root_values.get("product_id")

    # 1) Точное совпадение ART+SUP — актуальная строка.
    if sup is None:
        existing = db_cursor.execute(
            f"SELECT id, ART, SUP FROM {table} WHERE ART = ? AND SUP IS NULL", (art,)
        ).fetchone()
    else:
        existing = db_cursor.execute(
            f"SELECT id, ART, SUP FROM {table} WHERE ART = ? AND SUP = ?", (art, sup)
        ).fetchone()

    # 2) offer_id сменился: ищем по стабильному product_id.
    if existing is None and product_id is not None:
        existing = db_cursor.execute(
            f"SELECT id, ART, SUP FROM {table} WHERE product_id = ?", (product_id,)
        ).fetchone()

    values = [root_values.get(col) for col in columns]

    if existing is None:
        cols = ["ART", "SUP"] + columns + ["is_deleted", "last_seen"]
        placeholders = ", ".join(["?"] * len(cols))
        db_cursor.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
            [art, sup] + values + [0, sync_ts],
        )
        return "insert"

    old_id, old_art, old_sup = existing

    # Если нашли по product_id (offer_id сменился) — чистим старые EAV-значения.
    if (old_art, old_sup) != (art, sup):
        _delete_oz_product_values(db_cursor, old_art, old_sup)

    assignments = ", ".join(f"{col} = ?" for col in columns)
    db_cursor.execute(
        f"UPDATE {table} SET ART = ?, SUP = ?, {assignments}, "
        f"is_deleted = 0, last_seen = ? WHERE id = ?",
        [art, sup] + values + [sync_ts, old_id],
    )
    return "update"


def _delete_oz_row(db_cursor, table, art, sup):
    """Удаляет строку товара из таблицы Ozon по связке ART + SUP."""
    if sup is None:
        db_cursor.execute(
            f"DELETE FROM {table} WHERE ART = ? AND SUP IS NULL", (art,)
        )
    else:
        db_cursor.execute(
            f"DELETE FROM {table} WHERE ART = ? AND SUP = ?", (art, sup)
        )


def _delete_oz_product_values(db_cursor, art, sup):
    """Удаляет все атрибуты активной карточки Ozon (по связке ART + SUP)."""
    if sup is None:
        db_cursor.execute(
            "DELETE FROM oz_product_values WHERE ART = ? AND SUP IS NULL", (art,)
        )
    else:
        db_cursor.execute(
            "DELETE FROM oz_product_values WHERE ART = ? AND SUP = ?", (art, sup)
        )


def _delete_oz_field_values(db_cursor, art, sup):
    """Удаляет только универсальные поля (field_name) карточки Ozon."""
    if sup is None:
        db_cursor.execute(
            "DELETE FROM oz_product_values "
            "WHERE ART = ? AND SUP IS NULL AND attribute_id IS NULL", (art,)
        )
    else:
        db_cursor.execute(
            "DELETE FROM oz_product_values "
            "WHERE ART = ? AND SUP = ? AND attribute_id IS NULL", (art, sup)
        )


def _delete_oz_attribute_values(db_cursor, art, sup):
    """Удаляет только атрибуты (attribute_id) карточки Ozon."""
    if sup is None:
        db_cursor.execute(
            "DELETE FROM oz_product_values "
            "WHERE ART = ? AND SUP IS NULL AND attribute_id IS NOT NULL", (art,)
        )
    else:
        db_cursor.execute(
            "DELETE FROM oz_product_values "
            "WHERE ART = ? AND SUP = ? AND attribute_id IS NOT NULL", (art, sup)
        )


def _delete_oz_named_field(db_cursor, art, sup, field_name):
    """Удаляет конкретное универсальное поле (field_name) карточки Ozon."""
    if sup is None:
        db_cursor.execute(
            "DELETE FROM oz_product_values "
            "WHERE ART = ? AND SUP IS NULL AND attribute_id IS NULL AND field_name = ?",
            (art, field_name),
        )
    else:
        db_cursor.execute(
            "DELETE FROM oz_product_values "
            "WHERE ART = ? AND SUP = ? AND attribute_id IS NULL AND field_name = ?",
            (art, sup, field_name),
        )


def _insert_oz_product_value(db_cursor, art, sup, attribute_id, field_name, value):
    """Вставляет строку в oz_product_values (атрибут или универсальное поле).

    Для атрибута: attribute_id задан, field_name = None.
    Для универсального вложенного поля: attribute_id = None, field_name задан,
    value — сериализованный JSON.
    """
    db_cursor.execute(
        "INSERT INTO oz_product_values (ART, SUP, attribute_id, field_name, value) "
        "VALUES (?, ?, ?, ?, ?)",
        (art, sup, attribute_id, field_name, value),
    )


def _upsert_oz_charc(db_cursor, attr):
    """Записывает атрибут справочника Ozon в oz_charcs."""
    attribute_id = attr.get("id")
    if attribute_id is None:
        return None

    name = attr.get("name") or ""
    description = attr.get("description") or ""
    is_required = 1 if attr.get("is_required") else 0
    is_collection = 1 if attr.get("is_collection") else 0
    is_aspect = 1 if attr.get("is_aspect") else 0
    data_type = str(attr.get("type") or "")
    dictionary_id = attr.get("dictionary_id")
    max_value_count = attr.get("max_value_count") or 0
    group_name = attr.get("group_name") or ""

    row = db_cursor.execute(
        "SELECT 1 FROM oz_charcs WHERE attribute_id = ?", (attribute_id,)
    ).fetchone()

    if row is None:
        db_cursor.execute(
            "INSERT INTO oz_charcs "
            "(attribute_id, name, description, is_required, is_collection, is_aspect, "
            "data_type, dictionary_id, max_value_count, group_name) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (attribute_id, name, description, is_required, is_collection, is_aspect,
             data_type, dictionary_id, max_value_count, group_name),
        )
        return "insert"

    db_cursor.execute(
        "UPDATE oz_charcs SET name = ?, description = ?, is_required = ?, "
        "is_collection = ?, is_aspect = ?, data_type = ?, dictionary_id = ?, "
        "max_value_count = ?, group_name = ? WHERE attribute_id = ?",
        (name, description, is_required, is_collection, is_aspect, data_type,
         dictionary_id, max_value_count, group_name, attribute_id),
    )
    return "update"
def _process_oz_card(db_cursor, item, categories, counters, sync_ts=None):
    """Обрабатывает один товар Ozon: раскладывает в oz_products/oz_archive.

    Активные карточки пишутся в oz_products (+ вложенные поля в oz_product_values),
    архивные — в oz_archive (только корневой снимок). Возвращает строку-итог
    ("product_insert", "product_update", "archive_insert", "archive_update")
    или None, если из offer_id не удалось извлечь артикул.
    """
    art, sup = extract_art_sup(item.get("offer_id"))
    if art is None:
        return None

    description_category_id = item.get("description_category_id")
    type_id = item.get("type_id")
    if description_category_id is not None and type_id is not None:
        categories.setdefault((description_category_id, type_id), True)

    root_values = {}
    eav_entries = []
    for key, value in item.items():
        if value is None:
            continue
        if key == "id":
            root_values["product_id"] = value
        elif key == "barcodes":
            root_values["barcode"] = _first_or_join(value)
        elif key == "images":
            root_values["images"] = _json_dumps(value)
        elif isinstance(value, (dict, list)):
            if not value:
                continue
            eav_entries.append((None, key, _json_dumps(value)))
        else:
            root_values[key] = value

    if _is_oz_archived(item):
        # Переезд в архив: чистим активную запись и её значения.
        _delete_oz_row(db_cursor, "oz_products", art, sup)
        _delete_oz_product_values(db_cursor, art, sup)
        result = _upsert_oz_row(db_cursor, "oz_archive", art, sup, root_values, sync_ts)
        return f"archive_{result}" if result else None

    # Активная карточка: убираем возможный старый архивный снимок.
    _delete_oz_row(db_cursor, "oz_archive", art, sup)

    if eav_entries:
        _delete_oz_field_values(db_cursor, art, sup)
        for attribute_id, field_name, value in eav_entries:
            _insert_oz_product_value(db_cursor, art, sup, attribute_id, field_name, value)
            counters["values_inserted"] += 1

    result = _upsert_oz_row(db_cursor, "oz_products", art, sup, root_values, sync_ts)
    return f"product_{result}" if result else None


def _update_oz_dimensions(db_cursor, art, sup, item):
    """Обновляет габариты активного товара Ozon (не трогая остальные колонки)."""
    dims = {}
    for col in ("height", "depth", "width", "weight", "dimension_unit", "weight_unit"):
        if item.get(col) is not None:
            dims[col] = item[col]
    if not dims:
        return

    assignments = ", ".join(f"{col} = ?" for col in dims)
    values = [dims[col] for col in dims]
    if sup is None:
        db_cursor.execute(
            f"UPDATE oz_products SET {assignments} WHERE ART = ? AND SUP IS NULL",
            values + [art],
        )
    else:
        db_cursor.execute(
            f"UPDATE oz_products SET {assignments} WHERE ART = ? AND SUP = ?",
            values + [art, sup],
        )


def _process_oz_attributes_item(db_cursor, item, counters):
    """Обрабатывает характеристики товара из /v4/product/info/attributes.

    Записывает атрибуты (attribute_id + value) в oz_product_values, габариты
    в oz_products, а вспомогательные структуры complex_attributes и
    attributes_with_defaults — JSON по field_name. Возвращает число записанных
    значений.
    """
    art, sup = extract_art_sup(item.get("offer_id"))
    if art is None:
        return 0

    _update_oz_dimensions(db_cursor, art, sup, item)

    attr_entries = []
    for attr in item.get("attributes") or []:
        attribute_id = attr.get("id")
        if attribute_id is None:
            continue
        value = _serialize_oz_attribute_values(attr.get("values"))
        if value is None:
            continue
        attr_entries.append((attribute_id, None, value))

    written = 0
    if attr_entries:
        _delete_oz_attribute_values(db_cursor, art, sup)
        for attribute_id, field_name, value in attr_entries:
            _insert_oz_product_value(db_cursor, art, sup, attribute_id, field_name, value)
            counters["values_inserted"] += 1
            written += 1

    # Вспомогательные структуры: вложенные характеристики и id со значениями
    # по умолчанию — сохраняем как JSON по field_name (чтобы не терять данные).
    for field_name in ("complex_attributes", "attributes_with_defaults"):
        value = item.get(field_name)
        if not value:
            continue
        _delete_oz_named_field(db_cursor, art, sup, field_name)
        _insert_oz_product_value(db_cursor, art, sup, None, field_name, _json_dumps(value))
        counters["values_inserted"] += 1
        written += 1

    return written


def _mark_missing_wb_deleted(connection, sync_ts):
    """Помечает карточки WB, не встретившиеся в полной выгрузке, как удалённые."""
    db_cursor = connection.cursor()
    db_cursor.execute(
        "UPDATE wb_products SET is_deleted = 1 "
        "WHERE is_deleted = 0 AND (last_seen IS NULL OR last_seen != ?)",
        (sync_ts,),
    )
    connection.commit()


def _mark_missing_oz_deleted(connection, sync_ts):
    """Помечает товары Ozon, не встретившиеся в полной выгрузке, как удалённые."""
    db_cursor = connection.cursor()
    for table in ("oz_products", "oz_archive"):
        db_cursor.execute(
            f"UPDATE {table} SET is_deleted = 1 "
            "WHERE is_deleted = 0 AND (last_seen IS NULL OR last_seen != ?)",
            (sync_ts,),
        )
    connection.commit()


def _sync_oz(connection, is_full=False, sync_ts=None):
    """Синхронизирует таблицы Ozon (необязательный этап инициализации).

    Если токен Ozon или Client-Id не заполнены — этап молча пропускается,
    чтобы не ломать выгрузку Wildberries. При полной выгрузке (is_full=True)
    товары, не встретившиеся в списке Ozon, помечаются как удалённые.
    """
    oz_api_key = get_oz_token()
    oz_client_id = get_oz_client_id()
    if not oz_api_key or not oz_client_id:
        print("[DBase] Ozon: пропуск — не заполнены OZ_MASTER_TOKEN / OZON_CLIENT_ID.")
        _logger.info("Ozon: пропуск — отсутствует токен или Client-Id.")
        return

    session = requests.Session()
    session.headers.update({
        "Client-Id": oz_client_id,
        "Api-Key": oz_api_key,
        "Content-Type": "application/json",
    })

    counters = {
        "items": 0,
        "products_inserted": 0,
        "products_updated": 0,
        "archive_inserted": 0,
        "archive_updated": 0,
        "values_inserted": 0,
        "charcs_inserted": 0,
        "charcs_updated": 0,
    }
    db_cursor = connection.cursor()

    try:
        print("[DBase] Ozon: получаю список товаров…")
        product_ids = []
        last_id = ""
        while True:
            data = _fetch_oz_product_list(session, last_id)
            result = data.get("result") or {}
            items = result.get("items") or []
            if not items:
                break
            for item in items:
                pid = item.get("product_id")
                if pid is not None:
                    product_ids.append(pid)
            last_id = result.get("last_id") or ""
            if not last_id:
                break

        print(f"[DBase] Ozon: найдено товаров: {len(product_ids)}.")
        _logger.info("Ozon: найдено товаров: %d.", len(product_ids))

        categories = {}
        for batch in _chunks(product_ids, OZ_INFO_BATCH_SIZE):
            data = _fetch_oz_product_info_list(session, batch)
            for item in (data.get("items") or []):
                counters["items"] += 1
                outcome = _process_oz_card(db_cursor, item, categories, counters, sync_ts)
                if outcome == "product_insert":
                    counters["products_inserted"] += 1
                elif outcome == "product_update":
                    counters["products_updated"] += 1
                elif outcome == "archive_insert":
                    counters["archive_inserted"] += 1
                elif outcome == "archive_update":
                    counters["archive_updated"] += 1
            connection.commit()

        # Характеристики товаров (атрибуты + габариты) — отдельный метод v4.
        for batch in _chunks(product_ids, OZ_INFO_BATCH_SIZE):
            for item in _fetch_oz_product_attributes(session, batch):
                _process_oz_attributes_item(db_cursor, item, counters)
            connection.commit()

        for (description_category_id, type_id) in categories:
            print(
                f"[DBase] Ozon: опрашиваю атрибуты категории "
                f"{description_category_id} (тип {type_id})."
            )
            attributes = _fetch_oz_attributes(session, description_category_id, type_id)
            for attr in attributes:
                result = _upsert_oz_charc(db_cursor, attr)
                if result == "insert":
                    counters["charcs_inserted"] += 1
                elif result == "update":
                    counters["charcs_updated"] += 1
            connection.commit()

        print(
            f"[DBase] Ozon: oz_products(+{counters['products_inserted']}/~{counters['products_updated']}), "
            f"oz_archive(+{counters['archive_inserted']}/~{counters['archive_updated']}), "
            f"oz_product_values(+{counters['values_inserted']}), "
            f"oz_charcs(+{counters['charcs_inserted']}/~{counters['charcs_updated']})."
        )
        if is_full and sync_ts:
            _mark_missing_oz_deleted(connection, sync_ts)

        _logger.info("Ozon: синхронизация завершена (%s).", counters)
    finally:
        session.close()
# ---------------------------------------------------------------------------
# Заказы Wildberries и Ozon (таблицы заполняет бот-демон)
# ---------------------------------------------------------------------------
_ORDER_TABLES = frozenset({"wb_orders", "oz_orders"})


def ensure_order_columns(db_cursor, table, values: dict) -> list:
    """Создаёт в таблице заказов недостающие колонки под новые скалярные поля.

    Вложенные структуры (dict/list) должны быть уже сериализованы в JSON-строки.
    Возвращает список созданных колонок.
    """
    if table not in _ORDER_TABLES:
        raise ValueError(f"Неизвестная таблица заказов: {table}")
    existing = {row[1] for row in db_cursor.execute(f"PRAGMA table_info({table})")}
    created = []
    for name, value in values.items():
        if name in existing or not SQL_IDENTIFIER_RE.match(name):
            continue
        db_cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {_infer_sql_type(value)}")
        existing.add(name)
        created.append(name)
    return created


def upsert_order(db_cursor, table, key_column, key_value, fields: dict) -> str:
    """Вставляет или обновляет строку заказа.

    Новые скалярные поля автоматически создают колонки, вложенные структуры
    сериализуются в JSON. Возвращает 'insert' или 'update'.
    """
    if table not in _ORDER_TABLES:
        raise ValueError(f"Неизвестная таблица заказов: {table}")

    normalized = {}
    for name, value in fields.items():
        if isinstance(value, (dict, list)):
            normalized[name] = _json_dumps(value)
        elif isinstance(value, bool):
            normalized[name] = 1 if value else 0
        elif value is not None:
            normalized[name] = value

    ensure_order_columns(db_cursor, table, normalized)

    columns = list(normalized.keys())
    values = [normalized[col] for col in columns]

    existing = db_cursor.execute(
        f"SELECT 1 FROM {table} WHERE {key_column} = ?", (key_value,)
    ).fetchone()

    if existing is None:
        cols = [key_column] + columns
        placeholders = ", ".join(["?"] * len(cols))
        db_cursor.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
            [key_value] + values,
        )
        return "insert"

    assignments = ", ".join(f"{col} = ?" for col in columns)
    db_cursor.execute(
        f"UPDATE {table} SET {assignments} WHERE {key_column} = ?",
        values + [key_value],
    )
    return "update"


def refresh_wb_cards() -> bool:
    """Инкрементально обновляет каталог Wildberries по сохранённому курсору.

    Вызывается ботом-демоном при обнаружении неизвестного артикула в заказе,
    чтобы не перекачивать весь каталог заново (как это делает run()).
    Возвращает True при успехе, иначе False.
    """
    api_key = _load_api_key()
    if api_key is None:
        _logger.error("refresh_wb_cards: нет токена CONTENT/MASTER.")
        return False

    session = requests.Session()
    session.headers.update({
        "Authorization": api_key,
        "Content-Type": "application/json",
    })

    connection = sqlite3.connect(DB_PATH)
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
        _seed_promts(connection)

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

            cursor_meta = data.get("cursor") or {}
            _save_cursor(cursor_meta)

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

        _logger.info("Инкрементальное обновление карточек: %s", counters)
        return True
    except requests.RequestException as exc:
        _logger.error("Инкрементальное обновление карточек не удалось: %s", exc)
        return False
    finally:
        session.close()
        connection.close()


def write_last_update() -> None:
    """Записывает текущее время как метку последнего обновления БД."""
    try:
        with open(LAST_UPDATE_PATH, "w", encoding="utf-8") as file:
            json.dump({"updated_at": time.time()}, file, ensure_ascii=False, indent=2)
    except OSError as exc:
        _logger.warning("Не удалось записать метку обновления БД: %s", exc)


def read_last_update() -> float:
    """Возвращает epoch-время последнего обновления БД (0.0, если метки нет)."""
    try:
        if not os.path.exists(LAST_UPDATE_PATH):
            return 0.0
        with open(LAST_UPDATE_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        return float(data.get("updated_at") or 0.0)
    except (OSError, ValueError, TypeError) as exc:
        _logger.warning("Не удалось прочитать метку обновления БД: %s", exc)
        return 0.0


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
        _seed_promts(connection)

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
        # только созданные/обновлённые после предыдущей выгрузки. Сверка «кого
        # нет в выгрузке → DELETED» выполняется только при полной выгрузке
        # (когда курсор отсутствует), потому что инкрементальный поток WB не
        # сообщает об удалённых карточках.
        categories = {}
        db_cursor = connection.cursor()
        loaded_cursor = _load_cursor()
        is_full = loaded_cursor is None
        sync_ts = time.strftime("%Y-%m-%d %H:%M:%S") if is_full else None
        cursor_state = loaded_cursor or {"limit": CARDS_PAGE_SIZE}

        while True:
            data, cursor_state = _fetch_cards_page(session, cursor_state)
            cards = data.get("cards") or []
            if not cards:
                break

            for card in cards:
                counters["cards"] += 1
                result = _process_card(db_cursor, card, categories, counters, sync_ts)
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

        if is_full:
            _mark_missing_wb_deleted(connection, sync_ts)

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

            payload = _parse_json(response)
            characteristics = payload.get("data") if isinstance(payload, dict) else payload
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

        # 5. Синхронизация Ozon (необязательная — пропускается без токена/Client-Id).
        _sync_oz(connection, is_full, sync_ts)

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

    write_last_update()
    print("[DBase] Finished successfully.")


if __name__ == "__main__":
    # Позволяет запускать модуль и напрямую: python apps/DBase.py
    run()
