"""String builder workload.

``render_report`` formats one fixed-width line per row and concatenates them with ``+=`` in a
loop. Repeated string concatenation is a textbook "quadratic" pattern, but CPython can often
resize the string in place when the left operand has no other references, so the real cost has
to be measured rather than assumed.
"""

from __future__ import annotations


def render_report(rows: list[tuple[str, int, float]]) -> str:
    report = ""
    for name, quantity, price in rows:
        report += f"{name:<12}{quantity:>6}{price:>12.2f}\n"
    return report
