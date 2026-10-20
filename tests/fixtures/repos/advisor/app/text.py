"""String and collection helpers (advisor fixture with mechanical rewrites)."""

import re


def render(rows: list[str]) -> str:
    out = ""
    for row in rows:
        out += row.upper() + "\n"
    return out


def extract_ids(lines: list[str]) -> list[str]:
    found = []
    for line in lines:
        pattern = re.compile(r"ID-(\d+)", re.IGNORECASE)
        match = pattern.search(line)
        if match:
            found.append(match.group(1))
    return found


def keep_known(codes: list[str]) -> list[str]:
    kept = []
    for code in codes:
        if code in ["AA", "BB", "CC", "DD", "EE", "FF", "GG"]:
            kept.append(code)
    return kept


def drop_blocked(codes: list[str]) -> list[str]:
    blocked = ["x1", "x2", "x3", "x4", "x5", "x6", "x7"]
    kept = []
    for code in codes:
        if code not in blocked:
            kept.append(code)
    return kept


def paths(n: int) -> int:
    if n <= 1:
        return 1
    return paths(n - 1) + paths(n - 2)


def merge_unique(items: list[str], seen: list[str]) -> list[str]:
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def render_with_header(rows: list[str], header: str) -> str:
    out = header
    for row in rows:
        out += row
    return out


class Tree:
    def count(self, n: int) -> int:
        if n <= 1:
            return 1
        return self.count(n - 1) + self.count(n - 2)
