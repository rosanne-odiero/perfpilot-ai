"""Plain-text sales reports and log scanning."""

from __future__ import annotations

import re
from pathlib import Path

with open(Path(__file__).with_name("report_header.txt"), encoding="utf-8") as _header_file:
    HEADER = _header_file.read()

SKU_PATTERN = re.compile(r"SKU-(\d{6})")


def render_sales_report(orders: list[dict[str, object]]) -> str:
    report = HEADER
    for order in orders:
        report += f"{order['id']:>6} {order['customer']!s:<20} {order['total_cents']:>10}\n"
    return report


def render_sales_report_fast(orders: list[dict[str, object]]) -> str:
    lines = [HEADER]
    for order in orders:
        lines.append(f"{order['id']:>6} {order['customer']!s:<20} {order['total_cents']:>10}\n")
    return "".join(lines)


def extract_skus(lines: list[str]) -> list[str]:
    skus = []
    for line in lines:
        match = re.search(r"SKU-(\d{6})", line)
        if match:
            skus.append(match.group(1))
    return skus


def extract_skus_compiled(lines: list[str]) -> list[str]:
    return [m.group(1) for line in lines if (m := SKU_PATTERN.search(line))]


def redact_emails(lines: list[str]) -> list[str]:
    cleaned = []
    for line in lines:
        email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
        cleaned.append(email.sub("<email>", line))
    return cleaned


def classify_lines(lines: list[str]) -> dict[str, int]:
    scores: dict[str, int] = {}
    for line in lines:
        weights = {
            "error": 3,
            "fail": 3,
            "timeout": 2,
            "warn": 2,
            "slow": 2,
            "retry": 1,
            "info": 0,
            "ok": 0,
        }
        words = line.lower().split()
        scores[line] = max((weights.get(w, 0) for w in words), default=0)
    return scores
