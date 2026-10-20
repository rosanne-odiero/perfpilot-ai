"""Deterministic input generator for the list lookup workload."""

from __future__ import annotations

import random


def make_input(n_known: int, n_queries: int, hit_ratio: float = 0.5, seed: int = 0) -> dict:
    """Return keyword arguments for ``find_known``.

    ``known_ids`` are unique ints drawn from ``[0, 4 * n_known)``. Each query is a known id with
    probability ``hit_ratio`` and otherwise an id that is guaranteed to be missing.
    """
    rng = random.Random(seed)
    universe = 4 * max(n_known, 1)
    known_ids = rng.sample(range(universe), n_known)
    known_set = set(known_ids)
    missing = [value for value in range(universe) if value not in known_set]
    queries = [
        rng.choice(known_ids) if known_ids and rng.random() < hit_ratio else rng.choice(missing)
        for _ in range(n_queries)
    ]
    return {"known_ids": known_ids, "queries": queries}
