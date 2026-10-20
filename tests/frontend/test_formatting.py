from __future__ import annotations

import pytest

from frontend import formatting as fmt


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "—"),
        (512, "512 B"),
        (2048, "2.0 KB"),
        (5 * 1024**2, "5.0 MB"),
        (3 * 1024**4, "3072.0 GB"),
    ],
)
def test_human_bytes(value: int | None, expected: str) -> None:
    assert fmt.human_bytes(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "—"), (0.0217, "21.7 µs"), (1.3754, "1.38 ms"), (164.2, "164 ms"), (2500.0, "2.50 s")],
)
def test_fmt_ms(value: float | None, expected: str) -> None:
    assert fmt.fmt_ms(value) == expected


def test_ratio_and_percent() -> None:
    assert fmt.fmt_ratio(1.6) == "1.60\u00d7"
    assert fmt.fmt_ratio(63.11) == "63.1\u00d7"
    assert fmt.fmt_ratio(None) == "—"
    assert fmt.fmt_pct(37.5) == "37.5%"


def test_dates_and_durations() -> None:
    assert fmt.fmt_dt("2026-10-20T16:48:18.674115") == "2026-10-20 16:48 UTC"
    assert fmt.fmt_dt(None) == "—"
    assert fmt.fmt_duration("2026-10-20T16:48:19", "2026-10-20T16:48:21.500000") == "2.5 s"
    assert fmt.fmt_duration("2026-10-20T16:00:00", "2026-10-20T16:30:00") == "30.0 min"
    assert fmt.fmt_duration(None, "2026-10-20T16:30:00") == "—"


def test_github_permalink_is_pinned_to_the_commit() -> None:
    link = fmt.github_permalink(
        "GITHUB_PUBLIC", "https://github.com/acme/shop.git", "a" * 40, "src/my file.py", 3, 7
    )
    assert link == f"https://github.com/acme/shop/blob/{'a' * 40}/src/my%20file.py#L3-L7"
    single = fmt.github_permalink(
        "GITHUB_PUBLIC", "https://github.com/acme/shop", "abc1234", "a.py", 5, 5
    )
    assert single == "https://github.com/acme/shop/blob/abc1234/a.py#L5"


@pytest.mark.parametrize(
    ("source", "locator", "sha", "path"),
    [
        ("LOCAL_DIRECTORY", "/srv/repo", "a" * 40, "a.py"),
        ("ZIP_UPLOAD", "zip:abc", "a" * 40, "a.py"),
        ("GITHUB_PUBLIC", "https://github.com/acme/shop", None, "a.py"),
        ("GITHUB_PUBLIC", "https://github.com/acme/shop", "main", "a.py"),
        ("GITHUB_PUBLIC", "https://evil.example/acme/shop", "a" * 40, "a.py"),
        ("GITHUB_PUBLIC", "https://github.com/acme/shop", "a" * 40, "../etc/passwd"),
        ("GITHUB_PUBLIC", "https://github.com/acme/shop", "a" * 40, "/abs.py"),
    ],
)
def test_github_permalink_refuses_anything_else(
    source: str, locator: str, sha: str | None, path: str
) -> None:
    assert fmt.github_permalink(source, locator, sha, path, 1) is None


def test_max_severity_and_items_of() -> None:
    assert fmt.max_severity({"LOW": 2, "HIGH": 1, "MEDIUM": 0}) == "HIGH"
    assert fmt.max_severity({"HIGH": 0}) is None
    assert fmt.items_of([{"a": 1}, "x"]) == [{"a": 1}]
    assert fmt.items_of({"items": [{"b": 2}], "total": 1}) == [{"b": 2}]
    assert fmt.items_of(None) == []
    assert fmt.short_sha("0123456789abcdef") == "01234567"
    assert fmt.humanize("ASYNC_BLOCKING") == "Async blocking"
