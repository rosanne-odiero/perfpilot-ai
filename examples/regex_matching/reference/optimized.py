"""REFERENCE IMPLEMENTATION (bundled, for tests).

Hand-written by the PerfPilot maintainers to exercise the benchmark engine. It was NOT produced
by the platform's optimizer. The benchmark labels any run against this file as
"reference implementation (bundled, for testing)".

Change: compile the pattern once at import time and call the bound ``findall`` method, skipping
the ``re`` module's per-call pattern-cache lookup. Because ``re`` already caches compiled
patterns, the expected gain is modest; the benchmark decides whether it is meaningful.
Trade-off: none semantically (same pattern, same flags).
"""

from __future__ import annotations

import re

_ERROR_CODE = re.compile(r"\bERR-(\d{4})\b")


def extract_error_codes(lines: list[str]) -> list[str]:
    findall = _ERROR_CODE.findall
    codes: list[str] = []
    for line in lines:
        codes.extend(findall(line))
    return codes
