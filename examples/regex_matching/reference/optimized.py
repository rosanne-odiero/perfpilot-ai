"""REFERENCE IMPLEMENTATION (bundled, for tests).

Hand-written by the PerfPilot maintainers to exercise the benchmark engine. It was NOT produced
by the platform's optimizer. The benchmark labels any run against this file as
"reference implementation (bundled, for testing)".

Change: the literal fix for "regex compiled inside a loop" - compile the pattern once at module
level - while keeping the ``re.findall(PATTERN, line)`` call shape, as many real refactors do.
Because ``re`` already caches compiled patterns by their source string, this removes no
compilation work; whether it helps, does nothing, or even hurts must be measured.
Trade-off: none semantically (same pattern, same flags).
"""

from __future__ import annotations

import re

ERROR_CODE = re.compile(r"\bERR-(\d{4})\b")


def extract_error_codes(lines: list[str]) -> list[str]:
    codes: list[str] = []
    for line in lines:
        codes.extend(re.findall(ERROR_CODE, line))
    return codes
