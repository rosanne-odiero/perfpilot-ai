"""REFERENCE IMPLEMENTATION (bundled, for tests).

Hand-written by the PerfPilot maintainers to exercise the benchmark engine. It was NOT produced
by the platform's optimizer. The benchmark labels any run against this file as
"reference implementation (bundled, for testing)".

Change: build a ``set`` once so each membership test is an average O(1) hash lookup.
Trade-off: requires hashable ids and costs O(len(known_ids)) extra memory.
"""

from __future__ import annotations


def find_known(known_ids: list[int], queries: list[int]) -> list[int]:
    known = set(known_ids)
    return [query for query in queries if query in known]
