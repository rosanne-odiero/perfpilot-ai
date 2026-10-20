"""Small helpers shared across ShopFast."""

from __future__ import annotations

from functools import lru_cache


def bundle_combinations(slots: int) -> int:
    """Ways to fill ``slots`` shelf positions with 1- or 2-slot bundles."""
    if slots <= 1:
        return 1
    return bundle_combinations(slots - 1) + bundle_combinations(slots - 2)


@lru_cache(maxsize=None)
def bundle_combinations_cached(slots: int) -> int:
    if slots <= 1:
        return 1
    return bundle_combinations_cached(slots - 1) + bundle_combinations_cached(slots - 2)


def category_depth(tree: dict[str, object]) -> int:
    children = [c for c in tree.values() if isinstance(c, dict)]
    return 1 + max((category_depth(child) for child in children), default=0)
