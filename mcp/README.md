# MCP-сервер «Пятёрочки»

`mcp/mcp_server.py` — стандартный **MCP stdio-сервер** без внешних зависимостей
от SDK: чистый JSON-RPC поверх stdin/stdout. `mcp_http_server.py` — тот же
набор инструментов по **Streamable HTTP** (`http://127.0.0.1:8765/mcp`).

## Инструменты

| Tool | Аргументы | Описание |
|---|---|---|
| `refresh_session` | — | Прогреть 5ka.ru и принять свежие защитные cookies |
| `find_store` | `address` | Найти физический магазин по адресу → `store_id` |
| `get_store_info` | `store_id` | Карточка магазина: адрес, метро, координаты |
| `get_store_hours` | `store_id` | Часы работы / круглосуточность |
| `find_nearest_stores` | `latitude`, `longitude`, `limit` | Ближайшие магазины с расстоянием |
| `list_stores_in_area` | 4 координаты области | Магазины в прямоугольнике карты |
| `list_store_categories` | `store_id` | Дерево категорий магазина |
| `search_products` | `store_id`, `query`, + фильтры | Поиск товаров с фильтрами |
| `list_category_products` | `store_id`, `category_id`, + фильтры | Товары категории |
| `find_products` | `query`, `store_id` или `address` | Универсальный поиск |
| `get_product_promotion` | `store_id`, `plu` | Условия акции на товар |
| `get_product_info` | `store_id`, `plu` | Состав, калории, БЖУ, ссылка |

Фильтры поиска: `price_min`, `price_max`, `brand`, `promo_only`,
`sort` (`popularity` / `price_asc` / `price_desc` / `discount_desc`),
`limit`, `offset`.

В ответах не передаётся сырой payload API (`raw`) — только нормализованные
поля, чтобы не раздувать контекст модели.

## Запуск вручную

```bash
# Windows
set PYATEROCHKA_COOKIES_FILE=C:\secrets\pyaterochka\cookies.json
python mcp\mcp_server.py          # stdio
python mcp_http_server.py         # HTTP → http://127.0.0.1:8765/mcp

# Linux/macOS
PYATEROCHKA_COOKIES_FILE=~/secrets/cookies.json python mcp/mcp_server.py
```

Если `PYATEROCHKA_COOKIES_FILE` не задан, сервер ищет `cookies.json` рядом с
проектом. Прокси при необходимости:

```bash
set PYATEROCHKA_PROXY=socks5://USER:PASSWORD@HOST:PORT
```

## Подключение MCP-клиента (stdio)

```json
{
  "mcpServers": {
    "pyaterochka": {
      "command": "python",
      "args": ["C:/полный/путь/к/pyaterochka-mcp-tool/mcp/mcp_server.py"],
      "env": {
        "PYATEROCHKA_COOKIES_FILE": "C:/полный/путь/к/cookies.json"
      }
    }
  }
}
```

Подходит для Claude Desktop, Cursor, opencode и любых клиентов со схемой
`mcpServers`. Для HTTP-варианта укажите в клиенте URL `http://127.0.0.1:8765/mcp`
(или публичный адрес через ngrok/cloudflared — см. корневой README).

## Сценарий модели

1. `find_store` по адресу пользователя;
2. `search_products` / `list_category_products` с полученным `store_id`;
3. фильтры цены, бренда и акций;
4. короткий итоговый список с ценами.

Пример запроса:

```text
Найди в Пятёрочке по адресу Москва, Кировоградская улица, 17 молоко
дешевле 200 рублей, отсортируй по цене.
```

Модель вызовет:

```json
{
  "address": "Москва, Кировоградская улица, 17",
  "query": "молоко",
  "price_max": 200,
  "sort": "price_asc",
  "limit": 20
}
```

## Безопасность

`cookies.json` содержит действующую веб-сессию — не коммитьте его,
не отправляйте значения cookies в чаты и логи. Если файл утёк, очистите
cookies на сайте и экспортируйте новые.
