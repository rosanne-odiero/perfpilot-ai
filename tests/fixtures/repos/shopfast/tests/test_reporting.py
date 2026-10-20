from __future__ import annotations

from shopfast import reporting

ORDERS = [
    {"id": 1, "customer": "Ada", "total_cents": 1500},
    {"id": 2, "customer": "Grace", "total_cents": 990},
]


def test_reports_are_identical() -> None:
    slow = reporting.render_sales_report(ORDERS)
    assert slow == reporting.render_sales_report_fast(ORDERS)
    assert slow.startswith("ShopFast sales report")
    assert slow.count("\n") == 4


def test_extract_skus() -> None:
    lines = ["shipped SKU-000123 and SKU-000999", "nothing here", "SKU-424242 restocked"]
    assert reporting.extract_skus(lines) == ["000123", "424242"]
    assert reporting.extract_skus_compiled(lines) == ["000123", "424242"]


def test_redact_emails() -> None:
    assert reporting.redact_emails(["mail ada@example.com now"]) == ["mail <email> now"]


def test_classify_lines() -> None:
    scores = reporting.classify_lines(["request ok", "upstream timeout", "disk error"])
    assert scores == {"request ok": 0, "upstream timeout": 2, "disk error": 3}
