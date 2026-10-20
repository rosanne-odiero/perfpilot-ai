"""REFERENCE IMPLEMENTATION (bundled, for tests).

Hand-written by the PerfPilot maintainers to exercise the benchmark engine. It was NOT produced
by the platform's optimizer. The benchmark labels any run against this file as
"reference implementation (bundled, for testing)".

Change: count every item in one pass with ``collections.Counter`` (O(n)) instead of calling
``list.count`` per element (O(n^2)). Key insertion order (first occurrence) is preserved.
Trade-off: items must be hashable.
"""

from __future__ import annotations

from collections import Counter


def count_duplicates(items: list[str]) -> dict[str, int]:
    return {item: count for item, count in Counter(items).items() if count > 1}
