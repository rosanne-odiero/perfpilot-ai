"""List lookup workload (deliberately inefficient, but correct).

``find_known`` returns every query id that also appears in ``known_ids``, preserving query
order and repeats. Membership is tested against a *list*, so each lookup is a linear scan and
the whole call is O(len(queries) * len(known_ids)).
"""

from __future__ import annotations


def find_known(known_ids: list[int], queries: list[int]) -> list[int]:
    found: list[int] = []
    for query in queries:
        if query in known_ids:
            found.append(query)
    return found
