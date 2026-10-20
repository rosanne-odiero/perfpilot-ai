"""Priority score formula and path-criticality factors."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from backend.audit.prioritization import (
    REACH_CALLED_FROM_ENTRY,
    REACH_ENTRY_SYMBOL,
    REACH_MODULE_REACHABLE,
    REACH_TEST_CODE,
    REACH_UNREACHED,
    fan_in_factor,
    loop_factor,
)
from backend.audit.static_audit import run_static_audit
from backend.core.config import Settings
from backend.models.audit_schemas import RepositoryInventory, SourceFileInfo

LOOP_BODY = """
def {name}(items):
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
"""


def test_factor_functions() -> None:
    assert loop_factor(0) == pytest.approx(0.6)
    assert loop_factor(1) == pytest.approx(0.8)
    assert loop_factor(5) == 1.0
    assert fan_in_factor(0, 0) == pytest.approx(0.7)
    assert fan_in_factor(5, 10) == pytest.approx(0.85)
    assert fan_in_factor(10, 10) == pytest.approx(1.0)


def test_reachability_levels_and_score(tmp_path: Path) -> None:
    files = {
        "app/__init__.py": "",
        "app/main.py": textwrap.dedent(
            """
            from app import service

            def main():
                service.handle([])

            if __name__ == "__main__":
                main()
            """
        )
        + LOOP_BODY.format(name="local_work"),
        "app/service.py": "from app import util\n\ndef handle(items):\n    return dedupe(items)\n"
        + LOOP_BODY.format(name="dedupe"),
        "app/util.py": LOOP_BODY.format(name="unused_helper"),
        "app/orphan.py": LOOP_BODY.format(name="orphan"),
        "tests/test_app.py": LOOP_BODY.format(name="test_helper"),
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    inventory = RepositoryInventory(
        root_name="r",
        files=[SourceFileInfo(path=p, size_bytes=1, is_python=True) for p in files],
    )
    result = run_static_audit(tmp_path, inventory, Settings())
    by_symbol = {f.location.symbol: f for f in result.findings if f.rule_id == "PP-LOOP-001"}
    reach = {s: f.priority_factors["entrypoint_reachability"] for s, f in by_symbol.items()}
    assert reach == {
        "local_work": REACH_MODULE_REACHABLE,
        "dedupe": REACH_CALLED_FROM_ENTRY,
        "unused_helper": REACH_MODULE_REACHABLE,
        "orphan": REACH_UNREACHED,
        "test_helper": REACH_TEST_CODE,
    }
    assert REACH_ENTRY_SYMBOL > REACH_CALLED_FROM_ENTRY
    finding = by_symbol["dedupe"]
    factors = finding.priority_factors
    assert factors["loop_depth"] == 1.0
    assert factors["path_criticality"] == pytest.approx(
        factors["entrypoint_reachability"] * factors["loop_factor"] * factors["fan_in_factor"],
        abs=1e-3,
    )
    expected = 100 * factors["severity_weight"] * factors["confidence_weight"]
    assert finding.priority_score == pytest.approx(expected * factors["path_criticality"], abs=0.01)
    ordered = [f.location.symbol for f in result.findings if f.rule_id == "PP-LOOP-001"]
    assert ordered[0] == "dedupe"
    assert ordered[-1] == "test_helper"
