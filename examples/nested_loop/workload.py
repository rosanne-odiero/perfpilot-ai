"""Nested loop workload (deliberately inefficient, but correct).

``customer_totals`` returns ``(customer name, total order amount)`` for every customer, in
customer order. For each customer it scans every order: O(len(customers) * len(orders)).
"""

from __future__ import annotations


def customer_totals(customers: list[dict], orders: list[dict]) -> list[tuple[str, float]]:
    totals: list[tuple[str, float]] = []
    for customer in customers:
        total = 0.0
        for order in orders:
            if order["customer_id"] == customer["id"]:
                total += order["amount"]
        totals.append((customer["name"], total))
    return totals
