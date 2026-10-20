"""Deterministic input generator for the nested loop workload."""

from __future__ import annotations

import random


def make_input(n_customers: int, n_orders: int, orphan_ratio: float = 0.05, seed: int = 0) -> dict:
    """Return keyword arguments for ``customer_totals``.

    Customers have unique ids; order amounts are floats with 2 decimals. About ``orphan_ratio``
    of the orders reference a customer id that does not exist, and some customers have no orders.
    """
    rng = random.Random(seed)
    customers = [{"id": 1000 + i, "name": f"customer-{i:05d}"} for i in range(n_customers)]
    orders = []
    for i in range(n_orders):
        if not customers or rng.random() < orphan_ratio:
            customer_id = -1 - i
        else:
            customer_id = rng.choice(customers)["id"]
        orders.append(
            {"id": i, "customer_id": customer_id, "amount": round(rng.uniform(1, 500), 2)}
        )
    return {"customers": customers, "orders": orders}
