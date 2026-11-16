"""Build a complete, paginated snapshot of one Pyaterochka store catalogue."""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from pyaterochka_store_api import PyaterochkaStoreAPI

ADDRESS = os.getenv("PYATEROCHKA_STORE_ADDRESS", "Биробиджан, ул. Советская, д. 68 к. 1")
PAGE_SIZE = int(os.getenv("CATALOG_PAGE_SIZE", "100"))
MAX_PAGES_PER_CATEGORY = int(os.getenv("CATALOG_MAX_PAGES", "100"))


def normalize(value: str) -> list[str]:
    return re.sub(r"[^0-9а-яё]+", " ", value.casefold()).split()


def product_dict(product):
    return {
        "plu": product.plu, "name": product.name, "price": product.price,
        "old_price": product.old_price, "unit": product.unit, "size": product.size,
        "brand": product.brand, "category": product.category, "image": product.image,
        "images": list(product.images), "is_promo": product.is_promo,
        "promo_label": product.promo_label, "units_to_activate": product.units_to_activate,
        "cpd_price": product.cpd_price, "rating": product.rating,
        "rating_count": product.rating_count, "is_available": product.is_available,
        "stock_limit": product.stock_limit, "url": product.url,
    }


async def main() -> None:
    async with PyaterochkaStoreAPI(timeout_ms=60000) as api:
        store = await api.resolve_store(ADDRESS)
        address_tokens = normalize(store.address)
        if "советская" not in address_tokens or "68" not in address_tokens or "1" not in address_tokens:
            raise RuntimeError(
                f"Exact store address was not confirmed. Requested: {ADDRESS}; got: {store.address}"
            )

        categories = await api.categories(store)
        if not categories:
            raise RuntimeError("The store API returned no categories; refusing to publish an empty snapshot.")

        all_products: dict[str, dict] = {}
        category_results = []
        for category in categories:
            category_id = str(category.get("id") or "")
            if not category_id:
                continue
            offset = 0
            count = 0
            for _ in range(MAX_PAGES_PER_CATEGORY):
                batch = await api.category_products(
                    store, category_id, limit=PAGE_SIZE, offset=offset
                )
                if not batch:
                    break
                for product in batch:
                    if product.plu:
                        all_products[product.plu] = product_dict(product)
                count += len(batch)
                offset += len(batch)
                if len(batch) < PAGE_SIZE:
                    break
            else:
                raise RuntimeError(f"Pagination limit reached for category {category_id}; snapshot incomplete.")
            category_results.append({**category, "product_count": count})

        if not all_products:
            raise RuntimeError("The API returned no products; refusing to publish an empty snapshot.")

        payload = {
            "source": "5ka.ru",
            "store": {
                "store_id": store.store_id, "sap_code": store.sap_code,
                "address": store.address, "latitude": store.latitude,
                "longitude": store.longitude, "work_start_time": store.work_start_time,
                "work_end_time": store.work_end_time, "is_24h": store.is_24h,
            },
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "category_count": len(category_results),
            "product_count": len(all_products),
            "categories": category_results,
            "products": list(all_products.values()),
        }
        output = Path("data/catalog.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"Saved {len(category_results)} categories and {len(all_products)} unique products to {output}")


if __name__ == "__main__":
    asyncio.run(main())
