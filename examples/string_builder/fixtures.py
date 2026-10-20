"""Deterministic input generator for the string builder workload."""

from __future__ import annotations

import random
import string


def make_input(n_rows: int, max_name_length: int = 12, seed: int = 0) -> list:
    """Return the ``rows`` list for ``render_report``: ``(name, quantity, price)`` tuples."""
    rng = random.Random(seed)
    rows = []
    for _ in range(n_rows):
        name = "".join(rng.choices(string.ascii_letters, k=rng.randint(1, max_name_length)))
        rows.append((name, rng.randint(0, 9999), round(rng.uniform(0.0, 10_000.0), 4)))
    return rows
