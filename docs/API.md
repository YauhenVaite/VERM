# API-интеграции лаунчера

Документация по всем HTTP-методам, эндпоинтам и телам запросов, которые
используются лаунчером (`main.py`) и его модулями из папки `apps/`.

> Бот интегрирован как модуль `apps/Bot.py` + демон `apps/_BotDaemon.py`; его эндпоинты описаны в отдельных разделах ниже.

Все обращения к внешним API в рамках лаунчера и его модулей выполняются к
**Wildberries Content API v2**, **Ozon Seller API** и **DeepSeek API**. Лаунчер (`main.py`)
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

**Структура ответа**

Wildberries возвращает объект, в котором массив характеристик лежит в поле
`data`:

```json
{
  "data": [
    {
      "id": 14177452,
      "name": "Описание",
      "required": true,
      "existNamedField": true
    }
  ]
}
```

Модуль обходит все уникальные `subjectID`, собранные при выгрузке карточек,
достаёт массив из поля `data` и записывает характеристики в таблицу `wb_charcs`.

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

## Методы Wildberries Content API — создание и восстановление карточек (CardsCreator)

Модуль `apps/CardsCreator.py` создаёт новые карточки, копии с новым SUP и
восстанавливает удалённые: собирает данные (из локальной БД для восстановления/копии
либо из «Конструктора полей» для новой карточки), формирует запрос на создание,
генерирует баркод, загружает фото (локальные файлы через `media/file`, ссылки из БД
через `media/save`) и выставляет остатки. Все запросы идут синхронно через `requests`
в одном фоновом потоке-воркере и гейтируются Rate Limiter'ом
(`DBase.LIMITER.wait_for_token(...)`).

### 1. Генерация баркодов

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://content-api.wildberries.ru/content/v2/barcodes` |
| Модуль | `apps/CardsCreator.py` |
| Rate-категория | `CONTENT` |
| Таймаут | 60 с |

**Заголовки**

```http
Authorization: {токен}
Content-Type: application/json
```

**Тело запроса**

```json
{ "count": 10 }
```

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|----------|
| `count` | `int` | да | Сколько уникальных баркодов сгенерировать. |

**Ответ (200)**

```json
{
  "data": ["5032781145187", "5032781145188"],
  "error": false,
  "errorText": "",
  "additionalErrors": ""
}
```

Баркоды хранятся в пуле `data/wb_barcode_pool.json` (пачка по 10) и расходуются
по одному на карточку; новая пачка генерируется только когда пул пуст.

### 2. Создание карточки товара

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://content-api.wildberries.ru/content/v2/cards/upload` |
| Модуль | `apps/CardsCreator.py` |
| Rate-категория | `CONTENT` |
| Таймаут | 60 с |

**Заголовки**

```http
Authorization: {токен}
Content-Type: application/json
```

**Тело запроса**

```json
[
  {
    "subjectID": 105,
    "variants": [
      {
        "vendorCode": "АртикулПродавца",
        "kizMarked": false,
        "title": "Наименование товара",
        "description": "Описание товара",
        "brand": "Бренд",
        "dimensions": { "length": 12, "width": 7, "height": 5, "weightBrutto": 1.242 },
        "documents": { "items": [], "excludeDocuments": true },
        "characteristics": [
          { "id": 12, "value": ["Turkish flag"] },
          { "id": 25471, "value": 1200 },
          { "id": 14177449, "value": ["red"] }
        ],
        "sizes": [
          { "techSize": "0", "wbSize": "42", "price": 5000, "skus": ["5032781145187"] }
        ]
      }
    ]
  }
]
```

**Описание полей (вариант)** — `vendorCode`, `kizMarked`, `title`, `description`,
`brand`, `dimensions` (длина/ширина/высота/вес брутто), `characteristics`
(список `{ id, value }`), `sizes` (список `{ techSize, wbSize, price, skus }`),
а также `documents`. При восстановлении в `skus` подставляется новый
сгенерированный баркод; тип каждого `value` характеристики определяется
эвристикой либо вручную в форме.

Поле `documents` при восстановлении отправляется всегда, но пока захардкожено:
`{"items": [], "excludeDocuments": true}` (в форме галочка «Документы не нужны»
включена по умолчанию). Если галочку снять — отправляется
`{"items": [], "excludeDocuments": false}` и показывается напоминание
«Добавьте документы на сайте». Метод асинхронный — карточка появляется в
`cards/list` позже либо попадает в черновик с ошибками.

### 3. Проверка создания карточки и числа фото (cards/list)

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://content-api.wildberries.ru/content/v2/get/cards/list` |
| Модуль | `apps/CardsCreator.py` (также `apps/DBase.py`) |
| Rate-категория | `CONTENT` |
| Таймаут | 60 с |

**Тело запроса (поиск по артикулу)**

```json
{
  "settings": {
    "sort": { "ascending": false },
    "filter": { "withPhoto": -1, "textSearch": "АртикулПродавца" }
  }
}
```

**Тело запроса (по nmID)**

```json
{
  "settings": {
    "sort": { "ascending": false },
    "filter": { "withPhoto": -1, "nmIDs": [213864079] }
  }
}
```

Используется для двух проверок: появление карточки (`textSearch = vendorCode`)
и количество загруженных фото (`nmIDs`, затем `len(card.photos)`).

### 4. Получение несозданных карточек с ошибками

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://content-api.wildberries.ru/content/v2/cards/error/list` |
| Модуль | `apps/CardsCreator.py` |
| Rate-категория | `CONTENT` |
| Лимит | 10 запросов/мин (интервал 6 с, всплеск 5) |

**Заголовки**

```http
Authorization: {токен}
Content-Type: application/json
```

**Тело запроса**

```json
{
  "cursor": { "limit": 100 },
  "order": { "ascending": true }
}
```

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|----------|
| `cursor.limit` | `int` | да | Размер пакета (до 100). |
| `order.ascending` | `bool` | да | Порядок выдачи пакетов. |

**Ответ (200, сокращённо)**

```json
{
  "data": {
    "items": [
      {
        "batchUUID": "b15fecaf-57fd-4b63-ab6f-18d630b8793e",
        "vendorCodes": ["АртикулПродавца"],
        "errors": { "АртикулПродавца": ["Поле Наименование содержит запрещённые символы"] },
        "updatedAt": "2025-12-19T23:59:59Z"
      }
    ],
    "cursor": { "next": false }
  },
  "error": false,
  "errorText": "",
  "additionalErrors": null
}
```

Модуль периодически (каждый третий опрос) проверяет, не попал ли
восстанавливаемый `vendorCode` в `data.items[].vendorCodes`. Если попал —
восстановление прерывается с показом текстов ошибок.

### 5. Загрузка медиафайла к карточке

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://content-api.wildberries.ru/content/v3/media/file` |
| Модуль | `apps/CardsCreator.py` |
| Rate-категория | `CONTENT` |
| Content-Type | `multipart/form-data` |

**Заголовки**

```http
Authorization: {токен}
X-Nm-Id: 213864079
X-Photo-Number: 1
```

| Заголовок | Тип | Обязательный | Описание |
|-----------|-----|--------------|----------|
| `X-Nm-Id` | `string` | да | Артикул WB (новый `nmID` созданной карточки). |
| `X-Photo-Number` | `int` | да | Номер файла, начиная с 1; больше числа уже загруженных. |

**Тело запроса** — `multipart/form-data`, поле `uploadfile` (бинарный файл).

**Ответ (200)**

```json
{ "data": {}, "error": false, "errorText": "", "additionalErrors": null }
```

Требования к изображениям: до 30 шт., от 700×900 px, до 32 Мб, качество от 65%,
форматы JPG/PNG/BMP/GIF(static)/WebP. Главное фото загружается первым (номер 1).

### 6. Загрузка медиафайлов по ссылкам

| Характеристика | Значение |
|----------------|----------|
| Метод | `POST` |
| URL | `https://content-api.wildberries.ru/content/v3/media/save` |
| Модуль | `apps/CardsCreator.py` |
| Rate-категория | `CONTENT` |
| Content-Type | `application/json` |

**Заголовки**

```http
Authorization: {токен}
Content-Type: application/json
```

**Тело запроса**

```json
{
  "nmId": 213864079,
  "data": [
    "https://basket-stage-02.dasec.ru/vol669/part66964/66964260/images/big/2.jpg"
  ]
}
```

| Поле | Тип | Обязательное | Описание |
|------|-----|--------------|----------|
| `nmId` | `int` | да | Артикул WB (новый `nmID` созданной карточки). |
| `data` | `string[]` | да | Прямые ссылки на медиа в порядке их следования в карточке (главное фото первым). |

**Ответ (200)**

```json
{ "data": {}, "error": false, "errorText": "", "additionalErrors": null }
```

Метод полностью заменяет медиа карточки указанными ссылками. Ссылки должны вести
напрямую на файл (без авторизации) и заканчиваться именем файла с расширением.
Используется для копий карточек: берутся ссылки `big` из `wb_product_values`
(поле `photos`). При невалидной ссылке ни один файл не загрузится, поэтому после
вызова число фото проверяется через `cards/list`.

### 7. Установка остатков

| Характеристика | Значение |
|----------------|----------|
| Метод | `PUT` |
| URL | `https://marketplace-api.wildberries.ru/api/v3/stocks/{warehouseId}` |
| Модуль | `apps/CardsCreator.py` (также `apps/_BotDaemon.py`) |
| Авторизация | `Authorization: {мастер-токен}` (`get_wb_token("MASTER")`) |
| Rate-категория | `STOCKS` |

**Тело запроса**

```json
{
  "stocks": [{ "chrtId": 901053321, "amount": 5 }]
}
```

`chrtId` берётся из массива `sizes` созданной карточки, `amount` — остаток,
введённый пользователем в окне «Установка остатков».

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
| `GET` | `/getUpdates` | Получение обновлений (long polling): команды `/status`, `/stop` и нажатия inline-кнопок (`callback_query`). |
| `POST` | `/sendMessage` | Текстовые уведомления (HTML, `<code>`); опционально с inline-клавиатурой (`reply_markup`). |
| `POST` | `/sendPhoto` | Фото товара с подписью (multipart/form-data); опционально с inline-клавиатурой. |
| `POST` | `/answerCallbackQuery` | Ответ на нажатие inline-кнопки (снимает «часики» и показывает краткий toast). |

**Inline-кнопка «Заказать».** Уведомления о новом заказе WB дублируются в личный чат владельца (`YOUR_TELEGRAM_ID`) с inline-клавиатурой (`reply_markup`): кнопка `Заказать` с `callback_data = "order:{id_заказа}"`. При нажатии бот через `getUpdates` получает `callback_query`, отвечает `answerCallbackQuery` и отправляет в группу `TELEGRAM_ORDER_BUTTON_GROUP_ID` сообщение вида «Арт. {артикул}\n{название}» (фото + подпись через `sendPhoto`, либо только текст через `sendMessage`).

---

## DeepSeek API

Распознавание фотографий товаров (извлечение характеристик) выполняется модулем
`apps/CardsCreator.py` через API DeepSeek.

| Параметр | Значение |
|----------|----------|
| Базовый URL | `https://api.deepseek.com` |
| Эндпоинт | `POST /chat/completions` |
| Модуль | `apps/CardsCreator.py` |
| Модель | `deepseek-flash` (из колонки `model` таблицы `promts`) |
| Аутентификация | Заголовок `Authorization: Bearer {DEEPSEEK_TOKEN}` |
| Таймаут | 180 с |

### POST /chat/completions

Отправляет системный промт и изображения товара, возвращает ответ модели.

Заголовки:

```http
Authorization: Bearer {DEEPSEEK_TOKEN}
Content-Type: application/json
```

Тело запроса (vision):

```json
{
  "model": "deepseek-flash",
  "messages": [
    {"role": "system", "content": "<system_prompt из таблицы promts>"},
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "Извлеки характеристики из фотографий товара."},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
      ]
    }
  ],
  "temperature": 0.0,
  "stream": false,
  "response_format": {"type": "json_object"}
}
```

Описание полей:

| Поле | Тип | Назначение |
|------|-----|-----------|
| `model` | `string` | Имя модели (`deepseek-flash`). Берётся из колонки `model` таблицы `promts`. |
| `messages[].system.content` | `string` | Системный промт — текст из `promts.prompt_text`. |
| `messages[].user.content` | `array` | Массив частей: текст (`{"type":"text"}`) и изображения (`{"type":"image_url"}`). |
| `image_url.url` | `string` | Data URL (`data:image/jpeg;base64,...`) или http(s)-URL. Форматы: JPEG/PNG/GIF/WebP. |
| `temperature` | `number` | `0.0` — точное извлечение, `0.7` — творческое описание. Берётся из `promts.temperature`. |
| `stream` | `boolean` | `false` — не потоковый ответ. |
| `response_format` | `object` | `{"type":"json_object"}` — режим JSON (только если в промте `response_format = json`). |

Ответ:

```json
{
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "{\"characteristics\":[{\"name\":\"...\",\"value\":\"...\"}]}"},
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
}
```

Из ответа берётся `choices[0].message.content` — строка, которая должна быть JSON вида
`{"characteristics": [{"name": "...", "value": "..."}]}`. Модуль парсит её (в т.ч. снимая
markdown-обёртку ```json ... ```) и заполняет окно «Сверка характеристик».

### Генерация описания по фото (`description:photo`)

Параллельно с извлечением характеристик (вторым запросом по тем же фото) модуль
генерирует «Описание» карточки. Используется тот же `POST /chat/completions`, но с
промтом `description:photo` (`task_type = description`, `variant = photo`,
`response_format = text`, `temperature = 0.5`). Ответ возвращается как обычный
текст (без `response_format: json_object`), и в теле запроса поле
`response_format` не передаётся:

```json
{
  "model": "deepseek-flash",
  "messages": [
    {"role": "system", "content": "<prompt_text из promts: description:photo>"},
    {
      "role": "user",
      "content": [
        {"type": "text", "text": "Составь описание товара по фотографиям."},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
      ]
    }
  ],
  "temperature": 0.5,
  "stream": false
}
```

Из ответа берётся `choices[0].message.content` как готовый текст описания, который
сразу подставляется в поле «Описание» окна «Конструктор полей». Кнопка ✨ рядом с
полем перегенерирует описание тем же запросом. Оба запроса (характеристики +
описание) выполняются параллельно через `concurrent.futures.ThreadPoolExecutor`.

### Универсальный промпт-генератор категории (`extract:generator`)

Помимо промпта извлечения `extract:standard`, в таблице `promts` заложен
универсальный промпт-генератор `extract:generator` (`task_type = extract`,
`variant = generator`, `response_format = json`, `temperature = 0.0`). Он формирует
индивидуальный системный промпт извлечения для конкретной категории товара
(`subjectID`), чтобы не генерировать его заново при каждом распознавании фото.

Плейсхолдеры, которые подставляются в `prompt_text` перед отправкой:

| Плейсхолдер | Назначение |
|-------------|------------|
| `{{category}}` | Название категории (например, «Кроссовки»). |
| `{{subject_id}}` | Числовой `subjectID` категории. |
| `{{common_charcs}}` | Общие (паспортные) характеристики, присутствующие у карточек всех категорий. |
| `{{charcs}}` | Список характеристик категории из `wb_charcs` (с пометкой обязательности). |

Ответ генератора — JSON:

```json
{
  "category": "Кроссовки",
  "prompt_text": "<готовый системный промпт извлечения>",
  "characteristics": ["Бренд", "Модель", "Цвет", "..."]
}
```

где `prompt_text` — готовый системный промпт (сохраняется в `promts.prompt_text` и
далее используется как `system_prompt` при распознавании фото этой категории), а
`characteristics` — точный перечень названий характеристик в порядке поиска.

В сгенерированном `prompt_text` генератор классифицирует каждую характеристику по
способу получения значения и прописывает соответствующую инструкцию:

| Способ | Описание |
|--------|----------|
| `факт` | Значение написано на фото — извлекается буквально, без додумывания. |
| `синоним` | На фото значение есть, но названо иначе (например, у книг «Бренд» = издательство) — задаётся правило сопоставления. |
| `вывод` | На фото значение не написано, определяется по содержимому (например, «Повод подарка», «Кому подарок», «Страна производства» по издательству) — разрешается вывод. |

Сгенерированный промпт извлечения требует от модели ответ вида:

```json
{"characteristics": [{"name": "Бренд", "value": "Эксмо", "source": "photo"}]}
```

где `source` = `photo` для значений, взятых с фото (факт/синоним), и `inferred` для
значений, полученных выводом. Если характеристика не определена — она не
добавляется.

Сгенерированный промпт сохраняется в `promts` под кодом `extract:subject:{subjectID}`
(`variant = custom`), чтобы не генерировать его заново при каждом распознавании фото
той же категории. При повторном анализе фото этой категории используется сохранённый
промпт.

---

## Сводная таблица эндпоинтов

| Метод | Endpoint | Модуль | Назначение |
|-------|----------|--------|------------|
| `POST` | `/content/v2/get/cards/list` | `apps/DBase.py` | Выгрузка списка карточек товаров (курсорная пагинация). |
| `GET` | `/content/v2/object/charcs/{subjectId}` | `apps/DBase.py`, `apps/CardsCreator.py` | Справочник характеристик категории товаров (ответ — объект с массивом в поле `data`). |
| `POST` | `/content/v2/cards/moveNm` | `apps/Stack.py` | Объединение/разъединение карточек товаров (управление группами). |
| `POST` | `/content/v2/barcodes` | `apps/CardsCreator.py` | Генерация уникальных баркодов (пул для создания/восстановления карточек). |
| `POST` | `/content/v2/cards/upload` | `apps/CardsCreator.py` | Создание карточки товара (новая карточка, копия с новым SUP или восстановление удалённой). |
| `POST` | `/content/v2/get/cards/list` | `apps/CardsCreator.py` | Проверка появления карточки и числа загруженных фото. |
| `POST` | `/content/v2/cards/error/list` | `apps/CardsCreator.py` | Список карточек с ошибками создания (черновики). |
| `POST` | `/content/v3/media/file` | `apps/CardsCreator.py` | Загрузка фото к карточке товара (локальные файлы). |
| `POST` | `/content/v3/media/save` | `apps/CardsCreator.py` | Загрузка медиа в карточку по ссылкам (копии карточек). |
| `PUT` | `/api/v3/stocks/{warehouseId}` | `apps/CardsCreator.py` | Установка остатков после создания/восстановления карточки. |
| `POST` | `/v3/product/list` | `apps/DBase.py` | Список товаров Ozon (пагинация `last_id`). |
| `POST` | `/v3/product/info/list` | `apps/DBase.py` | Детали товаров Ozon батчами по `product_id`. |
| `POST` | `/v4/product/info/attributes` | `apps/DBase.py` | Характеристики и габариты товаров Ozon (пагинация `last_id`). |
| `POST` | `/v1/description-category/attribute` | `apps/DBase.py` | Справочник атрибутов категории Ozon. |
| `POST` | `/chat/completions` | `apps/CardsCreator.py` | DeepSeek: генерация промптов категорий и распознавание фото товаров. |
| `GET` | `/api/v3/orders/new` | `apps/_BotDaemon.py` | Список новых заказов WB. |
| `POST` | `/api/v3/orders/status` | `apps/_BotDaemon.py` | Статусы сборочных заданий WB по ID. |
| `GET` | `/api/v3/orders` | `apps/_BotDaemon.py` | Сверка отмен за период (reconciliation). |
| `POST`/`PUT` | `/api/v3/stocks/{warehouseId}` | `apps/_BotDaemon.py` | Получение/обновление остатков WB. |
| `POST` | `/v4/posting/fbs/unfulfilled/list` | `apps/_BotDaemon.py` | Невыполненные FBS-отправления Ozon. |
| `POST` | `/v2/products/stocks` | `apps/_BotDaemon.py` | Обновление остатков Ozon. |
| `POST` | `/v4/product/info/stocks` | `apps/_BotDaemon.py` | Получение остатков Ozon. |
| `GET` | `/getUpdates` | `apps/_BotDaemon.py` | Telegram long polling (команды и callback-кнопки). |
| `POST` | `/sendMessage` | `apps/_BotDaemon.py` | Telegram: текстовое уведомление. |
| `POST` | `/sendPhoto` | `apps/_BotDaemon.py` | Telegram: фото товара. |
| `POST` | `/answerCallbackQuery` | `apps/_BotDaemon.py` | Telegram: ответ на нажатие inline-кнопки. |

