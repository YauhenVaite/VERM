# API-интеграции лаунчера

Документация по всем HTTP-методам, эндпоинтам и телам запросов, которые
используются лаунчером (`main.py`) и его модулями из папки `apps/`.

> Бот интегрирован как модуль `apps/Bot.py` + демон `apps/_BotDaemon.py`; его эндпоинты описаны в отдельных разделах ниже.

Все обращения к внешним API в рамках лаунчера и его модулей выполняются к
**Wildberries Content API v2** и **Ozon Seller API**. Лаунчер (`main.py`)
напрямую сетевые запросы не выполняет — он лишь читает токены через общий
модуль `apps/DBase.py` и сохраняет настройки. Модуль `apps/_Full_Update.py`
не делает собственных HTTP-вызовов: он удаляет файл курсора и запускает
`DBase.run()`.

---

## Общие сведения

| Параметр | Значение |
|----------|----------|
| Базовый URL | `https://content-api.wildberries.ru` |
| Протокол | HTTPS |
| Формат данных | `application/json` (UTF-8) |
| Аутентификация | Заголовок `Authorization: {токен}` (без префикса `Bearer`) |
| HTTP-клиент | `requests` (синхронный) |

### Аутентификация

Каждый запрос отправляет заголовки:

```http
Authorization: {токен}
Content-Type: application/json
```

Токен берётся из файла `.env` в корне проекта. Модуль `apps/DBase.py`
предоставляет функцию `get_wb_token(category)` со следующей логикой отката:

1. Если заполнен специализированный токен категории `CONTENT`
   (`WB_TOKEN_CONTENT`) — используется он.
2. Иначе используется мастер-токен `WB_MASTER_TOKEN` (а при его отсутствии —
   устаревший ключ `WB_API_KEY` для обратной совместимости).
3. Если ни одного валидного токена нет — модуль завершает работу с ошибкой.

Токен передаётся как есть, без добавления слова `Bearer`.

### Rate limiting (ограничение частоты запросов)

Перед каждым запросом к Content API модули вызывают
`DBase.LIMITER.wait_for_token("CONTENT")`. Это общий многопоточный
Rate Limiter (алгоритм Token Bucket), который не допускает превышения лимитов
Wildberries и защищает от HTTP `429`.

Лимит категории `CONTENT` (из `apps/DBase.py`, класс `WBRateLimiter`):

| Категория | Лимит | burst (max_tokens) | refill_rate |
|-----------|-------|--------------------|-------------|
| `CONTENT` | 100 запросов/мин (~600 мс) | 5 | 100 / 60 |
| `OZ` | базовый безопасный лимит | 2 | 2.0 |

Дополнительно в `apps/DBase.py` реализованы ретраи: при сетевой ошибке или
HTTP `429` запрос повторяется до 3 раз с паузой 5 секунд.

---

## Методы Wildberries Content API v2

### 1. Получение списка карточек товаров

Выгружает карточки товаров с курсорной пагинацией. Используется модулем
`apps/DBase.py` при построении/обновлении базы данных.

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://content-api.wildberries.ru/content/v2/get/cards/list` |
| Модуль | `apps/DBase.py` |
| Таймаут | 60 с |

**Заголовки**

```http
Authorization: {токен}
Content-Type: application/json
```

**Тело запроса**

```json
{
  "settings": {
    "sort": { "ascending": true },
    "filter": { "withPhoto": -1 },
    "cursor": {
      "updatedAt": "2024-01-01T00:00:00Z",
      "nmID": 123456789,
      "limit": 100
    }
  }
}
```

**Описание полей тела запроса**

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|----------|
| `settings.sort.ascending` | `bool` | да | Сортировка по возрастанию (для инкрементальной выгрузки). |
| `settings.filter.withPhoto` | `int` | да | Фильтр по наличию фото: `-1` — все карточки (с фото и без). |
| `settings.cursor.updatedAt` | `string` | нет | Метка времени последней выгруженной карточки (продолжение пагинации). |
| `settings.cursor.nmID` | `int` | нет | `nmID` последней выгруженной карточки (продолжение пагинации). |
| `settings.cursor.limit` | `int` | нет | Размер страницы. В проекте — константа `CARDS_PAGE_SIZE = 100`. |

**Примечания по курсору (реализация в `apps/DBase.py`)**

- При самом первом запуске (или после полного обновления) запрос начинается
  с пустого курсора: `"cursor": { "limit": 100 }` — поля `updatedAt` и `nmID`
  опускаются.
- При повторных запусках курсор (`updatedAt`, `nmID`, `limit`) загружается из
  файла `data/wb_cards_cursor.json` — это обеспечивает инкрементальное
  обновление (выкачиваются только созданные/изменённые карточки).
- Если запрос с курсором `updatedAt + nmID` завершается ошибкой, модуль стирает
  `nmID` и повторяет запрос только по `updatedAt` (Wildberries иногда отклоняет
  курсор с устаревшим `nmID`).
- Пагинация продолжается до тех пор, пока в ответе `cursor.total < 100`
  (по документации WB это означает, что получены все карточки), либо пока
  курсор не перестанет изменяться.

**Структура ответа (используемые поля)**

```json
{
  "cards": [
    {
      "vendorCode": "12345ABC",
      "nmID": 123456789,
      "imtID": 111222333,
      "subjectID": 105,
      "subjectName": "Название категории",
      "title": "Наименование",
      "brand": "Бренд",
      "description": "Описание",
      "dimensions": { "width": 10, "height": 5, "length": 20, "weightBrutto": 250 },
      "sizes": [ { "chrtID": 1, "techSize": "S", "wbSize": "S", "skus": ["..."] } ],
      "characteristics": [
        { "id": 14177452, "value": "..." }
      ]
    }
  ],
  "cursor": {
    "updatedAt": "2024-01-01T00:00:00Z",
    "nmID": 123456789,
    "total": 100
  }
}
```

Поля `cursor.updatedAt` и `cursor.nmID` сохраняются в
`data/wb_cards_cursor.json` для следующей инкрементальной выгрузки.

---

### 2. Получение характеристик категории

Возвращает метаданные (справочник) характеристик для конкретной категории
товаров (`subjectId`). Используется модулем `apps/DBase.py` для заполнения
таблицы `wb_charcs`.

| Характеристика | Значение |
|----------------|----------|
| Метод | `GET` |
| URL | `https://content-api.wildberries.ru/content/v2/object/charcs/{subjectId}` |
| Модуль | `apps/DBase.py` |
| Таймаут | 60 с |

**Заголовки**

```http
Authorization: {токен}
Content-Type: application/json
```

**Параметры пути**

| Параметр | Тип | Описание |
|----------|-----|----------|
| `subjectId` | `int` | Идентификатор категории товаров (`subjectID` карточки). |

**Тело запроса**

Отсутствует (метод `GET` без тела).

**Пример запроса**

```http
GET /content/v2/object/charcs/105 HTTP/1.1
Host: content-api.wildberries.ru
Authorization: {токен}
Content-Type: application/json
```

**Структура ответа (массив характеристик)**

```json
[
  {
    "id": 14177452,
    "name": "Описание",
    "required": true,
    "existNamedField": true
  }
]
```

Модуль обходит все уникальные `subjectID`, собранные при выгрузке карточек,
и записывает характеристики в таблицу `wb_charcs`.

---

### 3. Объединение / разъединение карточек (moveNm)

Перемещает карточки (`nmID`) в группу или выводит их из группы. Используется
модулем `apps/Stack.py`.

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://content-api.wildberries.ru/content/v2/cards/moveNm` |
| Модуль | `apps/Stack.py` |
| Таймаут | 30 с |

**Заголовки**

```http
Authorization: {токен}
Content-Type: application/json
```

**Тело запроса**

```json
{
  "nmIDs": [123456789, 987654321],
  "targetIMT": 111222333
}
```

**Описание полей тела запроса**

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|----------|
| `nmIDs` | `int[]` | да | Массив `nmID` карточек. Не более **30** штук за один запрос (`MAX_CARDS`). |
| `targetIMT` | `int` | нет | Целевой `imtID` для объединения. Если поле отсутствует, Wildberries генерирует новый `imtID` (разъединение). |

**Сценарии использования (реализация в `apps/Stack.py`)**

- **Объединение карточек в группу.** Передаётся список `nmIDs` и `targetIMT` —
  карточки получают общий `imtID`.
- **Групповое разъединение.** Передаётся список `nmIDs` **без** `targetIMT` —
  выбранные карточки получают один общий новый `imtID`.
- **Поштучное разъединение.** Для каждого `nmID` отправляется отдельный запрос
  без `targetIMT` — каждой карточке присваивается свой уникальный `imtID`.
  Запросы гейтируются Rate Limiter'ом (`DBase.LIMITER.wait_for_token("CONTENT")`).

**Структура ответа**

При успехе возвращается HTTP `2xx` без обязательного тела. В проекте ответ
считается успешным, если статус `< 400` и в теле (если оно есть) отсутствует
поле `error`. Ошибки разбираются из полей `errorText` и `additionalErrors`.

---

## Методы Ozon Seller API

Базовый URL: `https://api-seller.ozon.ru`. Используются модулем `apps/DBase.py`
при синхронизации таблиц Ozon (`oz_products`, `oz_archive`, `oz_product_values`,
`oz_charcs`). Если в `.env` не заполнены `OZ_MASTER_TOKEN` и `OZON_CLIENT_ID`,
этап Ozon пропускается.

### Аутентификация Ozon

В отличие от Wildberries, Ozon требует два заголовка:

```http
Client-Id: {OZON_CLIENT_ID}
Api-Key: {OZ_MASTER_TOKEN}
Content-Type: application/json
```

Токен читается через `get_oz_token("MASTER")`, Client-Id — через
`get_oz_client_id()`. Перед каждым запросом вызывается
`DBase.LIMITER.wait_for_token("OZ")`.

---

### 4. Список товаров

Возвращает список товаров продавца с пагинацией по `last_id`.

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://api-seller.ozon.ru/v3/product/list` |
| Модуль | `apps/DBase.py` |
| Таймаут | 60 с |

**Заголовки**

```http
Client-Id: {OZON_CLIENT_ID}
Api-Key: {OZ_MASTER_TOKEN}
Content-Type: application/json
```

**Тело запроса**

```json
{
  "filter": { "visibility": "ALL" },
  "last_id": "",
  "limit": 100
}
```

**Описание полей тела запроса**

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|----------|
| `filter.visibility` | `string` | да | Фильтр видимости. `ALL` — все товары. |
| `last_id` | `string` | нет | Курсор следующей страницы (пустая строка для первой). |
| `limit` | `int` | да | Размер страницы (`OZ_PAGE_SIZE` = 100). |

**Структура ответа**

```json
{
  "result": {
    "items": [
      { "product_id": 123456789, "offer_id": "ART-SUP" }
    ],
    "total": 100,
    "last_id": "..."
  }
}
```

Пагинация: модуль повторяет запрос с `last_id` из предыдущего ответа, пока
`last_id` не станет пустым.

---

### 5. Детали товаров (список)

Возвращает детали товаров батчем по списку `product_id`.

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://api-seller.ozon.ru/v3/product/info/list` |
| Модуль | `apps/DBase.py` |
| Таймаут | 60 с |

**Заголовки**

```http
Client-Id: {OZON_CLIENT_ID}
Api-Key: {OZ_MASTER_TOKEN}
Content-Type: application/json
```

**Тело запроса**

```json
{
  "product_id": [123456789, 987654321]
}
```

**Описание полей тела запроса**

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|----------|
| `product_id` | `int[]` | да | Список ID товаров. Модуль отправляет батчами до 1000 (`OZ_INFO_BATCH_SIZE`). |

**Структура ответа (ключевые поля)**

Ответ приходит сразу на верхнем уровне (`items`, без обёртки `result`):

```json
{
  "items": [
    {
      "id": 123456789,
      "offer_id": "ART-SUP",
      "name": "Название",
      "is_archived": false,
      "is_autoarchived": false,
      "barcodes": ["9785000000000"],
      "description_category_id": 200001485,
      "type_id": 971445087,
      "created_at": "2023-03-23T09:38:59Z",
      "updated_at": "2025-10-07T12:21:42Z",
      "images": ["https://..."],
      "currency_code": "RUB",
      "min_price": "290.00",
      "old_price": "500.00",
      "price": "300.00",
      "vat": "0.00",
      "volume_weight": 0.2,
      "sources": [ { "sku": 905654295, "source": "fbo" } ],
      "stocks": { "fbo": 5 },
      "statuses": { "imported": true },
      "sku": 905654295
    }
  ]
}
```

Обработка полей модулем `DBase`:

- `id` → колонка `product_id`; `barcodes` → колонка `barcode` (склейка); `images` → колонка `images` (JSON).
- `is_archived` определяет разделение: `true` → `oz_archive`, иначе → `oz_products`.
- Остальные скалярные поля (строки/числа/булевы) → колонки `oz_products`/`oz_archive` (неизвестные создаются автоматически).
- Вложенные списки/объекты (`sources`, `commissions`, `stocks`, `statuses`, `visibility_details`, `price_indexes`, `promotions`, `availabilities` и т.п.) → `oz_product_values` (`field_name` = имя поля, `value` = JSON).
- `description_category_id` + `type_id` накапливаются для запроса справочника атрибутов.

---

### 6. Справочник атрибутов категории

Возвращает список атрибутов для категории.

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://api-seller.ozon.ru/v1/description-category/attribute` |
| Модуль | `apps/DBase.py` |
| Таймаут | 60 с |

**Заголовки**

```http
Client-Id: {OZON_CLIENT_ID}
Api-Key: {OZ_MASTER_TOKEN}
Content-Type: application/json
```

**Тело запроса**

```json
{
  "description_category_id": 17028922,
  "type_id": 123,
  "language": "RU"
}
```

**Описание полей тела запроса**

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|----------|
| `description_category_id` | `int` | да | ID категории описания. |
| `type_id` | `int` | да | ID типа товара. |
| `language` | `string` | да | Язык значений (`RU`). |

**Структура ответа**

```json
{
  "result": [
    {
      "id": 85,
      "name": "Цвет",
      "description": "Цвет товара",
      "type": "String",
      "is_collection": false,
      "is_required": true,
      "dictionary_id": 123
    }
  ]
}
```

Модуль записывает эти данные в `oz_charcs` (`attribute_id`, `name`,
`description`, `is_required`, `is_collection`, `data_type`, `dictionary_id`).

---

### 7. Характеристики товаров

Возвращает характеристики (`attributes`) и габариты товаров. В отличие от
`product/info/list`, который не содержит атрибутов, этот метод отдаёт их
вместе с размерами упаковки.

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://api-seller.ozon.ru/v4/product/info/attributes` |
| Модуль | `apps/DBase.py` |
| Таймаут | 60 с |

**Заголовки**

```http
Client-Id: {OZON_CLIENT_ID}
Api-Key: {OZ_MASTER_TOKEN}
Content-Type: application/json
```

**Тело запроса**

```json
{
  "filter": {
    "product_id": ["123456789"],
    "offer_id": [],
    "sku": [],
    "visibility": "ALL"
  },
  "last_id": "",
  "limit": 1000,
  "sort_by": "id",
  "sort_dir": "ASC"
}
```

**Описание полей тела запроса**

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|----------|
| `filter.product_id` | `string[]` | нет | Поиск по `product_id` (значения — строки). |
| `filter.offer_id` | `string[]` | нет | Поиск по артикулу продавца. |
| `filter.sku` | `string[]` | нет | Поиск по SKU Ozon. |
| `filter.visibility` | `string` | да | Фильтр видимости (`ALL`). |
| `last_id` | `string` | нет | Курсор следующей страницы. |
| `limit` | `int` | да | Размер страницы (1–1000). |
| `sort_by` / `sort_dir` | `string` | нет | Поле и направление сортировки. |

**Структура ответа (ключевые поля)**

```json
{
  "result": [
    {
      "id": 213761435,
      "offer_id": "21470",
      "name": "Плёнка защитная",
      "barcode": "",
      "barcodes": ["123124123"],
      "type_id": 124572394,
      "description_category_id": 71107562,
      "height": 10, "depth": 210, "width": 140, "dimension_unit": "mm",
      "weight": 50, "weight_unit": "g",
      "images": ["https://..."],
      "attributes": [
        { "id": 85, "complex_id": 0, "values": [ { "dictionary_value_id": 971034861, "value": "Brand" } ] }
      ],
      "attributes_with_defaults": [5435, 3452],
      "complex_attributes": []
    }
  ],
  "total": 1,
  "last_id": "onVsfA=="
}
```

Обработка полей модулем `DBase`:

- `attributes[].id` + `values[].value` → `oz_product_values` (`attribute_id`, `value`).
- `complex_attributes` и `attributes_with_defaults` → `oz_product_values` (по `field_name`, JSON).
- габариты (`height`, `depth`, `width`, `weight`, `dimension_unit`, `weight_unit`) → колонки `oz_products`.
- пагинация — по `last_id`, пока он не станет пустым.

---

## Методы Wildberries Marketplace API v3 (бот)

Используются демоном `apps/_BotDaemon.py` (запускается модулем `apps/Bot.py`).
Базовый URL: `https://marketplace-api.wildberries.ru`, авторизация — заголовок
`Authorization: {токен}` (мастер-токен WB, `get_wb_token("MASTER")`).
Лимиты сборочных заданий: 300 запр/мин (интервал 200 мс, всплеск 20);
**запрос с кодом 4XX учитывается как 10 запросов**, поэтому демон не ретраит
4xx и открывает circuit breaker.

### 1. Список новых заказов

| Характеристика | Значение |
|----------------|----------|
| Метод | `GET` |
| URL | `https://marketplace-api.wildberries.ru/api/v3/orders/new` |
| Модуль | `apps/_BotDaemon.py` |
| Таймаут | 60 с |

Заголовок: `Authorization: {токен}`. Тела запроса нет. Ответ — `{ "orders": [...] }`.

### 2. Статусы сборочных заданий

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://marketplace-api.wildberries.ru/api/v3/orders/status` |
| Модуль | `apps/_BotDaemon.py` |

**Тело запроса**

```json
{ "orders": [5632423] }
```

`orders` — массив ID сборочных заданий (до 1000).

**Ответ**

```json
{
  "orders": [
    { "id": 5632423, "isCancellable": false, "supplierStatus": "new", "wbStatus": "waiting" }
  ]
}
```

`supplierStatus`: `new`, `confirm`, `complete`, `cancel`, `cancel_carrier`.
`wbStatus`: `waiting`, `sorted`, `sold`, `canceled`, `canceled_by_client`,
`declined_by_client`, `defect`, `ready_for_pickup`, `accepted_by_carrier`,
`sent_to_carrier`, `canceled_by_carrier`.

### 3. Заказы за период (сверка отмен)

| Характеристика | Значение |
|----------------|----------|
| Метод | `GET` |
| URL | `https://marketplace-api.wildberries.ru/api/v3/orders?limit=1000&next={next}&dateFrom={unix}&dateTo={unix}` |
| Модуль | `apps/_BotDaemon.py` |

Используется для сверки отмен за период (`dateFrom`/`dateTo` в Unix-time) с курсорной пагинацией через `next`. Ответ — `{ "orders": [...], "next": 0 }`, где каждый заказ содержит `id`, `article` (vendorCode), `nmId`, `chrtId`. Так ловятся отмены, случившиеся пока бот был выключен.

> **Важно:** этот эндпоинт **не возвращает** `supplierStatus` и `wbStatus`.
> Поэтому после получения списка заказов демон дозапрашивает статусы через
> `POST /api/v3/orders/status` батчами до 1000 `id` (см. раздел 2) и по ним
> определяет, является ли заказ отменой (`cancel_message`).

### 4. Остатки товаров (получение/обновление)

| Характеристика | Значение |
|----------------|----------|
| URL | `https://marketplace-api.wildberries.ru/api/v3/stocks/{warehouseId}` |
| Модуль | `apps/_BotDaemon.py` |

- Получение остатков — `POST` с телом `{ "chrtIds": [123] }`.
- Обновление остатков — `PUT` с телом `{ "stocks": [{ "chrtId": 123, "amount": 5 }] }`.

---

## Методы Ozon Seller API (бот)

Используются демоном `apps/_BotDaemon.py`. Базовый URL:
`https://api-seller.ozon.ru`, заголовки `Client-Id` и `Api-Key`.

### 1. Невыполненные FBS-отправления

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://api-seller.ozon.ru/v4/posting/fbs/unfulfilled/list` |
| Модуль | `apps/_BotDaemon.py` |

**Тело запроса**

```json
{
  "sort_dir": "ASC",
  "limit": 100,
  "filter": {
    "cutoff_from": "2026-08-19T12:01:00.000Z",
    "cutoff_to": "2026-10-18T12:01:00.000Z",
    "statuses": ["awaiting_packaging"]
  },
  "cursor": "",
  "with": {
    "analytics_data": false,
    "barcodes": false,
    "financial_data": false,
    "legal_info": false
  }
}
```

`filter` использует фильтр по времени сборки через `cutoff_from`/`cutoff_to`
(альтернатива — `delivering_date_from`/`delivering_date_to`; использовать обе
пары сразу нельзя). Демон задаёт окно ±30 дней от завтрашнего дня 12:01.
`limit` ограничен диапазоном (0, 100]; выгрузка идёт постранично через `cursor`,
пока в ответе `has_next` == true.

Ответ — объект с массивом отправлений (поле `postings`) и флагами `has_next` /
`cursor` для следующей страницы. Каждое отправление содержит `posting_number`,
`status` и `products` (список объектов с `offer_id`, `name`, `quantity`, `sku`).

### 2. Обновление остатков

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://api-seller.ozon.ru/v2/products/stocks` |
| Модуль | `apps/_BotDaemon.py` |

**Тело запроса**

```json
{
  "stocks": [
    { "offer_id": "9785001957812", "stock": 5, "warehouse_id": 1020005000394863 }
  ]
}
```

Обновление отправляется **только для товаров, найденных в `oz_products`**
(жёсткая связка `WB article == Ozon offer_id`).

### 3. Информация об остатках (получение)

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://api-seller.ozon.ru/v4/product/info/stocks` |
| Модуль | `apps/_BotDaemon.py` |

**Тело запроса**

```json
{
  "cursor": "",
  "filter": {
    "offer_id": ["9785001957812"],
    "visibility": "ALL"
  },
  "limit": 1000
}
```

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|----------|
| `cursor` | `string` | нет | Курсор пагинации (пустая строка для первой страницы). |
| `filter.offer_id` | `string[]` | нет | Список `offer_id` (артикулов) для запроса остатков. |
| `filter.visibility` | `string` | да | Видимость товара (`ALL`). |
| `limit` | `int` | нет | Размер страницы (до 1000). |

**Структура ответа**

```json
{
  "items": [
    {
      "offer_id": "9785001957812",
      "product_id": 1000123456,
      "stocks": [
        { "sku": 1000123456, "type": "fbs", "present": 150, "reserved": 25 },
        { "sku": 1000123456, "type": "fbo", "present": 75, "reserved": 10 }
      ]
    }
  ],
  "cursor": "next-cursor-12345",
  "total": 1
}
```

Бот берёт запись `stocks[]` с `type == "fbs"` и считает доступный остаток как
`present - reserved`. Результат используется в уведомлении о новом заказе Ozon
(«Остаток: Oz - …шт, Wb - …шт»).

---

## Telegram Bot API (бот)

Базовый URL: `https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}`.

| Метод | Endpoint | Назначение |
|-------|----------|------------|
| `GET` | `/getUpdates` | Получение обновлений (long polling; команды `/status`, `/stop`). |
| `POST` | `/sendMessage` | Текстовые уведомления (HTML, `<code>`). |
| `POST` | `/sendPhoto` | Фото товара с подписью (multipart/form-data). |

---

## Сводная таблица эндпоинтов

| Метод | Endpoint | Модуль | Назначение |
|-------|----------|--------|------------|
| `POST` | `/content/v2/get/cards/list` | `apps/DBase.py` | Выгрузка списка карточек товаров (курсорная пагинация). |
| `GET` | `/content/v2/object/charcs/{subjectId}` | `apps/DBase.py` | Справочник характеристик категории товаров. |
| `POST` | `/content/v2/cards/moveNm` | `apps/Stack.py` | Объединение/разъединение карточек товаров (управление группами). |
| `POST` | `/v3/product/list` | `apps/DBase.py` | Список товаров Ozon (пагинация `last_id`). |
| `POST` | `/v3/product/info/list` | `apps/DBase.py` | Детали товаров Ozon батчами по `product_id`. |
| `POST` | `/v4/product/info/attributes` | `apps/DBase.py` | Характеристики и габариты товаров Ozon (пагинация `last_id`). |
| `POST` | `/v1/description-category/attribute` | `apps/DBase.py` | Справочник атрибутов категории Ozon. |
| `GET` | `/api/v3/orders/new` | `apps/_BotDaemon.py` | Список новых заказов WB. |
| `POST` | `/api/v3/orders/status` | `apps/_BotDaemon.py` | Статусы сборочных заданий WB по ID. |
| `GET` | `/api/v3/orders` | `apps/_BotDaemon.py` | Сверка отмен за период (reconciliation). |
| `POST`/`PUT` | `/api/v3/stocks/{warehouseId}` | `apps/_BotDaemon.py` | Получение/обновление остатков WB. |
| `POST` | `/v4/posting/fbs/unfulfilled/list` | `apps/_BotDaemon.py` | Невыполненные FBS-отправления Ozon. |
| `POST` | `/v2/products/stocks` | `apps/_BotDaemon.py` | Обновление остатков Ozon. |
| `POST` | `/v4/product/info/stocks` | `apps/_BotDaemon.py` | Получение остатков Ozon. |
| `GET` | `/getUpdates` | `apps/_BotDaemon.py` | Telegram long polling. |
| `POST` | `/sendMessage` | `apps/_BotDaemon.py` | Telegram: текстовое уведомление. |
| `POST` | `/sendPhoto` | `apps/_BotDaemon.py` | Telegram: фото товара. |

