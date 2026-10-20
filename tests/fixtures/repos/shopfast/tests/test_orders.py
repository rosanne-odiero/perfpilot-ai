from __future__ import annotations

from shopfast import orders


def test_order_summaries_match_batched(conn) -> None:
    ids = [12, 10, 99, 11]
    expected = [
        {"id": 12, "customer": "Ada", "tier": "gold", "total_cents": 4000},
        {"id": 10, "customer": "Ada", "tier": "gold", "total_cents": 1500},
        {"id": 11, "customer": "Grace", "tier": "basic", "total_cents": 990},
    ]
    assert orders.order_summaries(conn, ids) == expected
    assert orders.order_summaries_batched(conn, ids) == expected


def test_history_snapshots_are_independent() -> None:
    history = orders.history_snapshots({"a": 1}, [("a", 1), ("b", 2)])
    assert history == [{"a": 2}, {"a": 2, "b": 2}]
    history[0]["a"] = 100
    assert history[1]["a"] == 2
