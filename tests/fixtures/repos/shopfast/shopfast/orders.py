"""Order queries and order history snapshots."""

from __future__ import annotations

import copy
import sqlite3


def order_summaries(conn: sqlite3.Connection, order_ids: list[int]) -> list[dict[str, object]]:
    cursor = conn.cursor()
    summaries: list[dict[str, object]] = []
    for order_id in order_ids:
        cursor.execute(
            "SELECT id, customer_id, total_cents FROM orders WHERE id = ?", (order_id,)
        )
        row = cursor.fetchone()
        if row is None:
            continue
        customer = conn.execute(
            "SELECT name, tier FROM customers WHERE id = ?", (row[1],)
        ).fetchone()
        summaries.append(
            {"id": row[0], "customer": customer[0], "tier": customer[1], "total_cents": row[2]}
        )
    return summaries


def order_summaries_batched(
    conn: sqlite3.Connection, order_ids: list[int]
) -> list[dict[str, object]]:
    if not order_ids:
        return []
    placeholders = ",".join("?" for _ in order_ids)
    rows = conn.execute(
        "SELECT o.id, c.name, c.tier, o.total_cents FROM orders o "
        f"JOIN customers c ON c.id = o.customer_id WHERE o.id IN ({placeholders})",
        order_ids,
    ).fetchall()
    by_id = {row[0]: row for row in rows}
    return [
        {"id": i, "customer": by_id[i][1], "tier": by_id[i][2], "total_cents": by_id[i][3]}
        for i in order_ids
        if i in by_id
    ]


def history_snapshots(cart: dict[str, int], updates: list[tuple[str, int]]) -> list[dict[str, int]]:
    history = []
    state = dict(cart)
    for sku, quantity in updates:
        state[sku] = state.get(sku, 0) + quantity
        history.append(copy.deepcopy(state))
    return history
