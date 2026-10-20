"""Duplicate counter workload (deliberately inefficient, but correct).

``count_duplicates`` maps every item that occurs more than once to its number of occurrences.
It calls ``list.count`` for every element, which rescans the whole list each time: O(n^2).
"""

from __future__ import annotations


def count_duplicates(items: list[str]) -> dict[str, int]:
    duplicates: dict[str, int] = {}
    for item in items:
        occurrences = items.count(item)
        if occurrences > 1:
            duplicates[item] = occurrences
    return duplicates
