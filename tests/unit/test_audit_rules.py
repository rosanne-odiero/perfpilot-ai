"""Positive and false-positive tests for every static rule."""

from __future__ import annotations

import textwrap

import pytest

from backend.audit.rule_engine import build_snippet, finding_id, run_rules
from backend.audit.rules import RULE_CLASSES, rule_catalog
from backend.indexing.source import parse_source
from backend.models.audit_schemas import Finding
from backend.models.enums import Confidence, Provenance, Severity


def analyze(source: str, path: str = "pkg/mod.py") -> list[Finding]:
    module = parse_source(path, textwrap.dedent(source).lstrip("\n"))
    module.module_name = path.removesuffix(".py").replace("/", ".")
    return [item.finding for item in run_rules([module]).findings]


def lines(source: str, rule_id: str, path: str = "pkg/mod.py") -> list[int]:
    return sorted(f.location.line_start for f in analyze(source, path) if f.rule_id == rule_id)


def test_rule_ids_are_unique_and_stable() -> None:
    ids = [meta.rule_id for meta in rule_catalog()]
    assert len(ids) == len(set(ids)) == len(RULE_CLASSES)
    assert all(i.startswith("PP-") for i in ids)


# --- PP-LOOP-001 ---------------------------------------------------------------------


def test_list_membership_in_loop_positive() -> None:
    src = """
    def dedupe(items):
        seen = []
        for item in items:
            if item not in seen:
                seen.append(item)
        return seen

    def keep(rows, banned: list[str]):
        return [r for r in rows if r.name not in banned]

    def ids_check(users, orders):
        for order in orders:
            if order.user_id in [u.id for u in users]:
                yield order
    """
    found = [f for f in analyze(src) if f.rule_id == "PP-LOOP-001"]
    assert [f.location.line_start for f in found] == [4, 9, 13]
    assert found[0].confidence is Confidence.HIGH
    assert found[1].confidence is Confidence.MEDIUM  # parameter annotation only
    assert "hashable" in found[0].tradeoffs


def test_list_membership_false_positives() -> None:
    src = """
    ALLOWED = ["a", "b"]

    def f(items, lookup: dict[str, int], text: str):
        allowed = set(ALLOWED)
        small = [1, 2]
        for item in items:
            if item in allowed or item in lookup or item in text or item in range(10):
                pass
            if item in ALLOWED or item in small or item in ("x", "y"):
                pass
        return "a" in ALLOWED
    """
    assert lines(src, "PP-LOOP-001") == []


# --- PP-LOOP-002 ---------------------------------------------------------------------


def test_nested_loop_join_positive() -> None:
    src = """
    def join(orders, customers):
        out = []
        for order in orders:
            for customer in customers:
                if customer.id == order.customer_id:
                    out.append((order, customer))
        return out

    def first_match(orders, customers):
        for order in orders:
            match = next((c for c in customers if c.id == order.customer_id), None)
            yield match
    """
    found = [f for f in analyze(src) if f.rule_id == "PP-LOOP-002"]
    assert [(f.location.line_start, f.location.line_end) for f in found] == [(4, 5), (11, 11)]
    assert found[0].severity is Severity.HIGH


def test_nested_loop_join_false_positives() -> None:
    src = """
    def walk(trees):
        for tree in trees:
            for child in tree.children:
                if child.id == tree.id:
                    pass

    def grid(n):
        for i in range(n):
            for j in ("a", "b"):
                if i == j:
                    pass

    def compare(a, b):
        for x in a:
            for y in b:
                if x.score > y.score:
                    pass

    def pairwise(graph):
        for w in graph:
            for u in graph.nodes:
                if u == w:
                    continue
                yield u, w

    def diagonal(n, counts):
        for v in range(n):
            for w in range(n):
                if v == w:
                    assert counts[v][w] == 0
    """
    assert lines(src, "PP-LOOP-002") == []


# --- PP-LOOP-003 / PP-LOOP-004 ---------------------------------------------------------


def test_loop_invariant_work_positive() -> None:
    src = """
    import json

    def score(items, weights, config_text):
        for item in items:
            ranked = sorted(weights)
            settings = json.loads(config_text)
            data = open("prices.csv").read()
            yield ranked, settings, data, item
    """
    assert lines(src, "PP-LOOP-003") == [5, 6, 7]


def test_loop_invariant_work_false_positives() -> None:
    src = """
    def f(items, weights):
        for item in items:
            ranked = sorted(item.weights)
            remaining = list(weights)
            remaining.remove(item)
            fresh = set()
            fresh.add(item)
            n = len(weights)
        while True:
            text = open("status").read()
            if text:
                break
        for w in weights:
            weights.append(w)
            total = sum(weights)
            lo = min(n, 2.0)
            expected = list(range(5))
    """
    assert lines(src, "PP-LOOP-003") == []


def test_loop_invariant_comprehension() -> None:
    src = """
    def notify(orders, users):
        for order in orders:
            active_ids = {u.id for u in users if u.active}
            if order.user_id in active_ids:
                yield order
            mine = [o for o in orders if o.user_id == order.user_id]
            fresh = [u for u in users]
            fresh.append(order)
            squares = [i * i for i in range(5)]
    """
    assert lines(src, "PP-LOOP-003") == [3]


def test_stateful_iterators_are_not_invariant() -> None:
    src = """
    from itertools import islice

    def chunks(bits):
        while True:
            chunk = list(islice(bits, 6))
            if not chunk:
                return
            yield chunk
    """
    assert lines(src, "PP-LOOP-003") == []


def test_sort_in_loop() -> None:
    src = """
    def top(items):
        best = []
        for item in items:
            best.append(item)
            best.sort()
        return best

    def per_item(groups):
        for group in groups:
            members = list(group)
            members.append(0)
            members.sort()
    """
    assert lines(src, "PP-LOOP-004") == [5]


# --- PP-STR-001 -----------------------------------------------------------------------


def test_string_concat_positive() -> None:
    src = """
    def render(rows):
        out = ""
        for row in rows:
            out += f"{row}\\n"
        body = ""
        i = 0
        while i < 3:
            body = body + str(i)
            i += 1
        return out + body
    """
    found = [f for f in analyze(src) if f.rule_id == "PP-STR-001"]
    assert [f.location.line_start for f in found] == [4, 8]
    assert all("CPython" in f.description for f in found)


def test_string_concat_false_positives() -> None:
    src = """
    def f(rows):
        total = 0
        parts = []
        for row in rows:
            total += row.amount
            parts += [row.name]
            line = ""
            line += row.name
        for text in rows:
            text += "\n"
            print(text)
        return total, parts
    """
    assert lines(src, "PP-STR-001") == []


def test_string_concat_read_inside_the_loop_is_not_flagged() -> None:
    src = """
    async def stream(resp, emit):
        answer = ""
        async for chunk in resp:
            if chunk.text:
                answer += chunk.text
                yield {"answer": answer}

    def preview(rows):
        text = ""
        for row in rows:
            text += row
            emit(text)
        tail = ""
        for row in rows:
            tail = tail + row
            if tail[-3:] == "end":
                break
        buf = ""
        while len(buf) < 80:
            buf += "x"
        return text, tail, buf

    def closure(rows, cb):
        acc = ""
        for row in rows:
            acc += row
            cb(lambda: acc)
        return acc
    """
    assert lines(src, "PP-STR-001") == []


def test_string_concat_is_flagged_in_async_for_and_while_when_used_after_the_loop() -> None:
    src = """
    async def collect(resp):
        answer = ""
        async for chunk in resp:
            if chunk.text:
                answer += chunk.text
        return answer

    def pad(n):
        out = ""
        i = 0
        while i < n:
            out = out + "-"
            i += 1
        for row in range(n):
            pass
        else:
            out += "!"
        return out
    """
    assert lines(src, "PP-STR-001") == [5, 12]


# --- PP-REGEX-001 ---------------------------------------------------------------------


def test_regex_in_loop() -> None:
    src = """
    import re
    PATTERN = r"\\d+"

    def f(lines, user_pattern):
        for line in lines:
            a = re.compile(r"[a-z]+")
            b = re.search(PATTERN, line)
            c = re.match(user_pattern, line)
            d = re.sub(f"{line}", "", line)
        e = re.compile("x")
    """
    found = sorted(
        (f for f in analyze(src) if f.rule_id == "PP-REGEX-001"),
        key=lambda f: f.location.line_start,
    )
    assert [f.location.line_start for f in found] == [6, 7]
    assert found[0].confidence is Confidence.HIGH
    assert found[1].confidence is Confidence.LOW


# --- PP-COLL-001 / 002 / 003 ----------------------------------------------------------


def test_list_search_methods_in_loop() -> None:
    src = """
    def f(values: list[int], other):
        for v in values:
            values.index(v)
            values.count(v)
            other.count(v)
            "abc".count("a")
    """
    assert lines(src, "PP-COLL-001") == [3, 4]


def test_list_as_queue() -> None:
    src = """
    from collections import deque

    def bfs(start):
        queue = [start]
        while queue:
            node = queue.pop(0)
            queue.insert(0, node)
        dq = deque([start])
        while dq:
            dq.pop()
            dq.popleft()
        stack = [1]
        while stack:
            stack.pop()
    """
    assert lines(src, "PP-COLL-002") == [6, 7]


def test_needless_materialization() -> None:
    src = """
    def f(groups, d):
        for g in groups:
            n = len(list(g))
            keys = list(d.keys())
        for k in list(d.keys()):
            del d[k]
        for g in groups:
            for k in list(d.keys()):
                expand(k)
    """
    assert lines(src, "PP-COLL-003") == [3, 4]


# --- PP-DB-001 ------------------------------------------------------------------------


def test_queries_in_loop_positive() -> None:
    src = """
    import sqlite3
    from sqlalchemy import select

    def a(conn: sqlite3.Connection, ids):
        cur = conn.cursor()
        for i in ids:
            cur.execute("SELECT 1 WHERE id = ?", (i,))
            cur.fetchone()

    def b(session, ids):
        return [session.get(User, i) for i in ids]

    def c(db, ids):
        for i in ids:
            db.query(User).filter(User.id == i).first()

    async def d(session, ids):
        for i in ids:
            await session.execute(select(User).where(User.id == i))

    def e(ids):
        for i in ids:
            User.objects.get(pk=i)
    """
    found = {f.location.line_start: f for f in analyze(src) if f.rule_id == "PP-DB-001"}
    assert sorted(found) == [7, 11, 15, 19, 23]
    assert found[7].confidence is Confidence.HIGH
    assert found[23].confidence is Confidence.HIGH


def test_queries_in_loop_false_positives() -> None:
    src = """
    import requests

    def f(conn, ids, cache):
        conn.execute("SELECT * FROM t WHERE id IN (?)", ids)
        for table in ["a", "b"]:
            conn.execute(f"DELETE FROM {table}")
        session = requests.Session()
        for i in ids:
            session.get(f"https://x/{i}")
            cache.get(i)
        page = 0
        while page < 10:
            conn.execute("SELECT 1 LIMIT 10 OFFSET ?", (page,))
            page += 1
    """
    assert lines(src, "PP-DB-001") == []


# --- PP-ASYNC-001 ---------------------------------------------------------------------


def test_blocking_calls_in_async_positive() -> None:
    src = """
    import time, subprocess, sqlite3
    import requests
    import httpx
    from urllib.request import urlopen

    async def handler(db, url):
        time.sleep(1)
        requests.get(url)
        urlopen(url)
        httpx.get(url)
        subprocess.run(["ls"])
        conn = sqlite3.connect("x.db")
        with open("f") as fh:
            fh.read()
        db.query(User).all()
        db.commit()
    """
    found = [f for f in analyze(src) if f.rule_id == "PP-ASYNC-001"]
    by_line = {f.location.line_start: f for f in found}
    assert sorted(by_line) == [7, 8, 9, 10, 11, 12, 13, 15]
    assert by_line[13].severity is Severity.LOW
    assert "2 times" in by_line[15].description  # one finding per coroutine for the session


def test_blocking_calls_in_async_false_positives() -> None:
    src = """
    import asyncio, time
    import requests, httpx

    async def ok(session, url):
        await asyncio.sleep(1)
        await asyncio.to_thread(time.sleep, 1)
        await asyncio.to_thread(lambda: requests.get(url))
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, requests.get, url)
        async with httpx.AsyncClient() as client:
            await client.get(url)
        await session.execute("SELECT 1")
        await session.commit()

        def sync_helper():
            time.sleep(1)

        return sync_helper

    def not_async(url):
        time.sleep(1)
        requests.get(url)
    """
    assert lines(src, "PP-ASYNC-001") == []


# --- PP-NET-001 / PP-NET-002 ----------------------------------------------------------


def test_network_calls_in_loop() -> None:
    src = """
    import requests, httpx

    def sync(urls):
        out = []
        for url in urls:
            out.append(requests.get(url, timeout=5))
        return out

    async def concurrent(urls):
        async with httpx.AsyncClient() as client:
            for url in urls:
                await client.get(url)

    def retry(url):
        for attempt in range(3):
            return requests.get(url)
        for _ in range(5):
            requests.get(url)
    """
    assert lines(src, "PP-NET-001") == [6, 12]


def test_client_per_call() -> None:
    src = """
    import httpx, requests

    CLIENT = httpx.Client()

    async def send(payload):
        async with httpx.AsyncClient() as client:
            await client.post("https://x", json=payload)

    def poll(urls):
        for url in urls:
            with requests.Session() as s:
                s.get(url)

    def get_client():
        return httpx.Client(timeout=5)

    class Api:
        def __init__(self):
            self.client = httpx.Client()

    def build():
        client = httpx.AsyncClient()
        return client
    """
    found = {f.location.line_start: f for f in analyze(src) if f.rule_id == "PP-NET-002"}
    assert sorted(found) == [6, 11]
    assert found[6].severity is Severity.LOW
    assert found[11].severity is Severity.MEDIUM


# --- PP-ALLOC-* -----------------------------------------------------------------------


def test_deepcopy_in_loop() -> None:
    src = """
    import copy

    def f(state, steps, records):
        snapshot = copy.deepcopy(state)
        for step in steps:
            yield copy.deepcopy(state)
        copies = [copy.deepcopy(r) for r in records]
        for key, value in state.items():
            copies.append(copy.deepcopy(value))
    """
    assert lines(src, "PP-ALLOC-001") == [6]


def test_constant_literal_in_loop() -> None:
    src = """
    def f(rows):
        for row in rows:
            codes = {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6, "g": 7, "h": 8}
            if row in [1, 2, 3, 4, 5, 6, 7, 8]:
                pass
            for x in [1, 2, 3, 4, 5, 6, 7, 8]:
                pass
            counts = {"a": 0, "b": 0, "c": 0, "d": 0, "e": 0, "f": 0, "g": 0, "h": 0}
            counts["a"] += 1
            small = [1, 2, 3]
            yield codes, small
    """
    assert lines(src, "PP-ALLOC-002") == [3]


def test_dataframe_growth_and_iterrows() -> None:
    src = """
    import pandas as pd

    def build(rows, frames):
        df = pd.DataFrame()
        for row in rows:
            df = pd.concat([df, pd.DataFrame([row])])
        out = pd.concat(frames)
        for _, r in df.iterrows():
            pass
        for t in df.itertuples():
            pass
        return out
    """
    assert lines(src, "PP-ALLOC-003") == [6]
    assert lines(src, "PP-ALLOC-004") == [8]


def test_pandas_rules_need_pandas() -> None:
    src = """
    def f(rows, df):
        out = []
        for row in rows:
            out = out.append(row)
        for _, r in df.iterrows():
            pass
    """
    assert lines(src, "PP-ALLOC-003") == []
    assert lines(src, "PP-ALLOC-004") == []


# --- PP-CACHE-001 / 002 ---------------------------------------------------------------


def test_recursion_without_cache() -> None:
    src = """
    import functools

    def fib(n):
        if n < 2:
            return n
        return fib(n - 1) + fib(n - 2)

    @functools.lru_cache(maxsize=None)
    def fib_cached(n):
        return n if n < 2 else fib_cached(n - 1) + fib_cached(n - 2)

    def depth(node):
        return 1 + max(depth(node.left), depth(node.right))

    def count(n):
        return 0 if n == 0 else 1 + count(n - 1)

    def noisy(n):
        print(n)
        return 1 if n < 2 else noisy(n - 1) + noisy(n - 2)
    """
    assert lines(src, "PP-CACHE-001") == [3]


def test_repeated_constant_call() -> None:
    src = """
    import json
    import pandas as pd
    CONFIG = '{"a": 1}'

    def f(text):
        a = json.loads(CONFIG)
        b = json.loads(CONFIG)
        c = json.loads(text)
        d = json.loads(text)
        prices = pd.read_csv("prices.csv")
        totals = pd.read_csv("prices.csv").sum()
        return a, b, c, d, prices, totals
    """
    found = [f for f in analyze(src) if f.rule_id == "PP-CACHE-002"]
    assert [f.location.line_start for f in found] == [7, 11]
    assert found[0].location.symbol == "f"


# --- PP-ARCH-001 / 002 ----------------------------------------------------------------


def test_import_time_work() -> None:
    src = """
    import json
    import requests
    import subprocess

    CONFIG = json.load(open("config.json"))
    RATES = requests.get("https://example.invalid/rates").json()
    TABLE = [i * i for i in range(1_000_000)]

    def lazy():
        return requests.get("https://example.invalid")

    if __name__ == "__main__":
        subprocess.run(["make"])
    """
    found = {f.location.line_start for f in analyze(src) if f.rule_id == "PP-ARCH-001"}
    assert found == {5, 6, 7}


def test_import_time_rule_skips_tests_and_build_scripts() -> None:
    src = """
    import subprocess
    subprocess.run(["git", "describe"])
    """
    assert lines(src, "PP-ARCH-001", path="setup.py") == []
    assert lines(src, "PP-ARCH-001", path="tests/test_x.py") == []


def test_unbounded_module_cache() -> None:
    src = """
    _cache = {}
    _memo: dict[str, int] = {}
    _bounded_cache = {}
    REGISTRY = {}

    def get(key):
        if key not in _cache:
            _cache[key] = compute(key)
        _memo.setdefault(key, 1)
        _bounded_cache[key] = 1
        if len(_bounded_cache) > 100:
            _bounded_cache.clear()
        REGISTRY[key] = 1
        return _cache[key]
    """
    assert lines(src, "PP-ARCH-002") == [1, 2]


# --- engine behavior ------------------------------------------------------------------


def test_code_that_runs_once_per_loop_is_not_reported() -> None:
    src = """
    def find(db, ids, codes):
        for i in ids:
            if i < 0:
                raise ValueError(f"bad ids: {[str(x) for x in ids]}")
            if i == 0:
                row = db.query(User).filter(User.id == i).first()
                return row
        for code in codes:
            text = ""
            if code:
                text += code
                break
    """
    found = {(f.rule_id, f.location.line_start) for f in analyze(src)}
    assert found == set()


def test_suppression_comment() -> None:
    src = """
    def f(items):
        seen = []
        for item in items:
            if item in seen:  # perfpilot: ignore[PP-LOOP-001]
                continue
            seen.append(item)
    """
    assert lines(src, "PP-LOOP-001") == []


def test_finding_contract_and_ids_are_deterministic() -> None:
    src = """
    def f(items):
        seen = []
        for item in items:
            if item not in seen:
                seen.append(item)
    """
    first, second = analyze(src), analyze(src)
    assert [f.id for f in first] == [f.id for f in second]
    finding = first[0]
    assert finding.provenance is Provenance.STATIC
    assert finding.location.symbol == "f"
    assert finding.snippet.split("\n")[-1].strip() == "if item not in seen:"
    assert finding.id == finding_id("PP-LOOP-001", "pkg/mod.py", 4, "item not in seen")


def test_snippet_is_bounded_and_exact() -> None:
    source_lines = [f"line {i}" for i in range(1, 41)]
    assert build_snippet(source_lines, 10, 10) == "line 8\nline 9\nline 10"
    long = build_snippet(source_lines, 5, 30).split("\n")
    assert long[0] == "line 5"
    assert len(long) == 12
    assert build_snippet(source_lines, 1, 1) == "line 1"


@pytest.mark.parametrize("rule", RULE_CLASSES)
def test_rules_ignore_unparseable_and_empty_modules(rule: type) -> None:
    module = parse_source("broken.py", "def f(:\n")
    assert module.tree is None
    assert run_rules([module], [rule()]).findings == []
    assert run_rules([parse_source("empty.py", "")], [rule()]).findings == []
