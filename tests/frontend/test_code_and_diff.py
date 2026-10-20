from __future__ import annotations

import re

from frontend.components.code_view import code_html, context_window, highlight_lines
from frontend.components.diff_view import (
    diff_stats,
    side_by_side_html,
    side_by_side_rows,
    unified_diff,
)

SOURCE = 'def f(x):\n    """doc\n    more"""\n    return x < 2\n'


def test_one_fragment_per_line_even_for_multiline_tokens() -> None:
    lines = highlight_lines(SOURCE, "Python")
    assert len(lines) == 4
    assert "&quot;&quot;&quot;doc" in lines[1]
    assert "more&quot;&quot;&quot;" in lines[2]
    assert "&lt;" in lines[3]


def test_html_is_escaped() -> None:
    html = code_html("x = '<script>alert(1)</script>'\n", language="python")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_line_numbers_highlight_and_window() -> None:
    source = "".join(f"line{i}\n" for i in range(1, 31))
    html = code_html(source, language="text", highlight=[12, 13], window=(10, 15), title="f.txt")
    numbers = [int(n) for n in re.findall(r'<td class="pp-ln">(\d+)</td>', html)]
    assert numbers == list(range(10, 16))
    assert html.count('class="pp-hl"') == 2
    assert 'class="pp-hl"><td class="pp-ln">12<' in html
    assert "f.txt" in html


def test_first_line_offsets_numbers() -> None:
    html = code_html("a\nb\n", language="text", first_line=41, highlight=[42])
    assert re.findall(r'<td class="pp-ln">(\d+)</td>', html) == ["41", "42"]
    assert 'class="pp-hl"><td class="pp-ln">42<' in html


def test_unknown_language_falls_back_to_plain_text() -> None:
    assert "hello" in code_html("hello\n", language="no-such-language", filename="x.unknown")


def test_context_window_is_clamped() -> None:
    assert context_window(3, 4, total=100) == (1, 10)
    assert context_window(50, 52, total=100) == (44, 58)
    assert context_window(98, 99, total=100) == (92, 100)
    assert context_window(5, 5, total=3) == (1, 3)


BEFORE = "a\nb\nc\nd\n"
AFTER = "a\nB\nc\nd\ne\n"


def test_side_by_side_alignment() -> None:
    rows = side_by_side_rows(BEFORE, AFTER)
    kinds = [r.kind for r in rows]
    assert kinds == ["equal", "replace", "equal", "equal", "insert"]
    assert rows[1].left == "b"
    assert rows[1].right == "B"
    assert rows[4].left_no is None
    assert rows[4].right_no == 5
    assert diff_stats(rows) == (2, 1)


def test_pure_deletion_and_uneven_replace() -> None:
    rows = side_by_side_rows("a\nb\nc\n", "a\nX\n")
    assert [r.kind for r in rows] == ["equal", "replace", "delete"]
    assert diff_stats(rows) == (1, 2)


def test_side_by_side_html_and_unified_diff() -> None:
    html = side_by_side_html(BEFORE, AFTER, before_title="Baseline", after_title="Reference")
    assert "Baseline" in html
    assert "Reference" in html
    assert "+2 / -1 lines" in html
    assert html.count("pp-diff-add") >= 2
    diff = unified_diff(BEFORE, AFTER, "workload.py", "reference/optimized.py")
    assert diff.startswith("--- workload.py\n+++ reference/optimized.py\n")
    assert "-b\n+B\n" in diff
    assert unified_diff(BEFORE, BEFORE, "a", "b") == ""


def test_python_tokens_are_coloured() -> None:
    (decorator, header, body) = highlight_lines(
        "@cache\ndef total(xs):\n    return sum(xs) + 1  # note\n", "Python"
    )
    assert ">cache</span>" in decorator
    assert '<span style="color:#ff7b72">def</span>' in header
    assert ">total</span>" in header
    assert ">sum</span>" in body
    assert ">1</span>" in body
    assert 'font-style:italic"># note</span>' in body


def test_untokenizable_python_is_plain_text() -> None:
    assert highlight_lines("def broken(:\n  x", "Python") == ["def broken(:", "  x"]
    assert highlight_lines("a = '''open\n", filename="a.py") == ["a = &#x27;&#x27;&#x27;open"]
    assert highlight_lines("", "Python") == [""]
