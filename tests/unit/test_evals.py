"""The evaluation runner: deterministic cases all pass, and a mocked LLM run is guarded."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from llm_support import EMPTY_OUTPUT, FakeLLM, client_factory, llm_settings

from backend.ai import llm_client
from backend.core.config import Settings

EVALS = Path(__file__).parents[2] / "evals"


def _runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "evaluation_runner", EVALS / "evaluation_runner.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


def test_cases_file_shape() -> None:
    cases = json.loads((EVALS / "recommendation_cases.json").read_text())["cases"]
    assert 20 <= len(cases) <= 30
    assert len({c["id"] for c in cases}) == len(cases)
    assert sum(not c["expect"]["rules"] for c in cases) >= 5


def test_deterministic_run_passes_and_writes_report(tmp_path: Path) -> None:
    runner = _runner()
    output = tmp_path / "report.md"
    assert runner.main(["--output", str(output)]) == 0
    report = output.read_text()
    assert "Cases passing every check: 30/30" in report
    assert "LLM calls / fallbacks | 0 / 0" in report
    assert not list(EVALS.glob(".work-*"))


def test_mocked_llm_run_drops_hallucinations(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def responder(step: str, user: str) -> Any:
        if step != "recommendations":
            return EMPTY_OUTPUT[step]
        return {
            "recommendations": [
                {
                    "title": "Rewrite the service in Rust",
                    "summary": "Everything would be faster.",
                    "rationale": "Trust me.",
                    "proposed_strategy": "Rewrite.",
                    "citations": [{"file_path": "src/main.rs", "line_start": 1, "line_end": 9}],
                    "uncertainty": "None.",
                    "expected_effect": "10x faster.",
                }
            ]
        }

    fake = FakeLLM(responder)
    monkeypatch.setattr(llm_client, "create_llm_client", client_factory(fake))
    runner = _runner()
    cases = json.loads((EVALS / "recommendation_cases.json").read_text())["cases"][:3]
    cfg = llm_settings(settings)
    results = [runner.evaluate_case(case, cfg, True) for case in cases]
    assert fake.calls > 0
    assert all(r.passed for r in results)
    for result in results:
        usage = result.llm.usage
        assert usage.rejected_items >= 1
        assert usage.rejected_citations >= 1
        assert all(rec.provenance.value == "STATIC" for rec in result.llm.recommendations)
    report = runner.render_report(results, True, "fake-model")
    assert "| AI_HYPOTHESIS recommendations kept | 0 |" in report
