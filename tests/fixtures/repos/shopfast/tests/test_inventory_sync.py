from __future__ import annotations

from shopfast import inventory_sync


def test_reconcile_variants_agree(products) -> None:
    rows = [{"sku": products[i].sku, "quantity": i} for i in (5, 1, 9)]
    rows.append({"sku": "SKU-UNKNOWN", "quantity": 3})
    expected = [(products[5].sku, 5), (products[1].sku, 1), (products[9].sku, 9)]
    assert inventory_sync.reconcile(rows, products) == expected
    assert inventory_sync.reconcile_indexed(rows, products) == expected


def test_restock_order_deduplicates() -> None:
    assert inventory_sync.restock_order(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


def test_duplicate_skus() -> None:
    assert inventory_sync.duplicate_skus(["a", "b", "a", "c", "b", "a"]) == ["a", "b"]


def test_low_stock() -> None:
    assert inventory_sync.low_stock({"a": 1, "b": 10, "c": 0}, threshold=2) == ["a", "c"]
