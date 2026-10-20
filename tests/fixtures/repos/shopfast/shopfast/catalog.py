"""Product catalog: generation, filtering and ranking."""

from __future__ import annotations

import random
from dataclasses import dataclass

CATEGORIES = ("skincare", "haircare", "fragrance", "makeup", "wellness", "accessories")
TAGS = ("vegan", "organic", "travel", "bestseller", "new", "gift", "refill", "sensitive")


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    category: str
    price_cents: int
    tags: tuple[str, ...]


def build_catalog(seed: int, size: int) -> list[Product]:
    rng = random.Random(seed)
    products = []
    for index in range(size):
        tags = tuple(sorted(rng.sample(TAGS, k=rng.randint(0, 3))))
        products.append(
            Product(
                sku=f"SKU-{index:06d}",
                name=f"Product {index}",
                category=rng.choice(CATEGORIES),
                price_cents=rng.randint(300, 25_000),
                tags=tags,
            )
        )
    return products


def filter_discontinued(products: list[Product], discontinued_skus: list[str]) -> list[Product]:
    kept = []
    for product in products:
        if product.sku not in discontinued_skus:
            kept.append(product)
    return kept


def unique_categories(products: list[Product]) -> list[str]:
    categories: list[str] = []
    for product in products:
        if product.category not in categories:
            categories.append(product.category)
    return categories


def products_by_tag(products: list[Product]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for product in products:
        for tag in sorted(product.tags):
            index.setdefault(tag, []).append(product.sku)
    return index


def top_rated(products: list[Product], ratings: dict[str, float], limit: int) -> list[Product]:
    best: list[Product] = []
    for product in products:
        best.append(product)
        best.sort(key=lambda p: ratings.get(p.sku, 0.0), reverse=True)
        del best[limit:]
    return best
