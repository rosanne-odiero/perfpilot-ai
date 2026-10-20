"""SQLite storage for customers and orders."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    tier TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    total_cents INTEGER NOT NULL
);
"""


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    return conn


def seed(
    conn: sqlite3.Connection,
    customers: Iterable[tuple[int, str, str]],
    orders: Iterable[tuple[int, int, int]],
) -> None:
    conn.executemany("INSERT INTO customers (id, name, tier) VALUES (?, ?, ?)", customers)
    conn.executemany("INSERT INTO orders (id, customer_id, total_cents) VALUES (?, ?, ?)", orders)
    conn.commit()
