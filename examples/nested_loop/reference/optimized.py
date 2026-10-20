"""REFERENCE IMPLEMENTATION (bundled, for tests).

Hand-written by the PerfPilot maintainers to exercise the benchmark engine. It was NOT produced
by the platform's optimizer. The benchmark labels any run against this file as
"reference implementation (bundled, for testing)".

Change: one pass over the orders builds a ``customer_id -> total`` index, then one pass over the
customers reads it: O(len(customers) + len(orders)) instead of the product.
Trade-offs: customer ids must be hashable; per-customer amounts are still added in order
sequence, so float sums match, but the contract allows a tiny tolerance in case a future change
reorders additions.
"""

from __future__ import annotations


def customer_totals(customers: list[dict], orders: list[dict]) -> list[tuple[str, float]]:
    wanted = {customer["id"] for customer in customers}
    totals_by_id: dict[object, float] = {}
    for order in orders:
        customer_id = order["customer_id"]
        if customer_id in wanted:
            totals_by_id[customer_id] = totals_by_id.get(customer_id, 0.0) + order["amount"]
    return [(customer["name"], totals_by_id.get(customer["id"], 0.0)) for customer in customers]
