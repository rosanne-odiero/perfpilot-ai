"""Execution-path criticality: role classification, paths, and the role-aware priority order."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from audit_support import SHOPFAST, scan_inventory

from backend.audit.path_ranking import CLI_PRIMARY_FACTOR, ROLE_FACTORS, path_hint
from backend.audit.static_audit import StaticAuditResult, run_static_audit
from backend.core.config import Settings
from backend.models.audit_schemas import Finding, RepositoryInventory, SourceFileInfo
from backend.models.enums import CodeRole

DEDUPE = """
def {name}(items):
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
"""

SERVICE_REPO: dict[str, str] = {
    "app/__init__.py": "",
    "app/main.py": textwrap.dedent(
        """
        from fastapi import APIRouter
        from app import services, helpers

        router = APIRouter()

        @router.get("/items")
        def list_items(batches):
            out = []
            for batch in batches:
                out.append(services.compute(batch))
            return out

        @router.post("/items")
        def create_item(items):
            seen = []
            for item in items:
                if item not in seen:
                    seen.append(item)
            return seen
        """
    ),
    "app/services.py": DEDUPE.format(name="compute"),
    "app/helpers.py": DEDUPE.format(name="imported_only"),
    "app/orphan.py": DEDUPE.format(name="orphan"),
    "app/tasks.py": "from celery import shared_task\n\n@shared_task\n"
    + DEDUPE.format(name="nightly").lstrip("\n"),
    "seed/seed.py": DEDUPE.format(name="seed_rows")
    + '\nif __name__ == "__main__":\n    seed_rows([])\n',
    "alembic/versions/0001_init.py": DEDUPE.format(name="upgrade"),
    "scripts/backfill.py": DEDUPE.format(name="backfill")
    + '\nif __name__ == "__main__":\n    backfill([])\n',
    "examples/demo.py": DEDUPE.format(name="demo"),
    "tests/test_app.py": DEDUPE.format(name="helper_in_test"),
}


def _audit(root: Path, files: dict[str, str]) -> StaticAuditResult:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    inventory = RepositoryInventory(
        root_name="r",
        files=[SourceFileInfo(path=p, size_bytes=1, is_python=True) for p in files],
    )
    return run_static_audit(root, inventory, Settings())


def _by_symbol(result: StaticAuditResult) -> dict[str, Finding]:
    return {
        f.location.symbol: f
        for f in result.findings
        if f.rule_id == "PP-LOOP-001" and f.location.symbol
    }


@pytest.mark.parametrize(
    ("path", "role"),
    [
        ("tests/test_x.py", CodeRole.TEST),
        ("pkg/conftest.py", CodeRole.TEST),
        ("backend/seed/seed.py", CodeRole.SEED_MIGRATION),
        ("backend/scripts/x/seed_ingredients.py", CodeRole.SEED_MIGRATION),
        ("alembic/versions/abc_init.py", CodeRole.SEED_MIGRATION),
        ("shop/migrations/0002_auto.py", CodeRole.SEED_MIGRATION),
        ("app/fixtures/load.py", CodeRole.SEED_MIGRATION),
        ("examples/demo.py", CodeRole.EXAMPLE_DOCS),
        ("docs/conf.py", CodeRole.EXAMPLE_DOCS),
        ("scripts/backfill.py", CodeRole.CLI_SCRIPT),
        ("benchmarks/run.py", CodeRole.CLI_SCRIPT),
        ("manage.py", CodeRole.CLI_SCRIPT),
        ("app/tasks.py", CodeRole.BACKGROUND_TASK),
        ("app/workers/email.py", CodeRole.BACKGROUND_TASK),
        ("app/routers/items.py", None),
        ("app/seeding_utils.py", None),
        ("app/testimonials.py", None),
    ],
)
def test_path_hints(path: str, role: CodeRole | None) -> None:
    assert path_hint(path) == role


def test_roles_paths_and_order(tmp_path: Path) -> None:
    result = _audit(tmp_path, SERVICE_REPO)
    found = _by_symbol(result)
    roles = {name: f.code_role for name, f in found.items()}
    assert roles == {
        "create_item": CodeRole.REQUEST_HANDLER,
        "compute": CodeRole.REQUEST_PATH,
        "nightly": CodeRole.BACKGROUND_TASK,
        "imported_only": CodeRole.LIBRARY,
        "orphan": CodeRole.INTERNAL,
        "seed_rows": CodeRole.SEED_MIGRATION,
        "upgrade": CodeRole.SEED_MIGRATION,
        "backfill": CodeRole.CLI_SCRIPT,
        "demo": CodeRole.EXAMPLE_DOCS,
        "helper_in_test": CodeRole.TEST,
    }
    compute = found["compute"]
    assert compute.execution_path == ["app.main:list_items", "app.services:compute"]
    # list_items calls compute once per batch: the hot loop upstream raises the loop factor
    assert compute.priority_factors["path_loop"] == 1.0
    assert compute.priority_factors["loop_factor"] == 1.0
    assert found["imported_only"].execution_path == ["app.main", "app.helpers:imported_only"]
    for finding in found.values():
        assert finding.code_role is not None
        factors = finding.priority_factors
        assert factors["path_role"] == ROLE_FACTORS[finding.code_role]
        assert factors["path_criticality"] == pytest.approx(
            factors["entrypoint_reachability"]
            * factors["path_role"]
            * factors["loop_factor"]
            * factors["fan_in_factor"],
            abs=1e-3,
        )
    order = [f.location.symbol for f in result.findings if f.location.symbol in found]
    assert order.index("create_item") < order.index("seed_rows")
    assert order.index("compute") < order.index("backfill") < order.index("seed_rows")
    assert order.index("nightly") < order.index("orphan")
    assert order[-1] == "helper_in_test"
    assert found["seed_rows"].priority_score < found["create_item"].priority_score / 2


def test_cli_is_primary_without_service_entrypoints(tmp_path: Path) -> None:
    files = {
        "tool/__init__.py": "",
        "tool/cli.py": "from tool import core\n\ndef main():\n    core.dedupe([])\n\n"
        'if __name__ == "__main__":\n    main()\n',
        "tool/core.py": DEDUPE.format(name="dedupe"),
    }
    found = _by_symbol(_audit(tmp_path, files))
    assert found["dedupe"].code_role is CodeRole.CLI_SCRIPT
    assert found["dedupe"].priority_factors["path_role"] == CLI_PRIMARY_FACTOR
    assert found["dedupe"].execution_path == ["tool.cli:main", "tool.core:dedupe"]


def test_for_iterable_is_not_a_hot_loop(tmp_path: Path) -> None:
    files = {
        "app/__init__.py": "",
        "app/api.py": textwrap.dedent(
            """
            from fastapi import APIRouter
            from app.svc import load

            router = APIRouter()

            @router.get("/")
            def index():
                total = 0
                for row in load([]):
                    total += row
                return total
            """
        ),
        "app/svc.py": DEDUPE.format(name="load"),
    }
    found = _by_symbol(_audit(tmp_path, files))
    assert found["load"].code_role is CodeRole.REQUEST_PATH
    assert found["load"].priority_factors["path_loop"] == 0.0


def test_shopfast_handlers_outrank_cli_and_internal_code() -> None:
    result = run_static_audit(SHOPFAST, scan_inventory(SHOPFAST), Settings())
    roles = {(f.location.file_path, f.location.line_start): f.code_role for f in result.findings}
    assert roles[("shopfast/api.py", 19)] is CodeRole.REQUEST_HANDLER
    assert roles[("shopfast/api.py", 31)] is CodeRole.REQUEST_HANDLER
    assert roles[("shopfast/orders.py", 13)] is CodeRole.CLI_SCRIPT
    assert roles[("shopfast/pricing.py", 61)] is CodeRole.INTERNAL
    top_two = {(f.location.file_path, f.location.line_start) for f in result.findings[:2]}
    assert top_two == {("shopfast/api.py", 19), ("shopfast/api.py", 31)}
    orders = next(f for f in result.findings if f.location.file_path == "shopfast/orders.py")
    assert orders.execution_path == ["shopfast.app:main", "shopfast.orders:order_summaries"]
