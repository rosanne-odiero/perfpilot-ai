"""Dependency preparation: static manifest sanitizing and the install lifecycle.

The lifecycle tests drive DockerSandboxBackend against the docker CLI *test double* from
conftest, which exercises PerfPilot's staging/rename/cleanup logic only. Real installs are covered
by the docker+network integration test.
"""

from __future__ import annotations

import itertools
import json
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from backend.core.config import Settings
from backend.core.exceptions import SecurityViolationError, ValidationFailedError
from backend.runtime.execution import install_failure_reason
from backend.sandbox.approvals import DependencyInstallGrant
from backend.sandbox.backend import DependencyInstallRequest, SandboxRunResult
from backend.sandbox.docker_backend import DockerSandboxBackend
from backend.sandbox.environment import (
    DependencyManifest,
    PreparedEnvironment,
    RequirementsSource,
    build_dependency_manifest,
    deps_dir_for,
    prepare_environment,
)

if TYPE_CHECKING:
    from conftest import FakeDocker


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip())
    return root


def test_requirements_file_is_sanitized(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "requirements.txt": """
                # runtime deps
                -r requirements/base.txt
                --index-url https://user:hunter2hunter2@evil.example/simple
                --extra-index-url https://evil.example/simple
                -e .
                ./local_pkg
                git+https://github.com/o/r.git#egg=r
                pkg @ https://evil.example/pkg.whl
                requests==2.32.3 --hash=sha256:abc123
                numpy>=1.26 ; python_version >= "3.10"
                flask[async] \\
                    >=3.0
                requests==2.32.3
                -r ../../etc/passwd
                -c constraints.txt
            """,
            "requirements/base.txt": "pydantic>=2\n-r ../requirements.txt\n",
        },
    )
    manifest = build_dependency_manifest(
        root, RequirementsSource(kind="requirements_file", path_rel="requirements.txt")
    )
    assert manifest.requirements == [
        "pydantic>=2",
        "requests==2.32.3",
        'numpy>=1.26; python_version >= "3.10"',
        "flask[async]>=3.0",
    ]
    assert manifest.sources == ["requirements.txt", "requirements/base.txt"]
    reasons = {s.location: s.reason for s in manifest.skipped}
    assert reasons["requirements.txt:3"].startswith("pip option not allowed: --index-url")
    assert "--extra-index-url" in reasons["requirements.txt:4"]
    assert "-e" in reasons["requirements.txt:5"]
    assert "local path" in reasons["requirements.txt:6"]
    assert "local path" in reasons["requirements.txt:7"]  # VCS URL is not a requirement
    assert "direct URL" in reasons["requirements.txt:8"]
    assert "'..'" in reasons["requirements.txt:14"]
    assert "-c" in reasons["requirements.txt:15"]
    assert all("hunter2" not in s.text for s in manifest.skipped)


def test_pyproject_dependencies_and_extras(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "pyproject.toml": """
                [project]
                name = "demo"
                dependencies = ["httpx>=0.27", "local @ file:///tmp/x"]

                [project.optional-dependencies]
                test = ["pytest-asyncio"]
            """
        },
    )
    source = RequirementsSource(kind="pyproject", path_rel="pyproject.toml", extras=["test"])
    manifest = build_dependency_manifest(root, source)
    assert manifest.requirements == ["httpx>=0.27", "pytest-asyncio"]
    assert [s.reason for s in manifest.skipped] == ["direct URL / VCS references are not installed"]
    with pytest.raises(ValidationFailedError, match="unknown optional-dependency"):
        build_dependency_manifest(root, source.model_copy(update={"extras": ["nope"]}))


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[tool.poetry]\nname = 'x'\n", "Poetry"),
        ("[tool.black]\n", "no \\[project\\] table"),
        ("not = [toml", "not valid TOML"),
    ],
)
def test_unsupported_pyprojects(tmp_path: Path, content: str, message: str) -> None:
    (tmp_path / "pyproject.toml").write_text(content)
    with pytest.raises(ValidationFailedError, match=message):
        build_dependency_manifest(
            tmp_path, RequirementsSource(kind="pyproject", path_rel="pyproject.toml")
        )


def test_dynamic_dependencies_are_reported(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\ndynamic = ['dependencies']\n")
    manifest = build_dependency_manifest(
        tmp_path, RequirementsSource(kind="pyproject", path_rel="pyproject.toml")
    )
    assert manifest.requirements == []
    assert "dynamic" in manifest.skipped[0].reason


def test_requirements_path_escapes_are_rejected(tmp_path: Path) -> None:
    outside = _write(tmp_path / "outside", {"requirements.txt": "requests\n"})
    root = tmp_path / "repo"
    root.mkdir()
    (root / "reqs.txt").symlink_to(outside / "requirements.txt")
    with pytest.raises(SecurityViolationError):
        build_dependency_manifest(
            root,
            RequirementsSource(kind="requirements_file", path_rel="../outside/requirements.txt"),
        )
    with pytest.raises(SecurityViolationError):
        build_dependency_manifest(
            root, RequirementsSource(kind="requirements_file", path_rel="reqs.txt")
        )
    (root / "real.txt").write_text("requests\n")
    (root / "alias.txt").symlink_to(root / "real.txt")
    with pytest.raises(ValidationFailedError, match="not found"):
        build_dependency_manifest(
            root, RequirementsSource(kind="requirements_file", path_rel="alias.txt")
        )


# ---------------------------------------------------------------------- lifecycle


def _grant(**overrides: object) -> DependencyInstallGrant:
    data: dict[str, object] = {
        "approval_id": "a1",
        "snapshot_id": "snap-1",
        "image": "perfpilot-sandbox:latest",
        "allow_sdist_builds": False,
    }
    data.update(overrides)
    return DependencyInstallGrant.model_validate(data)


@pytest.fixture
def workspace(settings: Settings) -> Path:
    return _write(
        settings.workspaces_dir / "snap-1" / "repo", {"requirements.txt": "attrs==24.2.0\n"}
    )


SOURCE = RequirementsSource(kind="requirements_file", path_rel="requirements.txt")


def test_successful_install_publishes_deps_dir(
    settings: Settings, workspace: Path, fake_docker: FakeDocker
) -> None:
    fake_docker.set_mode(run="echo", exit_code=0)
    prepared = prepare_environment(
        DockerSandboxBackend(settings),
        _grant(),
        workspace_dir=workspace,
        source=SOURCE,
        settings=settings,
    )
    final = deps_dir_for(settings, "snap-1")
    assert prepared.succeeded
    assert prepared.deps_dir == str(final)
    assert final.is_dir()
    env_root = final.parent
    assert (env_root / "requirements.txt").read_text() == "attrs==24.2.0\n"
    assert not list(env_root.glob("deps.partial-*"))
    meta = json.loads((env_root / "environment.json").read_text())
    assert meta["succeeded"] is True
    assert meta["approval_id"] == "a1"
    run = next(c for c in fake_docker.calls() if c[0] == "run")
    assert "--only-binary=:all:" in run
    assert ("--network", "bridge") in list(itertools.pairwise(run))


def test_failed_install_leaves_no_deps_dir(
    settings: Settings, workspace: Path, fake_docker: FakeDocker
) -> None:
    fake_docker.set_mode(run="echo", exit_code=1)
    prepared = prepare_environment(
        DockerSandboxBackend(settings),
        _grant(allow_sdist_builds=True),
        workspace_dir=workspace,
        source=SOURCE,
        settings=settings,
    )
    assert not prepared.succeeded
    assert prepared.result.exit_code == 1
    assert not deps_dir_for(settings, "snap-1").exists()
    assert not list(deps_dir_for(settings, "snap-1").parent.glob("deps.partial-*"))
    run = next(c for c in fake_docker.calls() if c[0] == "run")
    assert "--prefer-binary" in run


def test_install_preconditions(settings: Settings, workspace: Path, tmp_path: Path) -> None:
    backend = DockerSandboxBackend(settings)
    (workspace / "empty.txt").write_text("# nothing\n-e .\n")
    with pytest.raises(ValidationFailedError, match="no installable requirements"):
        prepare_environment(
            backend,
            _grant(),
            workspace_dir=workspace,
            source=RequirementsSource(kind="requirements_file", path_rel="empty.txt"),
            settings=settings,
        )
    with pytest.raises(SecurityViolationError):
        prepare_environment(
            backend,
            _grant(snapshot_id="../x"),
            workspace_dir=workspace,
            source=SOURCE,
            settings=settings,
        )
    with pytest.raises(SecurityViolationError):
        prepare_environment(
            backend, _grant(), workspace_dir=tmp_path, source=SOURCE, settings=settings
        )


# ---------------------------------------------------------------------- retries

MIRROR_404 = (
    "ERROR: HTTP error 404 while getting https://mirror.example/pypi/download/librt/0.7.7/"
    "librt-0.7.7-cp312-cp312-manylinux_2_28_aarch64.whl.metadata\n"
)


def _result(exit_code: int, stderr: str = "", **flags: bool) -> SandboxRunResult:
    return SandboxRunResult(
        exit_code=exit_code,
        timed_out=flags.get("timed_out", False),
        oom_killed=flags.get("oom_killed", False),
        duration_s=0.1,
        stdout="",
        stderr=stderr,
    )


class ScriptedInstaller:
    """TEST DOUBLE: returns pre-set install results; only exercises the retry policy."""

    def __init__(self, *results: SandboxRunResult) -> None:
        self.results = list(results)
        self.calls = 0

    def install_dependencies(self, request: DependencyInstallRequest) -> SandboxRunResult:
        self.calls += 1
        assert request.target_dir.is_dir()  # a fresh staging dir for every attempt
        return self.results.pop(0)


@pytest.fixture
def quick_retries(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr(settings, "sandbox_install_retry_delay_seconds", 0.0)
    return settings


def _prepare(installer: ScriptedInstaller, settings: Settings, workspace: Path) -> Any:
    return prepare_environment(
        installer,  # type: ignore[arg-type]
        _grant(),
        workspace_dir=workspace,
        source=SOURCE,
        settings=settings,
    )


def test_mirror_404_is_retried_until_the_install_succeeds(
    quick_retries: Settings, workspace: Path
) -> None:
    installer = ScriptedInstaller(_result(1, MIRROR_404), _result(0))
    prepared = _prepare(installer, quick_retries, workspace)
    assert prepared.succeeded
    assert installer.calls == prepared.attempts == 2
    assert prepared.retry_reasons == [MIRROR_404.strip()[:300]]
    assert deps_dir_for(quick_retries, "snap-1").is_dir()
    meta = json.loads(
        (deps_dir_for(quick_retries, "snap-1").parent / "environment.json").read_text()
    )
    assert meta["attempts"] == 2


def test_retries_are_bounded(quick_retries: Settings, workspace: Path) -> None:
    installer = ScriptedInstaller(*[_result(1, MIRROR_404) for _ in range(5)])
    prepared = _prepare(installer, quick_retries, workspace)
    assert not prepared.succeeded
    assert installer.calls == prepared.attempts == quick_retries.sandbox_install_attempts == 3
    assert not list(deps_dir_for(quick_retries, "snap-1").parent.glob("deps.partial-*"))
    reason = install_failure_reason(prepared)
    assert reason.startswith("dependency installation failed in the sandbox after 3 attempts")
    assert "HTTP error 404" in reason
    assert "PERFPILOT_SANDBOX_PIP_INDEX_URL" in reason


@pytest.mark.parametrize(
    "result",
    [
        _result(1, "ERROR: No matching distribution found for nope==9.9\n"),
        _result(1, "ERROR: Could not install packages due to an OSError\n"),
        _result(None, MIRROR_404, timed_out=True),  # type: ignore[arg-type]
        _result(137, MIRROR_404, oom_killed=True),
    ],
)
def test_real_install_errors_are_not_retried(
    quick_retries: Settings, workspace: Path, result: SandboxRunResult
) -> None:
    installer = ScriptedInstaller(result, _result(0))
    prepared = _prepare(installer, quick_retries, workspace)
    assert not prepared.succeeded
    assert installer.calls == prepared.attempts == 1


def test_failure_reasons_are_specific() -> None:
    def reason(result: SandboxRunResult) -> str:
        prepared = PreparedEnvironment(
            snapshot_id="s",
            deps_dir=None,
            manifest=DependencyManifest(requirements=["x"]),
            allow_sdist_builds=False,
            result=result,
        )
        return install_failure_reason(prepared)

    assert "timed out" in reason(_result(None, "", timed_out=True))  # type: ignore[arg-type]
    assert "ran out of memory" in reason(_result(137, "", oom_killed=True))
    missing = reason(_result(1, "ERROR: No matching distribution found for nope==9.9\n"))
    assert "No matching distribution found for nope==9.9" in missing
    assert "allow source builds" in missing
