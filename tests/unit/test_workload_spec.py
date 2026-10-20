from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from backend.models.workloads import (
    BenchmarkRequest,
    CandidateRef,
    ProfileRequest,
    WorkloadManifest,
    validate_env_names,
    validate_target,
)
from backend.services.workloads import EXAMPLES_DIR, discover_bundled

BUNDLED = ["duplicate_counter", "list_lookup", "nested_loop", "regex_matching", "string_builder"]


def _manifest(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "x",
        "description": "d",
        "entrypoint": "workload:run",
        "fixture": "fixtures:make_input",
        "expected_output": {"type": "int"},
        "default_size": "small",
        "sizes": {"small": {"n": 1}},
    }
    data.update(overrides)
    return data


def test_all_bundled_examples_are_discovered_and_valid() -> None:
    found = discover_bundled()
    assert [slug for slug, _, _ in found] == BUNDLED
    for slug, path, manifest in found:
        assert path == (EXAMPLES_DIR / slug).resolve()
        assert set(manifest.sizes) == {"small", "medium", "large"}
        assert manifest.reference_candidate == "reference/optimized.py"
        reference = (path / "reference" / "optimized.py").read_text()
        assert "REFERENCE IMPLEMENTATION" in reference
        assert (path / "README.md").is_file()
        assert (path / "tests" / "test_workload.py").is_file()


def test_manifest_defaults_and_validation() -> None:
    manifest = WorkloadManifest.model_validate(_manifest())
    assert manifest.invocation_mode == "single"
    assert manifest.timeout_seconds == 120
    with pytest.raises(ValidationError):
        WorkloadManifest.model_validate(_manifest(default_size="huge"))
    with pytest.raises(ValidationError):
        WorkloadManifest.model_validate(_manifest(sizes={}))
    with pytest.raises(ValidationError):
        WorkloadManifest.model_validate(_manifest(sizes={"../x": {}}, default_size="../x"))
    with pytest.raises(ValidationError):
        WorkloadManifest.model_validate(_manifest(unexpected=True))


@pytest.mark.parametrize(
    "target",
    [
        "../evil:run",
        "/etc/passwd:run",
        "pkg/mod:run",
        "C:\\mod:run",
        "mod:__import__",
        "__init__:run",
        "mod",
        "mod:run:extra",
        "mod:run.attr",
        "mod.:run",
        "",
    ],
)
def test_target_validation_rejects_unsafe_values(target: str) -> None:
    with pytest.raises(ValueError):
        validate_target(target, "entrypoint")
    with pytest.raises(ValidationError):
        WorkloadManifest.model_validate(_manifest(entrypoint=target))


@pytest.mark.parametrize("target", ["workload:run", "pkg.sub.mod:fn_2", "_m:_f"])
def test_target_validation_accepts_identifiers(target: str) -> None:
    assert validate_target(target, "entrypoint") == target


@pytest.mark.parametrize(
    "reference", ["/abs/optimized.py", "../optimized.py", "ref/../../x.py", "ref/opt.txt"]
)
def test_reference_candidate_must_stay_inside(reference: str) -> None:
    with pytest.raises(ValidationError):
        WorkloadManifest.model_validate(_manifest(reference_candidate=reference))


@pytest.mark.parametrize(
    "name",
    [
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "PATH",
        "HOME",
        "PERFPILOT_LLM_API_KEY",
        "lowercase",
        "BAD-NAME",
    ],
)
def test_forbidden_env_vars(name: str) -> None:
    with pytest.raises(ValueError):
        validate_env_names([name])


def test_permitted_env_vars_are_normalized() -> None:
    assert validate_env_names(["B_VAR", "A_VAR", "B_VAR"]) == ["A_VAR", "B_VAR"]


def test_requests_validate_bounds() -> None:
    assert BenchmarkRequest(workload_id="w").candidate == "reference"
    assert BenchmarkRequest(workload_id="w").warmups == 5
    assert BenchmarkRequest(workload_id="w").runs == 20
    request = BenchmarkRequest.model_validate(
        {"workload_id": "w", "candidate": {"candidate_id": "c"}}
    )
    assert request.candidate == CandidateRef(candidate_id="c")
    for bad in ({"runs": 1}, {"warmups": -1}, {"threshold_percent": 0}, {"candidate": "other"}):
        with pytest.raises(ValidationError):
            BenchmarkRequest.model_validate({"workload_id": "w", **bad})
    with pytest.raises(ValidationError):
        ProfileRequest.model_validate({"workload_id": "w", "extra": 1})


def test_bundled_manifest_files_are_json() -> None:
    for slug in BUNDLED:
        data = json.loads((EXAMPLES_DIR / slug / "workload.json").read_text())
        assert data["seed"] == WorkloadManifest.model_validate(data).seed
