"""Prices, taxes and discount rules."""

from __future__ import annotations

import json
from dataclasses import dataclass

from shopfast.catalog import Product

TAX_RATES = {"us": 0.07, "eu": 0.2, "uk": 0.2, "ke": 0.16}
RATES_JSON = '{"base": "USD", "rates": {"EUR": 0.92, "GBP": 0.79, "KES": 129.5}, "symbols": ["EUR", "GBP", "KES"]}'

_price_cache = {}


@dataclass(frozen=True)
class DiscountRule:
    name: str
    category: str
    percent: int
    priority: int

    def applies_to(self, product: Product) -> bool:
        return product.category == self.category

    def apply(self, price_cents: int) -> int:
        return price_cents * (100 - self.percent) // 100


def price_with_tax(sku: str, base_cents: int, region: str) -> int:
    key = (sku, base_cents, region)
    if key in _price_cache:
        return _price_cache[key]
    value = round(base_cents * (1 + TAX_RATES.get(region, 0.0)))
    _price_cache[key] = value
    return value


def apply_discounts(products: list[Product], rules: list[DiscountRule]) -> dict[str, int]:
    prices = {}
    for product in products:
        ordered_rules = sorted(rules, key=lambda r: r.priority)
        price = product.price_cents
        for rule in ordered_rules:
            if rule.applies_to(product):
                price = rule.apply(price)
        prices[product.sku] = price
    return prices


def regional_price_list(products: list[Product], regions: list[str]) -> dict[str, dict[str, int]]:
    return {
        region: {p.sku: price_with_tax(p.sku, p.price_cents, region) for p in products}
        for region in regions
    }


def region_summary(prices_by_region: dict[str, dict[str, int]]) -> list[str]:
    lines = []
    for region in sorted(prices_by_region):
        skus = list(prices_by_region[region].keys())
        lines.append(f"{region}: {len(skus)} skus")
    return lines


def currency_table() -> dict[str, float]:
    rates = json.loads(RATES_JSON)["rates"]
    symbols = json.loads(RATES_JSON)["symbols"]
    return {symbol: rates[symbol] for symbol in symbols}
