"""docs/openapi.json, docs/api.md and the settings table in docs/deployment.md are generated.

If this fails, routes or settings changed: run `make openapi` and commit the result.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("script", ["export_openapi", "export_settings"])
def test_generated_docs_are_current(script: str) -> None:
    assert _load(script).main(["--check"]) == 0, f"stale docs: run `make openapi` ({script})"


def test_api_overview_groups_every_route() -> None:
    module = _load("export_openapi")
    schema = module.build_schema()
    markdown = module.render_markdown(schema)
    for path in schema["paths"]:
        assert f"`{path}`" in markdown
    assert markdown.index("## Repository audit") < markdown.index("## Workload optimization")


def test_settings_table_lists_every_setting() -> None:
    from backend.core.config import Settings

    table = _load("export_settings").render_table()
    for name in Settings.model_fields:
        assert f"`PERFPILOT_{name.upper()}`" in table
