"""Containment and isolation of workload execution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from backend.core.exceptions import SecurityViolationError, ValidationFailedError
from backend.profiling.runner import ResourceLimits, build_child_env, run_harness
from backend.services.workloads import resolve_target_file, resolve_workspace_file

LIMITS = ResourceLimits(timeout_seconds=30, memory_limit_mb=1024)
FIXTURES = "def make_input():\n    return None\n"


def _workspace(root: Path, workload_source: str, fixtures: str = FIXTURES) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "workload.py").write_text(workload_source)
    (root / "fixtures.py").write_text(fixtures)
    return root


def _request(workspace: Path, **overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "mode": "time",
        "workspace_root": str(workspace),
        "entrypoint": "workload:run",
        "fixture": "fixtures:make_input",
        "fixture_kwargs": {},
        "invocation_mode": "none",
        "include_output_value": True,
    }
    request.update(overrides)
    return request


# -- parent-side resolution ------------------------------------------------------------------


def test_entrypoint_outside_workspace_is_rejected(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws", "def run():\n    return 1\n")
    with pytest.raises(ValidationFailedError):
        resolve_target_file(ws, "json:dumps")  # importable, but not inside the workspace
    with pytest.raises(ValidationFailedError):
        resolve_target_file(ws, "../ws:run")


def test_symlinked_module_escaping_workspace_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("def run():\n    return 'escaped'\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "workload.py").symlink_to(outside)
    with pytest.raises(SecurityViolationError):
        resolve_target_file(ws, "workload:run")


def test_symlinked_package_escaping_workspace_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside_pkg"
    outside.mkdir()
    (outside / "__init__.py").write_text("")
    (outside / "mod.py").write_text("def run():\n    return 1\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pkg").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SecurityViolationError):
        resolve_target_file(ws, "pkg.mod:run")


@pytest.mark.parametrize("relative", ["../x.py", "/etc/hosts", "a/../../x.py"])
def test_workspace_file_traversal_is_rejected(tmp_path: Path, relative: str) -> None:
    ws = _workspace(tmp_path / "ws", "def run():\n    return 1\n")
    with pytest.raises(SecurityViolationError):
        resolve_workspace_file(ws, relative)


# -- child-side enforcement (the harness re-checks everything) --------------------------------


def test_harness_refuses_module_outside_workspace(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws", "def run():\n    return 1\n")
    run = run_harness(
        _request(ws, entrypoint="json:dumps"), run_dir=tmp_path / "run", limits=LIMITS
    )
    assert not run.ok
    assert run.result is not None
    assert run.result["error"]["code"] == "module_not_found"


def test_harness_refuses_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("def run():\n    return 'escaped'\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "fixtures.py").write_text(FIXTURES)
    (ws / "workload.py").symlink_to(outside)
    run = run_harness(_request(ws), run_dir=tmp_path / "run", limits=LIMITS)
    assert not run.ok
    assert run.result is not None
    assert run.result["error"]["code"] == "outside_workspace"


@pytest.mark.parametrize(
    ("entrypoint", "code"),
    [
        ("../ws:run", "invalid_target"),
        ("/abs:run", "invalid_target"),
        ("workload:__init__", "invalid_target"),
    ],
)
def test_harness_rejects_malformed_targets(tmp_path: Path, entrypoint: str, code: str) -> None:
    ws = _workspace(tmp_path / "ws", "def run():\n    return 1\n")
    run = run_harness(_request(ws, entrypoint=entrypoint), run_dir=tmp_path / "run", limits=LIMITS)
    assert run.result is not None
    assert run.result["error"]["code"] == code


@pytest.mark.parametrize("script", ["../evil.py", "/etc/hosts"])
def test_command_mode_script_must_be_inside_workspace(tmp_path: Path, script: str) -> None:
    ws = _workspace(tmp_path / "ws", "def run():\n    return 1\n")
    request = {
        "mode": "command",
        "workspace_root": str(ws),
        "command": {"kind": "script", "target": script, "args": []},
    }
    run = run_harness(request, run_dir=tmp_path / "run", limits=LIMITS)
    assert run.result is not None
    assert run.result["error"]["code"] == "outside_workspace"


# -- environment scrubbing -------------------------------------------------------------------


def test_non_permitted_env_vars_do_not_reach_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PERFPILOT_TEST_SECRET", "s3cr3t-value")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("ALLOWED_SETTING", "visible")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "evil"))
    ws = _workspace(tmp_path / "ws", "import os\n\ndef run():\n    return dict(os.environ)\n")
    run = run_harness(
        _request(ws), run_dir=tmp_path / "run", limits=LIMITS, permitted_env=["ALLOWED_SETTING"]
    )
    assert run.ok, run.failure_reason()
    pairs = dict(run.result["output_value"]["__dict__"])  # type: ignore[index]
    assert pairs["ALLOWED_SETTING"] == "visible"
    assert "PERFPILOT_TEST_SECRET" not in pairs
    assert "AWS_SECRET_ACCESS_KEY" not in pairs
    assert "PYTHONPATH" not in pairs
    assert pairs["PATH"] == os.defpath
    assert pairs["HOME"].startswith(str((tmp_path / "run").resolve()))
    assert "s3cr3t-value" not in str(run.result)


def test_child_env_cannot_override_fixed_variables(tmp_path: Path) -> None:
    with pytest.raises(SecurityViolationError):
        build_child_env(["PATH"], tmp_path)
    env = build_child_env(["MISSING_VAR"], tmp_path, parent_env={})
    assert "MISSING_VAR" not in env
    assert env["PYTHONHASHSEED"] == "0"


def test_child_runs_in_private_working_directory(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws", "import os\n\ndef run():\n    return os.getcwd()\n")
    run = run_harness(_request(ws), run_dir=tmp_path / "run", limits=LIMITS)
    assert run.ok
    assert Path(run.result["output_value"]).resolve() == (tmp_path / "run" / "cwd").resolve()  # type: ignore[index]


def test_argv_is_a_fixed_list_without_shell(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws", "def run():\n    return 1\n")
    run = run_harness(
        _request(ws, entrypoint="workload:run"), run_dir=tmp_path / "run", limits=LIMITS
    )
    assert run.argv[1:4] == ["-P", "-s", "-B"]
    assert run.argv[4].endswith("harness_entry.py")
    assert not any(part in ("sh", "/bin/sh", "-c") for part in run.argv)


def test_wrapper_must_be_absolute(tmp_path: Path) -> None:
    ws = _workspace(tmp_path / "ws", "def run():\n    return 1\n")
    with pytest.raises(SecurityViolationError):
        run_harness(_request(ws), run_dir=tmp_path / "run", limits=LIMITS, wrapper_argv=["py-spy"])
