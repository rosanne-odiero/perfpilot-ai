"""Deterministic input generator for the regex matching workload."""

from __future__ import annotations

import random

_WORDS = ["request", "handled", "user", "timeout", "cache", "db", "retry", "ok", "upstream", "id"]


def make_input(n_lines: int, error_ratio: float = 0.2, seed: int = 0) -> list:
    """Return the ``lines`` list for ``extract_error_codes``.

    Each line has 4-12 filler words. With probability ``error_ratio`` it contains one or two
    ``ERR-dddd`` tokens; some lines contain decoys such as ``ERR-12`` or ``XERR-1234``.
    """
    rng = random.Random(seed)
    lines = []
    for index in range(n_lines):
        words = rng.choices(_WORDS, k=rng.randint(4, 12))
        if rng.random() < error_ratio:
            for _ in range(rng.randint(1, 2)):
                words.insert(rng.randrange(len(words) + 1), f"ERR-{rng.randint(0, 9999):04d}")
        elif rng.random() < 0.1:
            words.insert(rng.randrange(len(words) + 1), rng.choice(["ERR-12", "XERR-1234"]))
        lines.append(f"2024-01-01T00:00:{index % 60:02d} " + " ".join(words))
    return lines
