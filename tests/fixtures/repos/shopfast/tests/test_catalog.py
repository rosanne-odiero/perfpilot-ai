from __future__ import annotations

from shopfast import catalog


def test_build_catalog_is_deterministic() -> None:
    assert catalog.build_catalog(seed=1, size=5) == catalog.build_catalog(seed=1, size=5)


def test_filter_discontinued(products: list[catalog.Product]) -> None:
    dropped = [products[0].sku, products[3].sku]
    kept = catalog.filter_discontinued(products, dropped)
    assert len(kept) == len(products) - 2
    assert all(p.sku not in dropped for p in kept)


def test_unique_categories_keeps_first_seen_order(products: list[catalog.Product]) -> None:
    categories = catalog.unique_categories(products)
    assert len(categories) == len(set(categories))
    assert categories[0] == products[0].category


def test_top_rated(products: list[catalog.Product]) -> None:
    ratings = {p.sku: float(i % 7) for i, p in enumerate(products)}
    best = catalog.top_rated(products, ratings, limit=3)
    assert len(best) == 3
    assert [ratings[p.sku] for p in best] == [6.0, 6.0, 6.0]


def test_products_by_tag(products: list[catalog.Product]) -> None:
    index = catalog.products_by_tag(products)
    for tag, skus in index.items():
        assert all(tag in next(p for p in products if p.sku == sku).tags for sku in skus)
