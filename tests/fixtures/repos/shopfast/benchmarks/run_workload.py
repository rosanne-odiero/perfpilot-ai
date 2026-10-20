"""Deterministic, seeded workload over ShopFast's hot paths (no network access).

Usage: python benchmarks/run_workload.py [--scale 1.0] [--seed 1234]
Prints a JSON checksum of the results and the elapsed wall time.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shopfast import api, catalog, db, inventory_sync, orders, pricing, reporting, utils  # noqa: E402


def run(seed: int, scale: float) -> dict[str, object]:
    rng = random.Random(seed)
    size = int(6000 * scale)
    products = catalog.build_catalog(seed, size)
    skus = [p.sku for p in products]

    discontinued = rng.sample(skus, k=size // 3)
    kept = catalog.filter_discontinued(products, discontinued)
    categories = catalog.unique_categories(products)
    ratings = {sku: rng.random() for sku in skus}
    best = catalog.top_rated(products, ratings, limit=10)

    rules = [
        pricing.DiscountRule(f"r{i}", rng.choice(catalog.CATEGORIES), rng.randint(1, 30), i)
        for i in range(25)
    ]
    discounted = pricing.apply_discounts(products, rules)
    regional = pricing.regional_price_list(products, ["us", "eu", "ke"])

    rows = [{"sku": rng.choice(skus), "quantity": rng.randint(0, 50)} for _ in range(size)]
    matched = inventory_sync.reconcile(rows, products)
    restock = inventory_sync.restock_order([rng.choice(skus) for _ in range(size)])
    dupes = inventory_sync.duplicate_skus([rng.choice(skus[:400]) for _ in range(size // 2)])

    conn = db.connect()
    customers = [(i, f"Customer {i}", rng.choice(["gold", "basic"])) for i in range(1, 201)]
    order_rows = [(i, rng.randint(1, 200), rng.randint(500, 90_000)) for i in range(1, 3001)]
    db.seed(conn, customers, order_rows)
    summaries = orders.order_summaries(conn, list(range(1, int(3000 * scale) + 1)))
    report = reporting.render_sales_report(summaries)
    log_lines = [f"order {s['id']} SKU-{rng.randint(0, 999_999):06d} by user{s['id']}@example.com" for s in summaries]
    extracted = reporting.extract_skus(log_lines)
    redacted = reporting.redact_emails(log_lines)

    fetched = [asyncio.run(api.get_product(products, rng.choice(skus))) for _ in range(20)]
    bundles = utils.bundle_combinations(28)

    digest = hashlib.sha256()
    for part in (kept, categories, best, discounted, regional, matched, restock, dupes, report, extracted, redacted, fetched, bundles):
        digest.update(repr(part).encode())
    return {
        "seed": seed,
        "scale": scale,
        "products": size,
        "matched_rows": len(matched),
        "orders": len(summaries),
        "checksum": digest.hexdigest()[:16],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--scale", type=float, default=1.0)
    args = parser.parse_args()
    started = time.perf_counter()
    result = run(args.seed, args.scale)
    result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
