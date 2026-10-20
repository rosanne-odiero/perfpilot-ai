from __future__ import annotations

from shopfast import utils


def test_bundle_combinations_agree() -> None:
    assert [utils.bundle_combinations(n) for n in range(8)] == [1, 1, 2, 3, 5, 8, 13, 21]
    assert utils.bundle_combinations(20) == utils.bundle_combinations_cached(20) == 10946


def test_category_depth() -> None:
    assert utils.category_depth({"a": {"b": {"c": {}}}, "d": 1}) == 4
