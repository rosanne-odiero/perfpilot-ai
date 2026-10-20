"""REFERENCE IMPLEMENTATION (bundled, for tests).

Hand-written by the PerfPilot maintainers to exercise the benchmark engine. It was NOT produced
by the platform's optimizer. The benchmark labels any run against this file as
"reference implementation (bundled, for testing)".

Change: build a list of lines and ``"".join`` them once, the textbook fix for ``+=`` in a loop.
On CPython the in-place ``+=`` optimisation can make the original nearly as fast, so the gain
may be far below the textbook promise; the benchmark measures it instead of assuming it.
Trade-off: holds every line in a temporary list (extra memory) before joining.
"""

from __future__ import annotations


def render_report(rows: list[tuple[str, int, float]]) -> str:
    return "".join([f"{name:<12}{quantity:>6}{price:>12.2f}\n" for name, quantity, price in rows])
