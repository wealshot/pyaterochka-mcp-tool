"""MCP server for the Pyaterochka store/catalog API.

The implementation intentionally uses only the MCP stdio JSON-RPC surface and
existing project dependencies, so no additional MCP SDK is required. It is
compatible with clients that support ``initialize``, ``tools/list`` and
``tools/call`` over newline-delimited stdin/stdout.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

# Allow ``python mcp/mcp_server.py`` from the project root as well as
# ``python -m mcp.mcp_server``.
PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from pyaterochka_store_api import (  # noqa: E402
    Product,
    ProductFilters,
    PyaterochkaAPIError,
    PyaterochkaStoreAPI,
    Store,
)

SERVER_NAME = "pyaterochka-catalog"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"


def _default_cookies_path() -> str | None:
    configured = os.getenv("PYATEROCHKA_COOKIES_FILE")
    if configured:
        return configured
    # Ищем cookies.json рядом с проектом (удобно для локальной разработки).
    for candidate in (PROJECT_DIR / "cookies.json", PROJECT_DIR.parent / "cookies.json"):
        if candidate.is_file():
            return str(candidate)
    return None


def _store_json(store: Store) -> dict[str, Any]:
    # raw API payload is intentionally excluded: it is large and is not needed
    # by a model to choose a store.
    return {
        "store_id": store.store_id,
        "sap_code": store.sap_code,
        "address": store.address,
        "latitude": store.latitude,
        "longitude": store.longitude,
        "distance_m": store.distance_m,
        "work_start_time": store.work_start_time,
        "work_end_time": store.work_end_time,
        "is_24h": store.is_24h,
        "metro": store.metro,
        "settlement": store.settlement,
    }


def _product_json(product: Product) -> dict[str, Any]:
    result = {
        "plu": product.plu,
        "name": product.name,
        "price": product.price,
        "old_price": product.old_price,
        "unit": product.unit,
        "size": product.size,
        "brand": product.brand,
        "category": product.category,
        "image": product.image,
        "images": list(product.images),
        "is_promo": product.is_promo,
        "promo_label": product.promo_label,
        "units_to_activate": product.units_to_activate,
        "cpd_price": product.cpd_price,
        "rating": product.rating,
        "rating_count": product.rating_count,
        "is_available": product.is_available,
        "stock_limit": product.stock_limit,
        "url": product.url,
    }
    if product.calories is not None or product.ingredients:
        result.update(
            {
                "calories_per_100g": product.calories,
                "protein_per_100g": product.protein,
                "fat_per_100g": product.fat,
                "carbs_per_100g": product.carbs,
                "ingredients": product.ingredients,
            }
        )
    return result


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _number_arg(arguments: dict[str, Any], name: str) -> float | None:
    value = arguments.get(name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Параметр {name} должен быть числом") from exc


def _int_arg(arguments: dict[str, Any], name: str, default: int) -> int:
    value = arguments.get(name, default)
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Параметр {name} должен быть целым числом") from exc
    if result < 0:
        raise ValueError(f"Параметр {name} не может быть отрицательным")
    return result


def _filters(arguments: dict[str, Any], *, query: str | None = None, category_id: str | None = None) -> ProductFilters:
    sort = str(arguments.get("sort", "popularity"))
    if sort not in {"popularity", "price_asc", "price_desc", "discount_desc"}:
        raise ValueError("sort должен быть popularity, price_asc, price_desc или discount_desc")
    return ProductFilters(
        query=query if query is not None else arguments.get("query"),
        category_id=category_id if category_id is not None else arguments.get("category_id"),
        price_min=_number_arg(arguments, "price_min"),
        price_max=_number_arg(arguments, "price_max"),
        brand=arguments.get("brand"),
        promo_only=bool(arguments.get("promo_only", False)),
        sort=sort,
        limit=_int_arg(arguments, "limit", 100),
        offset=_int_arg(arguments, "offset", 0),
    )


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


STRING = {"type": "string"}
NUMBER = {"type": "number"}
INTEGER = {"type": "integer", "minimum": 0}
BOOLEAN = {"type": "boolean"}

TOOLS = [
    _tool(
        "refresh_session",
        "Refresh the protective cookies for the current 5ka.ru web session.",
        {},
        [],
    ),
    _tool(
        "find_store",
        "Find the nearest Pyaterochka store for a full address and return its store_id.",
        {"address": {"type": "string", "description": "For example: Москва, Кировоградская улица, 17"}},
        ["address"],
    ),
    _tool(
        "get_store_info",
        "Get store details by store_id, including address, opening hours, metro and coordinates.",
        {"store_id": STRING},
        ["store_id"],
    ),
    _tool(
        "get_store_hours",
        "Get opening hours and 24-hour status for a store by store_id.",
        {"store_id": STRING},
        ["store_id"],
    ),
    _tool(
        "find_nearest_stores",
        "Find nearby Pyaterochka stores by coordinates, sorted by distance.",
        {
            "latitude": NUMBER,
            "longitude": NUMBER,
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        ["latitude", "longitude"],
    ),
    _tool(
        "get_product_promotion",
        "Get promotion details for a product PLU in a store.",
        {"store_id": STRING, "plu": STRING},
        ["store_id", "plu"],
    ),
    _tool(
        "get_product_info",
        "Get product details by PLU, including ingredients, calories, nutrients and URL.",
        {"store_id": STRING, "plu": STRING},
        ["store_id", "plu"],
    ),
    _tool(
        "list_stores_in_area",
        "List Pyaterochka stores inside a rectangular map area.",
        {
            "top_latitude": NUMBER,
            "bottom_latitude": NUMBER,
            "left_longitude": NUMBER,
            "right_longitude": NUMBER,
        },
        ["top_latitude", "bottom_latitude", "left_longitude", "right_longitude"],
    ),
    _tool(
        "list_store_categories",
        "Get the category tree for a store.",
        {"store_id": STRING},
        ["store_id"],
    ),
    _tool(
        "search_products",
        "Search products in a store and apply price, brand, promotion and sorting filters.",
        {
            "store_id": STRING,
            "query": STRING,
            "price_min": NUMBER,
            "price_max": NUMBER,
            "brand": STRING,
            "promo_only": BOOLEAN,
            "sort": {"type": "string", "enum": ["popularity", "price_asc", "price_desc", "discount_desc"]},
            "limit": INTEGER,
            "offset": INTEGER,
        },
        ["store_id", "query"],
    ),
    _tool(
        "list_category_products",
        "List products in a store category and apply filters.",
        {
            "store_id": STRING,
            "category_id": STRING,
            "price_min": NUMBER,
            "price_max": NUMBER,
            "brand": STRING,
            "promo_only": BOOLEAN,
            "sort": {"type": "string", "enum": ["popularity", "price_asc", "price_desc", "discount_desc"]},
            "limit": INTEGER,
            "offset": INTEGER,
        },
        ["store_id", "category_id"],
    ),
    _tool(
        "find_products",
        "Find products by store_id or address, resolving the nearest store when needed.",
        {
            "address": STRING,
            "store_id": STRING,
            "query": STRING,
            "price_min": NUMBER,
            "price_max": NUMBER,
            "brand": STRING,
            "promo_only": BOOLEAN,
            "sort": {"type": "string", "enum": ["popularity", "price_asc", "price_desc", "discount_desc"]},
            "limit": INTEGER,
            "offset": INTEGER,
        },
        ["query"],
    ),
]


class MCPServer:
    def __init__(self) -> None:
        self.api: PyaterochkaStoreAPI | None = None
        self._api_started = False
        self._api_lock = asyncio.Lock()

    async def start(self) -> None:
        # MCP handshake должен работать даже если интернет или cookies
        # временно недоступны. Реальный HTTP-клиент запускается лениво при
        # первом tool call, а ошибка возвращается самой нейросети.
        self.api = PyaterochkaStoreAPI(
            proxy=os.getenv("PYATEROCHKA_PROXY"),
            cookies_path=_default_cookies_path(),
            timeout_ms=60_000,
        )

    async def _ensure_api(self) -> PyaterochkaStoreAPI:
        # Локк: прогрев при старте и первый запрос пользователя могут прийти
        # одновременно — инициализировать сессию должен строго один из них.
        async with self._api_lock:
            if self.api is None:
                await self.start()
            assert self.api is not None
            if not self._api_started:
                await self.api.__aenter__()
                self._api_started = True
        return self.api

    async def close(self) -> None:
        if self.api is not None:
            await self.api.close()
            self.api = None
            self._api_started = False

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        api = await self._ensure_api()
        if name == "refresh_session":
            await api.refresh_session()
            return {"ok": True, "message": "Web-сессия прогрета; свежие защитные cookies приняты в памяти."}

        if name == "find_store":
            store = await api.resolve_store(str(arguments["address"]))
            return {"store": _store_json(store)}

        if name == "get_store_info":
            store = await api.store_info(str(arguments["store_id"]))
            return {"store": _store_json(store)}

        if name == "get_store_hours":
            store = await api.store_info(str(arguments["store_id"]))
            return {
                "store_id": store.store_id,
                "address": store.address,
                "work_start_time": store.work_start_time,
                "work_end_time": store.work_end_time,
                "is_24h": store.is_24h,
                "metro": store.metro,
            }

        if name == "find_nearest_stores":
            stores = await api.nearest_stores(
                float(arguments["latitude"]),
                float(arguments["longitude"]),
                limit=_int_arg(arguments, "limit", 10),
            )
            return {"stores": [_store_json(store) for store in stores], "count": len(stores)}

        if name == "get_product_promotion":
            products = await api.search(str(arguments["store_id"]), str(arguments["plu"]), limit=10)
            match = next((item for item in products if item.plu == str(arguments["plu"])), None)
            if match is None:
                return {
                    "found": False,
                    "message": "Товар с таким PLU не найден в поиске. Уточните PLU или поищите по названию.",
                }
            return {
                "found": True,
                "product": _product_json(match),
            }

        if name == "get_product_info":
            product = await api.product_info(str(arguments["store_id"]), str(arguments["plu"]))
            return {"product": _product_json(product)}

        if name == "list_stores_in_area":
            stores = await api.stores_in_area(
                top_latitude=float(arguments["top_latitude"]),
                bottom_latitude=float(arguments["bottom_latitude"]),
                left_longitude=float(arguments["left_longitude"]),
                right_longitude=float(arguments["right_longitude"]),
            )
            return {"stores": [_store_json(store) for store in stores], "count": len(stores)}

        if name == "list_store_categories":
            categories = await api.categories(str(arguments["store_id"]))
            return {"categories": categories, "count": len(categories)}

        if name == "search_products":
            filters = _filters(arguments, query=str(arguments["query"]))
            products = await api.products(str(arguments["store_id"]), filters)
            return {"store_id": str(arguments["store_id"]), "products": [_product_json(item) for item in products], "count": len(products), "filters": asdict(filters)}

        if name == "list_category_products":
            category_id = str(arguments["category_id"])
            filters = _filters(arguments, category_id=category_id)
            products = await api.products(str(arguments["store_id"]), filters)
            return {"store_id": str(arguments["store_id"]), "category_id": category_id, "products": [_product_json(item) for item in products], "count": len(products), "filters": asdict(filters)}

        if name == "find_products":
            store_id = arguments.get("store_id")
            store: Store
            if store_id:
                store = Store(sap_code=str(store_id), address="")
            elif arguments.get("address"):
                store = await api.resolve_store(str(arguments["address"]))
            else:
                raise ValueError("Нужно передать store_id или address")
            filters = _filters(arguments, query=str(arguments["query"]))
            products = await api.products(store, filters)
            return {"store": _store_json(store), "products": [_product_json(item) for item in products], "count": len(products), "filters": asdict(filters)}

        raise ValueError(f"Неизвестный tool: {name}")

    async def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")

        if method == "notifications/initialized":
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
        if method == "tools/call":
            params = request.get("params") or {}
            name = str(params.get("name", ""))
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                arguments = {}
            try:
                result = await self.call_tool(name, arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"content": [{"type": "text", "text": _json_text(result)}]},
                }
            except (KeyError, TypeError, ValueError, PyaterochkaAPIError, RuntimeError) as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": str(exc)}],
                    },
                }

        if request_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


async def serve() -> None:
    server = MCPServer()
    await server.start()
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("MCP message must be a JSON object")
                response = await server.handle(request)
            except (ValueError, json.JSONDecodeError) as exc:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
    finally:
        await server.close()


def main() -> None:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
