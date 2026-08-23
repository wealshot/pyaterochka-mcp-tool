"""Практичный API-слой для каталога Пятёрочки.

Модуль не зависит от внутреннего API-фреймворка старой библиотеки
``pyaterochka_api`` и поэтому может использоваться отдельным MSP/tool-агентом.
Запросы выполняются из браузерного контекста после прогрева 5ka.ru.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
from dataclasses import asdict, dataclass, replace
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urlparse

import aiohttp
from aiohttp import CookieJar
from yarl import URL

logger = logging.getLogger(__name__)



CATALOG_URL = "https://5d.5ka.ru/api"
SITE_URL = "https://5ka.ru"
DEFAULT_HEADERS = {
    "User-Agent": os.getenv(
        "PYATEROCHKA_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    # Эти заголовки и /api/ взяты из APK-экстракта, а не придуманы по
    # веб-эндпоинтам. Значения можно переопределить для другого клиента.
    "X-APP-VERSION": os.getenv("PYATEROCHKA_APP_VERSION", "8.45.0"),
    "X-PLATFORM": os.getenv("PYATEROCHKA_PLATFORM", "web"),
    "X-CAN-RECEIVE-PUSH": "true",
    "format": "json",
    "Origin": SITE_URL,
    "Referer": f"{SITE_URL}/",
} 

MAP_STORES_PATH = "/cita/v1/stores/map"
STORE_INFO_PATH = "/cita/v1/stores/{sap_code}"
ADDRESS_STORE_PATH = "/orders/v1/orders/stores/" 


class PyaterochkaAPIError(RuntimeError):
    """Ошибка ответа каталога Пятёрочки."""


@dataclass(frozen=True)
class Store:
    """Минимальные данные магазина; поле sap_code хранит актуальный store ID."""

    sap_code: str
    address: str
    latitude: float | None = None
    longitude: float | None = None
    distance_m: float | None = None
    work_start_time: str | None = None
    work_end_time: str | None = None
    is_24h: bool = False
    metro: str | None = None
    settlement: str | None = None
    raw: dict[str, Any] | None = None

    @property
    def store_id(self) -> str:
        """Современное имя идентификатора каталога (например, ``S105``)."""
        return self.sap_code


@dataclass(frozen=True)
class Product:
    """Нормализованный товар для передачи агенту."""

    plu: str
    name: str
    price: float | None
    old_price: float | None
    unit: str
    size: str
    brand: str
    category: str
    image: str
    is_promo: bool
    is_available: bool | None = None
    stock_limit: float | None = None
    promo_label: str = ""
    units_to_activate: int | None = None
    cpd_price: float | None = None
    rating: float | None = None
    rating_count: int | None = None
    images: tuple[str, ...] = ()
    raw: dict[str, Any] | None = None
    url: str = ""
    calories: float | None = None
    protein: float | None = None
    fat: float | None = None
    carbs: float | None = None
    ingredients: str = ""


@dataclass(frozen=True)
class SearchResult:
    """Результат catalog/v3/search вместе с данными для следующей страницы."""

    products: list[Product]
    categories: list[dict[str, Any]]
    query_type: str | None = None
    search_query_id: str | None = None
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class ProductFilters:
    """Фильтры, применяемые после ответа API, если сервер их игнорирует."""

    query: str | None = None
    category_id: str | None = None
    price_min: float | None = None
    price_max: float | None = None
    brand: str | None = None
    promo_only: bool = False
    sort: str = "popularity"
    limit: int = 50
    offset: int = 0


def _normal(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().casefold()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:[.,]\d+)?", str(value))
    return float(match.group(0).replace(",", ".")) if match else None


def _items(payload: Any, keys: Iterable[str]) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _position(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    source = raw.get("coordinates") or raw.get("location") or raw
    if not isinstance(source, dict):
        return None, None
    latitude = _number(source.get("latitude", source.get("lat")))
    longitude = _number(source.get("longitude", source.get("lon")))
    return latitude, longitude


def _address_key(text: Any) -> str:
    value = _normal(text)
    value = re.sub(r"\b(?:г|город|ул|улица|д|дом|к|корп|корпус|стр|строение)\b", " ", value)
    return re.sub(r"[^0-9а-яёa-z]+", " ", value).strip()


def _address_matches(left: Any, right: Any) -> bool:
    left_key = _address_key(left)
    right_key = _address_key(right)
    return bool(left_key and right_key and (left_key in right_key or right_key in left_key))


def _distance_m(latitude: float | None, longitude: float | None, target: tuple[float, float]) -> float:
    if latitude is None or longitude is None:
        return float("inf")
    dy = (latitude - target[0]) * 111_320
    dx = (longitude - target[1]) * 111_320 * math.cos(math.radians(target[0]))
    return (dx * dx + dy * dy) ** 0.5


def _address(raw: dict[str, Any]) -> str:
    value = (
        raw.get("address")
        or raw.get("shop_address")
        or raw.get("storeAddress")
        or raw.get("shopAddress")
        or raw.get("location")
    )
    if isinstance(value, dict):
        return str(
            value.get("formatted_address")
            or value.get("formattedAddress")
            or value.get("full")
            or value.get("address")
            or ""
        )
    return str(value or raw.get("name") or raw.get("title") or "")


def _store(raw: dict[str, Any]) -> Store | None:
    code = raw.get("sapCode") or raw.get("sap_code") or raw.get("storeSapCode")
    if code is None and isinstance(raw.get("store"), dict):
        return _store(raw["store"])
    if code is None:
        return None
    latitude, longitude = _position(raw)
    return Store(
        sap_code=str(code),
        address=_address(raw),
        latitude=latitude,
        longitude=longitude,
        distance_m=_number(raw.get("distance", raw.get("distance_m"))),
        work_start_time=_time_str(raw.get("work_start_time")),
        work_end_time=_time_str(raw.get("work_end_time")),
        is_24h=bool(raw.get("is_24h")),
        metro=str(raw.get("metro") or "") or None,
        settlement=str(raw.get("settlement") or "") or None,
        raw=raw,
    )


def _time_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    if text.isdigit() and len(text) >= 3:
        text = f"{text[:-2]}:{text[-2:]}"
    return text or None


def _first_url(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def _product_prices(raw: dict[str, Any]) -> tuple[float | None, float | None]:
    prices = raw.get("prices") if isinstance(raw.get("prices"), dict) else {}
    explicit_price = raw.get("price")
    explicit_old_price = raw.get("old_price", raw.get("oldPrice"))
    regular = _number(prices.get("regular", prices.get("price")))
    discount = _number(prices.get("discount"))
    cpd_price = _number(prices.get("cpd_promo_price"))
    markdown = _number(prices.get("markdown"))

    # Для реального catalog/v3 ответ имеет prices.regular и может иметь
    # discount/cpd_promo_price/markdown. В карточке нужна фактическая цена,
    # а regular — старая цена, если она действительно отличается.
    price = _number(explicit_price)
    if price is None:
        price = discount or cpd_price or markdown or regular
    old_price = _number(explicit_old_price)
    if old_price is None and regular is not None and price is not None and regular != price:
        old_price = regular
    return price, old_price


def _product(raw: dict[str, Any], category: str = "") -> Product:
    price, old_price = _product_prices(raw)
    media = raw.get("media") if isinstance(raw.get("media"), dict) else {}
    image_links = raw.get("image_links") if isinstance(raw.get("image_links"), dict) else {}
    media_links = media.get("image_links") if isinstance(media.get("image_links"), dict) else {}

    def _images(*sources: Any) -> list[str]:
        result: list[str] = []
        for source in sources:
            value = source.get("normal") or source.get("small") if isinstance(source, dict) else source
            if isinstance(value, list):
                result.extend(str(item) for item in value if isinstance(item, str))
            elif isinstance(value, str) and value:
                result.append(value)
        return result

    all_images = _images(image_links, media_links)
    image = _first_url(
        image_links.get("normal")
        or image_links.get("small")
        or media_links.get("normal")
        or media_links.get("small")
        or raw.get("image_link")
        or raw.get("image")
        or raw.get("image_url")
    )
    promo = raw.get("promo")
    prices = raw.get("prices") if isinstance(raw.get("prices"), dict) else {}
    is_promo = bool(
        raw.get("is_promo")
        or promo
        or prices.get("discount") is not None
        or prices.get("cpd_promo_price") is not None
        or prices.get("markdown") is not None
    )
    units_to_activate = None
    promo_label = ""
    if isinstance(promo, dict):
        rebate = promo.get("rebate")
        if isinstance(rebate, dict) and rebate.get("units_to_activate") is not None:
            units_to_activate = int(rebate["units_to_activate"])
    labels = raw.get("labels")
    if isinstance(labels, list):
        promo_label = ", ".join(str(item.get("label", "")) for item in labels if isinstance(item, dict) and item.get("label"))
    rating_raw = raw.get("rating") if isinstance(raw.get("rating"), dict) else None
    rating = _number(rating_raw.get("rating_average")) if rating_raw else None
    rating_count = _number(rating_raw.get("rates_count")) if rating_raw else None
    cpd_price = _number(prices.get("cpd_promo_price")) if prices.get("cpd_promo_price") is not None else None
    brand = raw.get("brand_name", raw.get("brand", ""))
    available = raw.get("is_available")
    return Product(
        plu=str(raw.get("plu", raw.get("id", ""))),
        name=str(raw.get("name", raw.get("title", ""))),
        price=price,
        old_price=old_price,
        unit=str(raw.get("unit", raw.get("uom", raw.get("measure", ""))) or ""),
        size=str(raw.get("size", raw.get("quantity", raw.get("property_clarification", ""))) or ""),
        brand=str(brand or ""),
        category=category or str(raw.get("category_name", "") or ""),
        image=image,
        is_promo=is_promo,
        is_available=bool(available) if isinstance(available, bool) else None,
        stock_limit=_number(raw.get("stock_limit")),
        promo_label=promo_label,
        units_to_activate=units_to_activate,
        cpd_price=cpd_price,
        rating=rating,
        rating_count=rating_count,
        images=tuple(all_images),
        raw=raw,
        url=_product_url(raw),
    )


def _product_url(raw: dict[str, Any]) -> str:
    """Ссылка на карточку товара на 5ka.ru."""
    slug = str(raw.get("slug") or raw.get("url_key") or raw.get("seo_url") or "").strip("/")
    plu = str(raw.get("plu", raw.get("id", "")))
    if slug:
        return f"https://5ka.ru/product/{slug}-{plu}"
    if plu:
        return f"https://5ka.ru/product/{plu}"
    return ""


def _flatten_categories(categories: list[dict[str, Any]], prefix: str = "") -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for category in categories:
        name = str(category.get("name", category.get("title", "")))
        full_name = f"{prefix} / {name}" if prefix and name else name or prefix
        result.append({"id": str(category.get("id", "")), "name": name, "full_name": full_name})
        children = category.get("categories") or category.get("children") or category.get("subcategories") or []
        if isinstance(children, list):
            result.extend(_flatten_categories([x for x in children if isinstance(x, dict)], full_name))
    return result


def filter_products(products: Iterable[Product], filters: ProductFilters) -> list[Product]:
    """Фильтрует и сортирует товары локально, чтобы поведение было стабильным."""
    query = _normal(filters.query)
    brand = _normal(filters.brand)
    query_words = [word for word in query.split() if len(word) >= 2] if query else []
    result = []
    for product in products:
        if query_words:
            name_normal = _normal(product.name)
            # Слова запроса должны встречаться в названии все вместе — но в любом
            # порядке. Это лояльнее, чем подстрока всего запроса целиком.
            if not all(word in name_normal for word in query_words):
                continue
        if brand and brand not in _normal(product.brand):
            continue
        if filters.price_min is not None and (product.price is None or product.price < filters.price_min):
            continue
        if filters.price_max is not None and (product.price is None or product.price > filters.price_max):
            continue
        if filters.promo_only and not product.is_promo:
            continue
        result.append(product)

    if filters.sort == "price_asc":
        result.sort(key=lambda item: item.price if item.price is not None else float("inf"))
    elif filters.sort == "price_desc":
        result.sort(key=lambda item: item.price if item.price is not None else float("-inf"), reverse=True)
    elif filters.sort == "discount_desc":
        result.sort(key=lambda item: (item.old_price or item.price or 0) - (item.price or 0), reverse=True)

    start = max(filters.offset, 0)
    return result[start : start + max(filters.limit, 0)]


class PyaterochkaStoreAPI:
    """Клиент: адрес магазина -> SAP-код -> категории -> товары."""

    def __init__(
        self,
        *,
        headless: bool = True,
        proxy: str | None = None,
        cookies_path: str | os.PathLike[str] | None = None,
        timeout_ms: int = 30_000,
        refresh_cookies: bool = True,
    ):
        self.headless = headless
        self.proxy = proxy or os.getenv("PYATEROCHKA_PROXY")
        self.cookies_path = Path(cookies_path or os.getenv("PYATEROCHKA_COOKIES_FILE", "")) if (cookies_path or os.getenv("PYATEROCHKA_COOKIES_FILE")) else None
        self.timeout_ms = timeout_ms
        self.refresh_cookies = refresh_cookies
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._http_session: aiohttp.ClientSession | None = None
        self._cookie_values: dict[str, str] = {}
        # Нестандартные заголовки (x-app-version, x-device-id, x-platform),
        # подсмотренные у живых запросов сайта к каталогу — как в референсной
        # библиотеке pyaterochka_api.
        self._unstandard_headers: dict[str, str] = {}
        # Совпадает с web-клиентом из проверенного запроса. Случайная метка
        # устройства при повторном использовании ServicePipe cookies может
        # выглядеть как новая подозрительная сессия.
        self._device_id = os.getenv("PYATEROCHKA_DEVICE_ID", "web-test-device")

    def _load_cookie_records(self) -> list[dict[str, Any]]:
        if self.cookies_path is None:
            return []
        if not self.cookies_path.is_file():
            raise FileNotFoundError(f"Файл cookies не найден: {self.cookies_path}")
        data = json.loads(self.cookies_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("cookies", data)
        if isinstance(data, dict):
            return [
                {"name": str(name), "value": str(value), "domain": "5ka.ru", "path": "/"}
                for name, value in data.items()
                if value is not None
            ]
        if not isinstance(data, list):
            raise ValueError("cookies-файл должен содержать список cookies браузера")
        return [
            item
            for item in data
            if isinstance(item, dict) and item.get("name") and item.get("value") is not None
        ]

    def _load_cookie_values(self) -> dict[str, str]:
        """Совместимый просмотр cookies без вывода самих значений."""
        return {
            str(item["name"]): str(item["value"])
            for item in self._load_cookie_records()
        }

    async def _open_cookie_http_session(self) -> None:
        from aiohttp_socks import ProxyConnector

        connector = None
        if self.proxy:
            parsed = urlparse(self.proxy)
            if not parsed.hostname or not parsed.port:
                raise ValueError("Прокси должен иметь вид socks5://[user:password@]host:port")
            connector = ProxyConnector.from_url(self.proxy)
        self._http_session = aiohttp.ClientSession(
            connector=connector,
            cookie_jar=CookieJar(unsafe=True),
        )
        # Важно сохранить доменную область cookies. Host-only cookies 5ka.ru
        # нельзя вручную копировать на 5d.5ka.ru: обычный браузер этого не
        # делает, а ServicePipe может воспринять такую смесь как подозрительную.
        records = self._load_cookie_records()
        self._cookie_values = {str(item["name"]): str(item["value"]) for item in records}
        for item in records:
            cookie = SimpleCookie()
            cookie[str(item["name"])] = str(item["value"])
            if item.get("domain"):
                cookie[str(item["name"])]['domain'] = str(item['domain'])
            cookie[str(item["name"])]['path'] = str(item.get('path') or '/')
            domain = str(item.get("domain") or "5ka.ru").lstrip(".")
            self._http_session.cookie_jar.update_cookies(
                cookie, response_url=URL(f"https://{domain}/")
            )
        if self.refresh_cookies:
            await self.refresh_session()

    async def __aenter__(self) -> "PyaterochkaStoreAPI":
        try:
            await self._start_browser()
            return self
        except Exception as exc:
            logger.warning(
                "Браузерный транспорт недоступен (%s: %s), переключаюсь на aiohttp+cookies",
                type(exc).__name__,
                exc,
            )
        if self.cookies_path is not None:
            await self._open_cookie_http_session()
            return self
        raise PyaterochkaAPIError("Не удалось запустить ни браузер, ни HTTP-сессию с cookies")

    async def _start_browser(self) -> None:
        """Запускает Camoufox-браузер и поднимает живую web-сессию.

        Куки из cookies-файла инжектируются в браузерный контекст, сайт
        прогревается, а нестандартные заголовки каталога снифаются с живых
        запросов — как в референсной библиотеке pyaterochka_api. После
        прогрева свежие ServicePipe-куки сохраняются обратно в файл, чтобы
        сессия переживала перезапуск.
        """
        from camoufox import AsyncCamoufox

        options: dict[str, Any] = {
            "locale": "ru-RU",
            "headless": self.headless,
            "block_images": False,
        }
        if self.proxy:
            parsed = urlparse(self.proxy)
            if not parsed.hostname or not parsed.port:
                raise ValueError("Прокси должен иметь вид socks5://[user:password@]host:port")
            if parsed.username or parsed.password:
                raise ValueError(
                    "Браузерный транспорт не поддерживает аутентифицированный прокси; "
                    "уберите пароль из PYATEROCHKA_PROXY"
                )
            server = f"{parsed.scheme or 'socks5'}://{parsed.hostname}:{parsed.port}"
            options["proxy"] = {"server": server}
        self._browser = await AsyncCamoufox(**options).start()
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()
        self._context.on("request", self._sniff_request)
        if self.cookies_path is not None:
            await self._inject_cookies()
        await self._warmup_browser()

    def _sniff_request(self, request: Any) -> None:
        """Подсматривает нестандартные заголовки у живых запросов к каталогу."""
        try:
            if not str(request.url).startswith(CATALOG_URL):
                return
            headers = request.headers or {}
            for header in ("x-app-version", "x-device-id", "x-platform"):
                value = headers.get(header)
                if value:
                    self._unstandard_headers[header] = str(value)
        except Exception:
            pass

    async def _inject_cookies(self) -> None:
        """Переносит куки из cookies-файла в браузерный контекст."""
        records = self._load_cookie_records()
        playwright_cookies: list[dict[str, Any]] = []
        for item in records:
            cookie: dict[str, Any] = {
                "name": str(item["name"]),
                "value": str(item["value"]),
                "path": str(item.get("path") or "/"),
            }
            domain = str(item.get("domain") or "")
            if domain:
                cookie["domain"] = domain
            else:
                cookie["url"] = f"{SITE_URL}/"
            expires = item.get("expirationDate")
            if isinstance(expires, (int, float)) and expires > 0:
                cookie["expires"] = float(expires)
            if item.get("httpOnly"):
                cookie["httpOnly"] = True
            if item.get("secure"):
                cookie["secure"] = True
            same_site = str(item.get("sameSite") or "").lower()
            if same_site in {"lax", "strict"}:
                cookie["sameSite"] = same_site.capitalize()
            elif same_site in {"no_restriction", "none"}:
                cookie["sameSite"] = "None"
            playwright_cookies.append(cookie)
        if playwright_cookies:
            await self._context.add_cookies(playwright_cookies)

    async def _warmup_browser(self) -> None:
        """Открывает 5ka.ru в живом браузере и забирает свежие куки/заголовки."""
        assert self._page is not None
        try:
            await self._page.goto(SITE_URL, wait_until="domcontentloaded", timeout=self.timeout_ms)
        except Exception:
            # Каталог всё равно может отвечать, даже если главная страница медленная.
            pass
        try:
            await self._page.wait_for_selector("#app", timeout=self.timeout_ms)
        except Exception:
            pass
        try:
            await self._page.locator('label[for="is-robot"].captcha-label').click(timeout=3000)
        except Exception:
            pass
        # Ждём, пока сайт сам сходит на каталог и мы подсмотрим все три
        # нестандартных заголовка. Если сайт молчит — перезагружаем страницу.
        required = ("x-app-version", "x-device-id", "x-platform")
        for _attempt in range(4):
            if all(h in self._unstandard_headers for h in required):
                break
            try:
                await self._wait_for_headers(required, min(15000, self.timeout_ms))
            except asyncio.TimeoutError:
                pass
            if not all(h in self._unstandard_headers for h in required):
                try:
                    await self._page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)
                except Exception:
                    pass
        # device-id из localStorage как запасной источник.
        try:
            device_id = await self._page.evaluate("() => localStorage.getItem('deviceId')")
            if device_id:
                self._device_id = str(device_id)
        except Exception:
            pass
        if "x-device-id" not in self._unstandard_headers and self._device_id:
            self._unstandard_headers["x-device-id"] = self._device_id
        await self._persist_browser_cookies()

    async def _wait_for_headers(self, required: tuple[str, ...], timeout_ms: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000
        while loop.time() < deadline:
            if all(h in self._unstandard_headers for h in required):
                return
            await asyncio.sleep(0.2)
        raise asyncio.TimeoutError

    async def _persist_browser_cookies(self) -> None:
        """Сохраняет свежие браузерные куки обратно в cookies-файл."""
        if self.cookies_path is None or self._context is None:
            return
        try:
            fresh = await self._context.cookies()
        except Exception:
            return
        try:
            records = self._load_cookie_records()
        except OSError:
            records = []
        index = {(str(r.get("domain") or ""), str(r["name"])): r for r in records if r.get("name")}
        changed = False
        for cookie in fresh:
            name = str(cookie.get("name") or "")
            if not name:
                continue
            domain = str(cookie.get("domain") or "")
            key = (domain, name)
            record = index.get(key)
            if record is None:
                record = {"name": name, "domain": domain, "path": str(cookie.get("path") or "/")}
                records.append(record)
                index[key] = record
            record["value"] = str(cookie.get("value") or "")
            expires = cookie.get("expires", -1)
            if isinstance(expires, (int, float)) and expires > 0:
                record["expirationDate"] = float(expires)
                record["session"] = False
            record["httpOnly"] = bool(cookie.get("httpOnly"))
            record["secure"] = bool(cookie.get("secure"))
            same_site = str(cookie.get("sameSite") or "").lower()
            record["sameSite"] = {
                "lax": "lax",
                "strict": "strict",
                "none": "no_restriction",
            }.get(same_site, "no_restriction")
            changed = True
        if changed:
            try:
                self.cookies_path.write_text(
                    json.dumps(records, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception as exc:  # noqa: BLE001 — playwright шумит, если драйвер уже умер
                logger.debug("Browser.close проигнорирован: %s", exc)
            self._browser = None
            self._context = None
            self._page = None

    async def refresh_session(self) -> None:
        """Обновляет web-сессию.

        В браузерном режиме перезагружаем 5ka.ru в живом контексте: ServicePipe
        выдаёт свежие куки реальному браузеру, и мы сохраняем их в файл, чтобы
        сессия переживала перезапуск. В aiohttp-режиме просто прогреваем
        страницы старыми куками (помогает слабее).
        """
        if self._context is not None and self._page is not None:
            await self._warmup_browser()
            return
        if self._http_session is None:
            raise RuntimeError("Обновление cookies доступно только внутри async with")
        for base, origin in (
            (f"{SITE_URL}/", None),
            ("https://5d.5ka.ru/", "https://5d.5ka.ru"),
        ):
            headers = {**DEFAULT_HEADERS, "x-device-id": self._device_id}
            if origin:
                headers = {**headers, "Origin": origin, "Referer": f"{origin}/"}
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    async with self._http_session.get(
                        URL(base),
                        headers=headers,
                        timeout=self.timeout_ms / 1000,
                        allow_redirects=True,
                    ) as response:
                        if response.status != 200:
                            last_error = PyaterochkaAPIError(
                                f"Не удалось обновить web-сессию: HTTP {response.status} ({base})"
                            )
                            await asyncio.sleep(1.0)
                            continue
                    break
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                    last_error = exc
                    await asyncio.sleep(1.0)
            else:
                if last_error is not None:
                    raise last_error

    def _save_cookies(self) -> None:
        """Сохраняет текущие cookies сессии обратно в cookies-файл."""
        if not self.cookies_path or self._http_session is None:
            return
        records = self._load_cookie_records()
        fresh: dict[str, str] = {}
        for cookie in self._http_session.cookie_jar:
            key = getattr(cookie, "key", None) or getattr(cookie, "name", None)
            value = getattr(cookie, "value", None)
            if key and value:
                fresh[str(key)] = str(value)
        changed = False
        for item in records:
            name = str(item["name"])
            if name in fresh:
                item["value"] = fresh[name]
                changed = True
        if changed:
            self.cookies_path.write_text(
                json.dumps(records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    async def _request_json(self, base_url: str, path: str, params: dict[str, Any]) -> Any:
        query = f"?{urlencode(params, doseq=True)}" if params else ""
        url = URL(f"{base_url}{path}{query}", encoded=True)
        if self._http_session is not None:
            # 5d иногда отдаёт 307 на канонический URL с этим параметром — в
            # aiohttp-режиме добавляем utm_referrer заранее. Браузерному
            # транспорту он не нужен: JS-fetch ходит так же, как настоящий сайт.
            aiohttp_params = params
            if base_url == CATALOG_URL and "utm_referrer" not in aiohttp_params:
                aiohttp_params = {**aiohttp_params, "utm_referrer": f"{SITE_URL}/"}
            query = f"?{urlencode(aiohttp_params, doseq=True)}" if aiohttp_params else ""
            url = URL(f"{base_url}{path}{query}", encoded=True)
            headers = {
                **DEFAULT_HEADERS,
                "x-device-id": self._device_id,
            }
            refresh_attempted = False
            redirects = 0
            retries_5xx = 0
            while True:
                try:
                    async with self._http_session.get(
                        url,
                        headers=headers,
                        timeout=self.timeout_ms / 1000,
                        allow_redirects=False,
                    ) as response:
                        if response.status in (307, 308):
                            location = response.headers.get("Location")
                            if not location:
                                raise PyaterochkaAPIError(f"GET {path}: HTTP {response.status} без Location")
                            redirect_url = URL(location)
                            redirects += 1
                            if redirect_url == url:
                                if refresh_attempted:
                                    raise PyaterochkaAPIError(
                                        f"GET {path}: API зацикливает 307/308; cookies или антибот-контекст устарели"
                                    )
                                await self.refresh_session()
                                refresh_attempted = True
                                url = URL(f"{base_url}{path}{query}", encoded=True)
                                continue
                            if redirects > 2:
                                raise PyaterochkaAPIError(
                                    f"GET {path}: слишком много редиректов при обновлении web-сессии"
                                )
                            url = redirect_url
                            continue
                        if response.status != 200:
                            if response.status == 403 and not refresh_attempted:
                                await self.refresh_session()
                                refresh_attempted = True
                                url = URL(f"{base_url}{path}{query}", encoded=True)
                                continue
                            body = (await response.text())[:300]
                            if response.status == 403:
                                body = "защитная страница/истёкшая cookie-сессия"
                            if 500 <= response.status <= 599 and retries_5xx < 3:
                                retries_5xx += 1
                                await asyncio.sleep(1.2 * retries_5xx)
                                continue
                            raise PyaterochkaAPIError(f"GET {path}: HTTP {response.status}: {body}")
                        try:
                            return await response.json(content_type=None)
                        except (TypeError, ValueError) as exc:
                            body = (await response.text())[:300]
                            raise PyaterochkaAPIError(f"GET {path}: ответ не JSON: {body}") from exc
                except asyncio.TimeoutError as exc:
                    if refresh_attempted:
                        raise PyaterochkaAPIError(f"GET {path}: таймаут ответа каталога") from exc
                    await self.refresh_session()
                    refresh_attempted = True
                    url = URL(f"{base_url}{path}{query}", encoded=True)
                    continue
            raise AssertionError("unreachable")
        if self._context is None:
            raise RuntimeError("Используйте клиент внутри async with")
        # Запросы выполняются fetch'ем прямо из страницы браузера — как в
        # референсной библиотеке pyaterochka_api: credentials=include несёт
        # живые ServicePipe-куки, mode=cors + referrer как у сайта, а
        # нестандартные заголовки подсмотрены у живых запросов сайта.
        return await self._page_fetch_json(path, url)

    async def _page_fetch_json(self, path: str, url: URL) -> Any:
        browser_headers = {
            "Accept": "application/json, text/plain, */*",
            **self._unstandard_headers,
        }
        if "x-device-id" not in browser_headers:
            browser_headers["x-device-id"] = self._device_id
        result = await self._page_fetch(str(url), browser_headers)
        status = int(result.get("status") or 0)
        body = result.get("text") or ""
        if status == 403:
            # Cookie-сессия могла протухнуть: перепрогреваем страницу в живом
            # браузере, ServicePipe выдаёт свежие куки, пробуем ещё раз.
            await self.refresh_session()
            result = await self._page_fetch(str(url), browser_headers)
            status = int(result.get("status") or 0)
            body = result.get("text") or ""
        if status != 200:
            snippet = body[:300]
            if status == 403:
                snippet = "защитная страница/истёкшая cookie-сессия"
            raise PyaterochkaAPIError(f"GET {path}: HTTP {status}: {snippet}")
        try:
            return json.loads(body)
        except (TypeError, ValueError) as exc:
            raise PyaterochkaAPIError(f"GET {path}: ответ не JSON: {body[:300]}") from exc

    async def _page_fetch(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        """Выполняет GET fetch'ем из контекста страницы (как настоящий сайт)."""
        assert self._page is not None
        return await self._page.evaluate(
            """async (payload) => {
                const resp = await fetch(payload.url, {
                    method: 'GET',
                    headers: payload.headers,
                    credentials: 'include',
                    mode: 'cors',
                    redirect: 'follow',
                });
                const text = await resp.text();
                return { status: resp.status, text: text };
            }""",
            {"url": url, "headers": headers},
        )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request_json(CATALOG_URL, path, params or {})

    async def stores_in_area(
        self,
        *,
        top_latitude: float,
        bottom_latitude: float,
        left_longitude: float,
        right_longitude: float,
    ) -> list[Store]:
        """Список магазинов в видимой области карты из APK.

        Приложение передаёт координаты верхнего левого и нижнего правого
        углов в ``cita/v1/stores/map``.
        """
        payload = await self._get(
            MAP_STORES_PATH,
            {
                "top_latitude": top_latitude,
                "bottom_latitude": bottom_latitude,
                "left_longitude": left_longitude,
                "right_longitude": right_longitude,
            },
        )
        return [
            store
            for store in (_store(item) for item in _items(payload, ("items", "stores", "results", "data")))
            if store is not None
        ]

    async def store_info(self, sap_code: str) -> Store:
        """Получает карточку магазина по store ID из ``cita/v1/stores/{sap_code}``."""
        payload = await self._get(STORE_INFO_PATH.format(sap_code=sap_code))
        if not isinstance(payload, dict):
            raise PyaterochkaAPIError(f"Карточка магазина {sap_code} имеет неожиданный формат")
        store = _store(payload)
        if store is None:
            raise PyaterochkaAPIError(f"В карточке магазина нет sap_code: {sap_code}")
        return store

    async def nearest_stores(self, latitude: float, longitude: float, *, limit: int = 10) -> list[Store]:
        """Возвращает ближайшие магазины к координатам, отсортированные по расстоянию."""
        stores = await self.stores_in_area(
            top_latitude=latitude + 0.015,
            bottom_latitude=latitude - 0.015,
            left_longitude=longitude - 0.022,
            right_longitude=longitude + 0.022,
        )
        target = (latitude, longitude)
        ranked = sorted(
            stores,
            key=lambda item: _distance_m(item.latitude, item.longitude, target),
        )
        result = []
        for store in ranked[:limit]:
            distance = _distance_m(store.latitude, store.longitude, target)
            result.append(Store(
                sap_code=store.sap_code,
                address=store.address,
                latitude=store.latitude,
                longitude=store.longitude,
                distance_m=round(distance),
                work_start_time=store.work_start_time,
                work_end_time=store.work_end_time,
                is_24h=store.is_24h,
                metro=store.metro,
                settlement=store.settlement,
                raw=store.raw,
            ))
        return result

    async def _site_get(self, path: str, params: dict[str, Any]) -> Any:
        return await self._request_json(SITE_URL, path, params)

    async def geocode(self, address: str) -> tuple[float, float]:
        """Возвращает (широту, долготу) для полного адреса."""
        data = await self._site_get("/api/maps/geocode/", {"geocode": address})
        try:
            position = data["response"]["GeoObjectCollection"]["featureMember"][0]["GeoObject"]["Point"]["pos"]
            longitude, latitude = (float(value) for value in position.split()[:2])
            return latitude, longitude
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise PyaterochkaAPIError("Геокодер не вернул координаты адреса") from exc

    async def resolve_store(self, address: str) -> Store:
        latitude, longitude = await self.geocode(address)
        target = (latitude, longitude)

        # Для конкретного физического магазина сначала используем карту. Для
        # адреса Кировоградская, 17 это возвращает S105. Endpoint orders/stores
        # может вернуть отдельный delivery-store (например, 3AHS), который не
        # совпадает с магазином у дома.
        try:
            map_stores = await self.stores_in_area(
                top_latitude=latitude + 0.012,
                bottom_latitude=latitude - 0.012,
                left_longitude=longitude - 0.018,
                right_longitude=longitude + 0.018,
            )
        except PyaterochkaAPIError:
            map_stores = []
        exact = [item for item in map_stores if _address_matches(address, item.address)]
        if exact:
            selected = min(exact, key=lambda item: _distance_m(item.latitude, item.longitude, target))
            return Store(
                sap_code=selected.sap_code,
                address=selected.address,
                latitude=selected.latitude or latitude,
                longitude=selected.longitude or longitude,
                distance_m=_distance_m(selected.latitude, selected.longitude, target),
                work_start_time=selected.work_start_time,
                work_end_time=selected.work_end_time,
                is_24h=selected.is_24h,
                metro=selected.metro,
                settlement=selected.settlement,
                raw=selected.raw,
            )
        if map_stores:
            selected = min(map_stores, key=lambda item: _distance_m(item.latitude, item.longitude, target))
            return Store(
                sap_code=selected.sap_code,
                address=selected.address,
                latitude=selected.latitude or latitude,
                longitude=selected.longitude or longitude,
                distance_m=_distance_m(selected.latitude, selected.longitude, target),
                work_start_time=selected.work_start_time,
                work_end_time=selected.work_end_time,
                is_24h=selected.is_24h,
                metro=selected.metro,
                settlement=selected.settlement,
                raw=selected.raw,
            )

        # Fallback для адресов, которые не попали в область карты. Этот
        # endpoint подтверждён APK и возвращает DeliveryStoreDto напрямую.
        payload = await self._get(ADDRESS_STORE_PATH, {"lat": latitude, "lon": longitude})
        candidates = []
        if isinstance(payload, dict):
            direct = _store(payload)
            if direct:
                candidates.append(direct)
            for key in ("selectedStore", "store", "stores", "items", "results", "data"):
                value = payload.get(key)
                values = value if isinstance(value, list) else [value]
                candidates.extend(item for item in (_store(x) for x in values if isinstance(x, dict)) if item)
        candidates = list({item.sap_code: item for item in candidates}.values())
        if not candidates:
            raise PyaterochkaAPIError(f"Магазин по адресу не найден: {address}")
        selected = min(candidates, key=lambda item: item.distance_m if item.distance_m is not None else float("inf"))
        return Store(
            sap_code=selected.sap_code,
            address=selected.address,
            latitude=latitude,
            longitude=longitude,
            distance_m=selected.distance_m,
            work_start_time=selected.work_start_time,
            work_end_time=selected.work_end_time,
            is_24h=selected.is_24h,
            metro=selected.metro,
            settlement=selected.settlement,
            raw=selected.raw,
        )

    async def categories(self, store: Store | str) -> list[dict[str, Any]]:
        sap_code = store.sap_code if isinstance(store, Store) else str(store)
        payload = await self._get(f"/catalog/v2/stores/{sap_code}/categories", {"mode": "store", "include_restrict": "true", "include_subcategories": "1"})
        return _flatten_categories(_items(payload, ("categories", "items", "data")))

    async def category_products(self, store: Store | str, category_id: str, *, limit: int = 100, offset: int = 0) -> list[Product]:
        sap_code = store.sap_code if isinstance(store, Store) else str(store)
        payload = await self._get(f"/catalog/v2/stores/{sap_code}/categories/{category_id}/products", {"mode": "store", "include_restrict": "true", "limit": limit, "offset": offset})
        return [_product(item, category_id) for item in _items(payload, ("products", "items", "data"))]

    async def search_page(
        self,
        store: Store | str,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        query_type: str | None = None,
        search_query_id: str | None = None,
    ) -> SearchResult:
        """Ищет товары и возвращает query metadata из ответа APK-модели.

        ``query_type`` и ``search_query_id`` необязательны для первого запроса,
        но позволяют MSP-инструменту продолжать серверную поисковую сессию.
        """
        sap_code = store.sap_code if isinstance(store, Store) else str(store)
        params: dict[str, Any] = {
            "q": query,
            "mode": "store",
            "offset": offset,
            "limit": limit,
            "include_restrict": "true",
        }
        if query_type:
            params["query_type"] = query_type
        if search_query_id:
            params["search_query_id"] = search_query_id
        payload = await self._get(f"/catalog/v3/stores/{sap_code}/search", params)
        if not isinstance(payload, dict):
            raise PyaterochkaAPIError("Поиск каталога имеет неожиданный формат")
        search_categories = []
        for item in _items(payload, ("categories",)):
            search_categories.append(
                {
                    "id": str(item.get("id", "")),
                    "name": str(item.get("name", "")),
                    "type": str(item.get("type", "")),
                    "parent_name": str(item.get("parent_name", "")),
                }
            )
        return SearchResult(
            products=[_product(item) for item in _items(payload, ("products", "items", "results", "data"))],
            categories=search_categories,
            query_type=payload.get("query_type"),
            search_query_id=payload.get("search_query_id"),
            raw=payload,
        )

    async def search(
        self,
        store: Store | str,
        query: str,
        *,
        limit: int = 100,
        offset: int = 0,
        query_type: str | None = None,
        search_query_id: str | None = None,
    ) -> list[Product]:
        """Совместимый короткий вариант поиска, возвращающий только товары."""
        result = await self.search_page(
            store,
            query,
            limit=limit,
            offset=offset,
            query_type=query_type,
            search_query_id=search_query_id,
        )
        return result.products

    async def product_info(self, store: Store | str, plu: str) -> Product:
        """Карточка товара: состав, калории (БЖУ) и подробное описание.

        Использует ``catalog/v2/stores/{store}/products/{plu}`` — тот же
        endpoint, что открывает карточку в приложении. Калорийность лежит в
        ``attributes`` («Энергетическая ценность»), состав — в ``ingredients``.
        """
        sap_code = store.sap_code if isinstance(store, Store) else str(store)
        payload = await self._get(
            f"/catalog/v2/stores/{sap_code}/products/{plu}",
            {"mode": "store", "include_restrict": "true"},
        )
        if not isinstance(payload, dict):
            raise PyaterochkaAPIError("Карточка товара имеет неожиданный формат")
        product = _product(payload)
        calories = protein = fat = carbs = None
        attrs = payload.get("attributes")
        if isinstance(attrs, list):
            for item in attrs:
                if not isinstance(item, dict):
                    continue
                name = _normal(item.get("name"))
                value = item.get("value")
                if name in ("энергетическая ценность", "калорийность", "ккал"):
                    calories = _number(value)
                elif name in ("белки", "белок"):
                    protein = _number(value)
                elif name in ("жиры", "жир"):
                    fat = _number(value)
                elif name in ("углеводы", "углевод"):
                    carbs = _number(value)
        ingredients = payload.get("ingredients")
        if isinstance(ingredients, str) and ingredients:
            product = replace(product, ingredients=ingredients)
        if calories is not None or protein is not None or fat is not None or carbs is not None:
            product = replace(product, calories=calories, protein=protein, fat=fat, carbs=carbs)
        return product

    async def products(self, store: Store | str, filters: ProductFilters | None = None) -> list[Product]:
        """Получает товары по категории или поиску и применяет единые фильтры."""
        if filters.query:
            products = await self.search(
                store,
                filters.query,
                limit=max(filters.offset + filters.limit, 100),
            )
        elif filters.category_id:
            products = await self.category_products(
                store,
                filters.category_id,
                limit=max(filters.offset + filters.limit, 100),
                offset=0,
            )
        else:
            categories = await self.categories(store)
            batches = await asyncio.gather(*(self.category_products(store, item["id"]) for item in categories if item["id"]))
            seen: set[str] = set()
            products = []
            for batch in batches:
                for product in batch:
                    if product.plu and product.plu not in seen:
                        seen.add(product.plu)
                        products.append(product)
        return filter_products(products, filters)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


async def _cli(args: argparse.Namespace) -> None:
    async with PyaterochkaStoreAPI(
        headless=args.headless,
        proxy=args.proxy,
        cookies_path=args.cookies,
    ) as api:
        store = Store(sap_code=args.sap_code, address=args.address) if args.sap_code else await api.resolve_store(args.address)
        if args.command == "resolve":
            print(_json(asdict(store)))
            return
        filters = ProductFilters(query=args.query, category_id=args.category_id, price_min=args.price_min, price_max=args.price_max, brand=args.brand, promo_only=args.promo_only, sort=args.sort, limit=args.limit, offset=args.offset)
        products = await api.products(store, filters)
        print(_json({"store": asdict(store), "products": [asdict(product) for product in products]}))


def main() -> None:
    parser = argparse.ArgumentParser(description="Поиск магазина Пятёрочки и товаров в нём")
    parser.add_argument("command", choices=("resolve", "products"))
    parser.add_argument("--address", required=True)
    parser.add_argument("--proxy", default=None)
    parser.add_argument(
        "--cookies",
        default=None,
        help="JSON-экспорт cookies браузера; также можно задать PYATEROCHKA_COOKIES_FILE",
    )
    parser.add_argument(
        "--sap-code", "--store-id", dest="sap_code",
        help="ID магазина (например, S105); позволяет пропустить геокодирование",
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--query")
    parser.add_argument("--category-id")
    parser.add_argument("--brand")
    parser.add_argument("--price-min", type=float)
    parser.add_argument("--price-max", type=float)
    parser.add_argument("--promo-only", action="store_true")
    parser.add_argument("--sort", choices=("popularity", "price_asc", "price_desc", "discount_desc"), default="popularity")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    try:
        asyncio.run(_cli(parser.parse_args()))
    except (PyaterochkaAPIError, aiohttp.ClientError, TimeoutError) as exc:
        parser.exit(2, f"Ошибка API Пятёрочки: {exc}\n")


if __name__ == "__main__":
    main()
