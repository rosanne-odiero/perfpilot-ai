from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.core.config import Settings
from backend.jobs import service as jobs
from backend.jobs.worker import Worker
from backend.models.database import session_scope
from backend.models.orm.workloads import ProfileRun, Project, Workload
from backend.models.workload_api import ProfileRunOut
from backend.models.workloads import ProfileRequest, WorkloadManifest
from backend.services import profiling
from backend.services.profiling import WorkloadExecutionError

ROOT = Path(__file__).resolve().parents[2]


def _register(tmp_path: Path, source: str) -> str:
    ws = tmp_path / "custom"
    ws.mkdir()
    (ws / "workload.py").write_text(source)
    (ws / "fixtures.py").write_text("def make_input(n, seed=0):\n    return list(range(n))\n")
    manifest = WorkloadManifest(
        name="custom",
        description="test workload",
        entrypoint="workload:run",
        fixture="fixtures:make_input",
        expected_output={"type": "int"},  # type: ignore[arg-type]
        default_size="small",
        sizes={"small": {"n": 10}},
        timeout_seconds=20,
    )
    with session_scope() as session:
        project = Project(name="custom", root_path=str(tmp_path), kind="test")
        session.add(project)
        session.flush()
        workload = Workload(
            project_id=project.id,
            slug="custom",
            name="custom",
            workspace_path=str(ws),
            manifest=manifest.model_dump(mode="json"),
        )
        session.add(workload)
        session.flush()
        return workload.id


def test_failing_workload_marks_profile_and_job_failed(settings: Settings, tmp_path: Path) -> None:
    workload_id = _register(
        tmp_path, "import os\n\ndef run(xs):\n    raise ValueError('token=abcdef1234567890XYZ')\n"
    )
    with session_scope() as session:
        run, job = profiling.submit_profile(session, ProfileRequest(workload_id=workload_id))
        run_id, job_id = run.id, job.id
    Worker(settings, isolate=False).drain()
    with session_scope() as session:
        stored = session.get(ProfileRun, run_id)
        assert stored is not None
        assert stored.status == "FAILED"
        assert "ValueError" in (stored.error or "")
        details = stored.error_details or {}
        assert details["code"] == "workload_execution_failed"
        assert "abcdef1234567890XYZ" not in json.dumps(details)
        job = jobs.get_job(session, job_id)
        assert job.status == "FAILED"


def test_profile_of_a_custom_workspace_workload(settings: Settings, tmp_path: Path) -> None:
    workload_id = _register(tmp_path, "def run(xs):\n    return sum(sorted(xs))\n")
    with session_scope() as session:
        run_id = profiling.create_profile_run(session, ProfileRequest(workload_id=workload_id)).id
    profiling.execute_profile_run(run_id, settings)
    with session_scope() as session:
        view = profiling.profile_view(session, run_id)
    assert view.status == "SUCCEEDED"
    assert [h.function_name for h in view.top_user_hotspots] == ["run"]


def test_missing_workspace_fails_cleanly(settings: Settings, tmp_path: Path) -> None:
    workload_id = _register(tmp_path, "def run(xs):\n    return 1\n")
    (tmp_path / "custom" / "workload.py").unlink()
    with session_scope() as session:
        run_id = profiling.create_profile_run(session, ProfileRequest(workload_id=workload_id)).id
    with pytest.raises(Exception) as excinfo:
        profiling.execute_profile_run(run_id, settings)
    assert not isinstance(excinfo.value, WorkloadExecutionError)
    with session_scope() as session:
        assert session.get(ProfileRun, run_id).status == "FAILED"  # type: ignore[union-attr]


def test_cli_profile_uses_the_same_services(tmp_path: Path) -> None:
    env = {**os.environ, "PERFPILOT_DATA_DIR": str(tmp_path / "cli-data")}
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_workload.py"),
            "profile",
            "list_lookup",
            "--size",
            "small",
            "--top",
            "3",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=True,
        cwd=ROOT,
    )
    payload = json.loads(completed.stdout)
    view = ProfileRunOut.model_validate({k: v for k, v in payload.items() if k != "hotspots"})
    assert view.status == "SUCCEEDED"
    assert view.top_user_hotspots[0].function_name == "find_known"
    assert len(payload["hotspots"]) == 3


def test_seeding_skips_invalid_examples(settings: Settings, tmp_path: Path) -> None:
    import shutil

    from backend.services.workloads import list_workloads, seed_bundled_examples

    examples = tmp_path / "examples"
    shutil.copytree(ROOT / "examples" / "list_lookup", examples / "good")
    bad_manifest = examples / "bad_manifest"
    bad_manifest.mkdir()
    (bad_manifest / "workload.json").write_text("{not json")
    escaping = examples / "escaping"
    shutil.copytree(ROOT / "examples" / "list_lookup", escaping)
    (escaping / "workload.py").unlink()
    (escaping / "workload.py").symlink_to(ROOT / "examples" / "list_lookup" / "workload.py")
    with session_scope() as session:
        project = seed_bundled_examples(session, examples)
        assert project is not None
        assert [w.slug for w in list_workloads(session, project.id)] == ["good"]
        first_snapshot = list_workloads(session, project.id)[0].source_snapshot_id
    with session_scope() as session:
        project = seed_bundled_examples(session, examples)
        assert project is not None
        workloads = list_workloads(session, project.id)
        assert len(workloads) == 1
        assert workloads[0].source_snapshot_id == first_snapshot
