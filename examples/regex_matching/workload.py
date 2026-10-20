"""Regex matching workload.

``extract_error_codes`` pulls the 4-digit code out of every ``ERR-1234`` token in a list of log
lines. The pattern string is passed to ``re.findall`` on every iteration instead of being
compiled once. ``re`` keeps an internal cache of compiled patterns, so the per-call cost is a
cache lookup rather than a full compile; whether hoisting ``re.compile`` matters must be measured.
"""

from __future__ import annotations

import re


def extract_error_codes(lines: list[str]) -> list[str]:
    codes: list[str] = []
    for line in lines:
        codes.extend(re.findall(r"\bERR-(\d{4})\b", line))
    return codes
