from __future__ import annotations

from shopfast import pricing
from shopfast.catalog import Product


def _product(category: str, price: int = 1000) -> Product:
    return Product(sku=f"SKU-{category}", name=category, category=category, price_cents=price, tags=())


def test_price_with_tax_is_cached() -> None:
    assert pricing.price_with_tax("SKU-1", 1000, "eu") == 1200
    assert pricing.price_with_tax("SKU-1", 1000, "eu") == 1200
    assert pricing.price_with_tax("SKU-1", 1000, "mars") == 1000


def test_apply_discounts_in_priority_order() -> None:
    rules = [
        pricing.DiscountRule("half", "makeup", 50, priority=2),
        pricing.DiscountRule("tenth", "makeup", 10, priority=1),
    ]
    prices = pricing.apply_discounts([_product("makeup"), _product("skincare")], rules)
    assert prices == {"SKU-makeup": 450, "SKU-skincare": 1000}


def test_region_summary() -> None:
    prices = pricing.regional_price_list([_product("a"), _product("b")], ["us", "eu"])
    assert pricing.region_summary(prices) == ["eu: 2 skus", "us: 2 skus"]


def test_currency_table() -> None:
    assert pricing.currency_table() == {"EUR": 0.92, "GBP": 0.79, "KES": 129.5}
