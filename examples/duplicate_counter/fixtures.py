"""Deterministic input generator for the duplicate counter workload."""

from __future__ import annotations

import random
import string


def _word(rng: random.Random) -> str:
    return "".join(rng.choices(string.ascii_lowercase, k=rng.randint(4, 10)))


def make_input(n_items: int, unique_ratio: float = 0.3, skew: float = 1.0, seed: int = 0) -> list:
    """Return the ``items`` list for ``count_duplicates``.

    About ``unique_ratio * n_items`` distinct words are drawn; item frequencies follow a Zipf-like
    law with exponent ``skew`` (0 = uniform), so some words repeat a lot and many appear once.
    """
    rng = random.Random(seed)
    vocabulary_size = max(1, round(n_items * unique_ratio))
    vocabulary: list[str] = []
    seen: set[str] = set()
    while len(vocabulary) < vocabulary_size:
        word = _word(rng)
        if word not in seen:
            seen.add(word)
            vocabulary.append(word)
    weights = [1.0 / (rank + 1) ** skew for rank in range(vocabulary_size)]
    return rng.choices(vocabulary, weights=weights, k=n_items)
