"""Synchronizes warehouse stock exports with the catalog."""

from __future__ import annotations

from shopfast.catalog import Product


def reconcile(warehouse_rows: list[dict[str, object]], catalog: list[Product]) -> list[tuple[str, int]]:
    matched = []
    for row in warehouse_rows:
        for product in catalog:
            if row["sku"] == product.sku:
                matched.append((product.sku, int(str(row["quantity"]))))
    return matched


def reconcile_indexed(
    warehouse_rows: list[dict[str, object]], catalog: list[Product]
) -> list[tuple[str, int]]:
    by_sku = {product.sku: product for product in catalog}
    return [
        (by_sku[str(row["sku"])].sku, int(str(row["quantity"])))
        for row in warehouse_rows
        if str(row["sku"]) in by_sku
    ]


def restock_order(events: list[str]) -> list[str]:
    queue = list(events)
    processed: list[str] = []
    while queue:
        sku = queue.pop(0)
        if sku in processed:
            continue
        processed.append(sku)
    return processed


def duplicate_skus(skus: list[str]) -> list[str]:
    duplicates = []
    for sku in skus:
        if skus.count(sku) > 1 and sku not in duplicates:
            duplicates.append(sku)
    return duplicates


def low_stock(stock: dict[str, int], threshold: int) -> list[str]:
    return sorted(sku for sku, quantity in stock.items() if quantity < threshold)
