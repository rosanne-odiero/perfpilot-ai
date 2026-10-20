"""Unit tests for DockerSandboxBackend: argv construction, path validation, availability and the
process handling (timeouts, kill, cleanup) using a docker CLI *test double*."""

from __future__ import annotations

import itertools
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from backend.core.config import Settings
from backend.core.exceptions import (
    NotConfiguredError,
    SecurityViolationError,
    UnavailableError,
    ValidationFailedError,
)
from backend.sandbox import docker_backend
from backend.sandbox.backend import (
    DependencyInstallRequest,
    ExecutionBackend,
    ReadOnlyMount,
    SandboxError,
    SandboxLimits,
    SandboxRunRequest,
    get_execution_backend,
)
from backend.sandbox.docker_backend import (
    DockerSandboxBackend,
    collect_output_files,
    find_docker_executable,
)

if TYPE_CHECKING:
    from conftest import FakeDocker, SandboxDirs


def _request(dirs: SandboxDirs, **overrides: object) -> SandboxRunRequest:
    data: dict[str, object] = {
        "workspace_dir": dirs.workspace,
        "argv": ["python", "main.py"],
        "output_dir": dirs.output,
    }
    data.update(overrides)
    return SandboxRunRequest.model_validate(data)


def _pairs(argv: list[str]) -> list[tuple[str, str]]:
    return list(itertools.pairwise(argv))


@pytest.fixture
def backend(settings: Settings, fake_docker: FakeDocker) -> DockerSandboxBackend:
    return DockerSandboxBackend(settings)


# ---------------------------------------------------------------------- argv construction


def test_run_argv_has_every_hardening_flag(
    backend: DockerSandboxBackend, sandbox_dirs: SandboxDirs, fake_docker: FakeDocker
) -> None:
    limits = SandboxLimits(
        cpus=1.5, memory_mb=512, pids=64, timeout_seconds=30, tmpfs_mb=32, output_max_bytes=4096
    )
    argv = backend.build_run_argv(_request(sandbox_dirs, limits=limits), container_name="c1")
    pairs = _pairs(argv)

    assert argv[0] == str(fake_docker.path)
    assert argv[1:4] == ["run", "--name", "c1"]
    assert "--rm" not in argv  # removed explicitly after inspect (OOM detection)
    for flag, value in [
        ("--network", "none"),
        ("--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=32m"),
        ("--cpus", "1.5"),
        ("--memory", "512m"),
        ("--memory-swap", "512m"),
        ("--pids-limit", "64"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--user", "65534:65534"),
        ("--ipc", "private"),
        ("--pull", "never"),
        ("--label", "perfpilot.sandbox=1"),
        ("--workdir", "/workspace"),
    ]:
        assert (flag, value) in pairs, flag
    assert "--read-only" in argv
    assert "--init" in argv
    assert "--privileged" not in argv
    ws = sandbox_dirs.workspace.resolve()
    out = sandbox_dirs.output.resolve()
    assert ("--mount", f"type=bind,source={ws},target=/workspace,readonly") in pairs
    assert ("--mount", f"type=bind,source={out},target=/out") in pairs
    assert argv[-3:] == ["perfpilot-sandbox:latest", "python", "main.py"]


def test_run_argv_env_is_fixed_plus_allowlisted(
    backend: DockerSandboxBackend, sandbox_dirs: SandboxDirs
) -> None:
    request = _request(sandbox_dirs, env={"PYTHONHASHSEED": "0", "PERFPILOT_HARNESS_MODE": "cpu"})
    argv = backend.build_run_argv(request, container_name="c1")
    envs = [value for flag, value in _pairs(argv) if flag == "--env"]
    assert envs == [
        "HOME=/tmp",
        "TMPDIR=/tmp",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONUNBUFFERED=1",
        "PYTHONHASHSEED=0",
        "PERFPILOT_HARNESS_MODE=cpu",
    ]


@pytest.mark.parametrize(
    "env",
    [
        {"PATH": "/evil"},
        {"PYTHONPATH": "/tmp"},
        {"LD_PRELOAD": "/x.so"},
        {"PERFPILOT_LLM_API_KEY": "abc"},
        {"OPENAI_API_KEY": "sk-abcdefghijklmnopqrstuvwxyz123456"},
        {"lower": "x"},
        {"PYTHONWARNINGS": "a\nb"},
        {"PERFPILOT_HARNESS_TOKEN": "ghp_" + "B" * 36},
    ],
)
def test_env_outside_allowlist_or_secret_is_rejected(env: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        SandboxRunRequest(workspace_dir=Path("/w"), argv=["python"], output_dir=Path("/o"), env=env)


@pytest.mark.parametrize(
    "argv",
    [[], ["python", "a\0b"], ["-v"], ["python", "x" * 5000], ["python"] + ["a"] * 300],
)
def test_bad_argv_structure_is_rejected(argv: list[str]) -> None:
    with pytest.raises(ValidationError):
        SandboxRunRequest(workspace_dir=Path("/w"), argv=argv, output_dir=Path("/o"))


@pytest.mark.parametrize("image", ["--privileged", "Bad Image", "img;rm", ""])
def test_bad_image_reference_is_rejected(image: str) -> None:
    with pytest.raises(ValidationError):
        SandboxRunRequest(
            workspace_dir=Path("/w"), argv=["python"], output_dir=Path("/o"), image=image
        )


def test_network_other_than_none_is_rejected() -> None:
    with pytest.raises(ValidationError):
        SandboxRunRequest.model_validate(
            {"workspace_dir": "/w", "argv": ["python"], "output_dir": "/o", "network": "bridge"}
        )


@pytest.mark.parametrize("workdir", ["../x", "/abs", "a/../../b", "a\\b", 'a"b'])
def test_bad_workdir_is_rejected(workdir: str) -> None:
    with pytest.raises(ValidationError):
        SandboxRunRequest(
            workspace_dir=Path("/w"), argv=["python"], output_dir=Path("/o"), workdir_rel=workdir
        )


def test_workdir_subdirectory(backend: DockerSandboxBackend, sandbox_dirs: SandboxDirs) -> None:
    argv = backend.build_run_argv(_request(sandbox_dirs, workdir_rel="pkg"), container_name="c")
    assert ("--workdir", "/workspace/pkg") in _pairs(argv)


def test_workdir_symlink_escape_is_rejected(
    backend: DockerSandboxBackend, sandbox_dirs: SandboxDirs, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (sandbox_dirs.workspace / "link").symlink_to(outside)
    with pytest.raises(SecurityViolationError):
        backend.build_run_argv(_request(sandbox_dirs, workdir_rel="link"), container_name="c")


def test_workspace_outside_workspaces_dir_is_rejected(
    backend: DockerSandboxBackend, sandbox_dirs: SandboxDirs, tmp_path: Path
) -> None:
    rogue = tmp_path / "rogue"
    rogue.mkdir()
    with pytest.raises(SecurityViolationError):
        backend.build_run_argv(_request(sandbox_dirs, workspace_dir=rogue), container_name="c")


def test_workspace_symlink_into_allowed_root_is_rejected(
    backend: DockerSandboxBackend, settings: Settings, sandbox_dirs: SandboxDirs, tmp_path: Path
) -> None:
    link = settings.workspaces_dir / "snap-link"
    link.symlink_to(tmp_path)
    with pytest.raises(SecurityViolationError):
        backend.build_run_argv(_request(sandbox_dirs, workspace_dir=link), container_name="c")


def test_workspace_root_itself_and_relative_paths_are_rejected(
    backend: DockerSandboxBackend, settings: Settings, sandbox_dirs: SandboxDirs
) -> None:
    with pytest.raises(SecurityViolationError):
        backend.build_run_argv(
            _request(sandbox_dirs, workspace_dir=settings.workspaces_dir), container_name="c"
        )
    with pytest.raises(SecurityViolationError):
        backend.build_run_argv(
            _request(sandbox_dirs, workspace_dir=Path("relative/repo")), container_name="c"
        )


def test_output_dir_must_be_separate_from_workspace(
    backend: DockerSandboxBackend, sandbox_dirs: SandboxDirs
) -> None:
    inside = sandbox_dirs.workspace / "out"
    inside.mkdir()
    with pytest.raises(SecurityViolationError):
        backend.build_run_argv(_request(sandbox_dirs, output_dir=inside), container_name="c")


def test_missing_paths_are_validation_errors(
    backend: DockerSandboxBackend, settings: Settings, sandbox_dirs: SandboxDirs
) -> None:
    with pytest.raises(ValidationFailedError):
        backend.build_run_argv(
            _request(sandbox_dirs, output_dir=settings.artifacts_dir / "missing"),
            container_name="c",
        )


def test_mount_paths_with_csv_breaking_characters_are_rejected(
    backend: DockerSandboxBackend, settings: Settings, sandbox_dirs: SandboxDirs
) -> None:
    odd = settings.workspaces_dir / "snap,2"
    odd.mkdir()
    with pytest.raises(SecurityViolationError):
        backend.build_run_argv(_request(sandbox_dirs, workspace_dir=odd), container_name="c")


def test_extra_read_only_mount_for_harness(
    backend: DockerSandboxBackend, settings: Settings, sandbox_dirs: SandboxDirs
) -> None:
    harness = Path(docker_backend.__file__).resolve().parents[1] / "sandbox" / "backend.py"
    request_file = settings.artifacts_dir / "runs" / "req.json"
    request_file.write_text("{}")
    request = _request(
        sandbox_dirs,
        extra_mounts=[
            ReadOnlyMount(source=harness, target="/perfpilot/harness_entry.py"),
            ReadOnlyMount(source=request_file, target="/perfpilot/request.json"),
        ],
    )
    pairs = _pairs(backend.build_run_argv(request, container_name="c"))
    assert (
        "--mount",
        f"type=bind,source={harness},target=/perfpilot/harness_entry.py,readonly",
    ) in (pairs)
    assert (
        "--mount",
        f"type=bind,source={request_file.resolve()},target=/perfpilot/request.json,readonly",
    ) in pairs


def test_extra_mount_outside_allowed_roots_is_rejected(
    backend: DockerSandboxBackend, sandbox_dirs: SandboxDirs, tmp_path: Path
) -> None:
    secret = tmp_path / "id_rsa"
    secret.write_text("key")
    request = _request(
        sandbox_dirs, extra_mounts=[ReadOnlyMount(source=secret, target="/perfpilot/x")]
    )
    with pytest.raises(SecurityViolationError):
        backend.build_run_argv(request, container_name="c")


@pytest.mark.parametrize(
    "target", ["/workspace/x", "/perfpilot", "/perfpilot/../etc", "perfpilot/x", "/perfpilot/a b"]
)
def test_extra_mount_target_must_be_under_perfpilot(target: str) -> None:
    with pytest.raises(ValidationError):
        ReadOnlyMount(source=Path("/x"), target=target)


def test_deps_dir_is_mounted_read_only_with_pythonpath(
    backend: DockerSandboxBackend, settings: Settings, sandbox_dirs: SandboxDirs
) -> None:
    deps = settings.workspaces_dir / "runtime-envs" / "snap-1" / "deps"
    deps.mkdir(parents=True)
    argv = backend.build_run_argv(_request(sandbox_dirs, deps_dir=deps), container_name="c")
    pairs = _pairs(argv)
    assert ("--mount", f"type=bind,source={deps.resolve()},target=/deps,readonly") in pairs
    assert ("--env", "PYTHONPATH=/deps") in pairs


def test_install_argv_uses_network_but_wheels_only(
    backend: DockerSandboxBackend, settings: Settings
) -> None:
    root = settings.workspaces_dir / "runtime-envs" / "snap-1"
    target = root / "deps.partial"
    target.mkdir(parents=True)
    manifest = root / "requirements.txt"
    manifest.write_text("requests==2.32.0\n")
    request = DependencyInstallRequest(
        manifest_file=manifest,
        target_dir=target,
        limits=SandboxLimits.from_settings(settings),
        index_url="https://pypi.example/simple",
    )
    argv = backend.build_install_argv(request, container_name="i1")
    pairs = _pairs(argv)
    assert ("--network", "bridge") in pairs
    assert ("--user", "65534:65534") in pairs
    assert ("--cap-drop", "ALL") in pairs
    assert "--read-only" in argv
    assert "--only-binary=:all:" in argv
    assert "--prefer-binary" not in argv
    assert ("--index-url", "https://pypi.example/simple") in pairs
    assert argv[-2:] == ["--requirement", "/perfpilot/requirements.txt"]
    assert "target=/workspace" not in " ".join(argv)  # the repository is not mounted

    sdist = request.model_copy(update={"allow_sdists": True})
    assert "--prefer-binary" in backend.build_install_argv(sdist, container_name="i2")


@pytest.mark.parametrize(
    "url", ["https://user:pw@pypi.example/simple", "file:///etc", "ftp://x", "https://x/ y"]
)
def test_install_index_url_validation(url: str, settings: Settings) -> None:
    with pytest.raises(ValidationError):
        DependencyInstallRequest(
            manifest_file=Path("/m"),
            target_dir=Path("/t"),
            limits=SandboxLimits.from_settings(settings),
            index_url=url,
        )


# ---------------------------------------------------------------------- availability


def test_availability_ok_with_test_double(backend: DockerSandboxBackend) -> None:
    status = backend.availability()
    assert status.available, status.reason
    assert status.details["image_id"].startswith("sha256:")
    assert isinstance(backend, ExecutionBackend)


def test_availability_daemon_down(backend: DockerSandboxBackend, fake_docker: FakeDocker) -> None:
    fake_docker.set_mode(info="down")
    status = backend.availability()
    assert not status.available
    assert "daemon not reachable" in (status.reason or "")


def test_availability_image_missing(backend: DockerSandboxBackend, fake_docker: FakeDocker) -> None:
    fake_docker.set_mode(image_present=False)
    status = backend.availability()
    assert not status.available
    assert "make sandbox-image" in (status.reason or "")


def test_availability_daemon_hangs(
    backend: DockerSandboxBackend, fake_docker: FakeDocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(docker_backend, "_INFO_TIMEOUT", 0.5)
    fake_docker.set_mode(info="hang")
    status = backend.availability()
    assert not status.available
    assert "did not respond" in (status.reason or "")


def test_docker_missing_disables_runtime_without_fallback(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(docker_backend, "FIXED_DOCKER_DIRS", ())
    assert find_docker_executable(settings) is None
    status = DockerSandboxBackend(settings).availability()
    assert not status.available
    assert "docker CLI not found" in (status.reason or "")
    with pytest.raises(UnavailableError) as excinfo:
        get_execution_backend(settings)
    assert "runtime mode is disabled" in excinfo.value.message
    with pytest.raises(SandboxError):
        DockerSandboxBackend(settings).build_run_argv(
            SandboxRunRequest(workspace_dir=Path("/w"), argv=["python"], output_dir=Path("/o")),
            container_name="c",
        )


def test_explicit_docker_path_must_be_executable(settings: Settings, tmp_path: Path) -> None:
    not_exec = tmp_path / "docker"
    not_exec.write_text("")
    settings.sandbox_docker_path = not_exec
    assert find_docker_executable(settings) is None


def test_unknown_backend_is_not_configured(settings: Settings) -> None:
    settings.sandbox_backend = "host"
    with pytest.raises(NotConfiguredError):
        get_execution_backend(settings)


def test_factory_returns_available_backend(settings: Settings, fake_docker: FakeDocker) -> None:
    assert get_execution_backend(settings).name == "docker"


# ---------------------------------------------------------------------- process handling


def test_run_collects_inspect_state_and_redacts_output(
    backend: DockerSandboxBackend, sandbox_dirs: SandboxDirs, fake_docker: FakeDocker
) -> None:
    fake_docker.set_mode(run="echo", exit_code=3)
    result = backend.run(_request(sandbox_dirs))
    assert result.exit_code == 3
    assert not result.timed_out
    assert not result.oom_killed
    assert not result.succeeded
    assert "hello from the test double" in result.stdout
    assert "ghp_" not in result.stdout
    assert "[REDACTED]" in result.stdout
    assert result.redactions == 1
    assert result.stderr.strip() == "stderr line"
    assert result.image_digest == "sha256:" + "b" * 64
    assert result.container_id
    assert result.container_id.startswith("c0ffee")
    name = next(c[2] for c in fake_docker.calls() if c[0] == "run")
    assert fake_docker.removed() == [name]
    assert ["container", "inspect", "--format", "{{json .}}", name] in fake_docker.calls()


def test_run_reports_oom_flag_from_inspect(
    backend: DockerSandboxBackend, sandbox_dirs: SandboxDirs, fake_docker: FakeDocker
) -> None:
    fake_docker.set_mode(run="echo", exit_code=137, oom=True)
    result = backend.run(_request(sandbox_dirs))
    assert result.oom_killed
    assert result.exit_code == 137


def test_timeout_kills_container_and_cleans_up(
    backend: DockerSandboxBackend,
    settings: Settings,
    sandbox_dirs: SandboxDirs,
    fake_docker: FakeDocker,
) -> None:
    fake_docker.set_mode(run="sleep")
    limits = SandboxLimits.from_settings(settings).model_copy(update={"timeout_seconds": 1})
    started = time.monotonic()
    result = backend.run(_request(sandbox_dirs, limits=limits))
    elapsed = time.monotonic() - started
    assert result.timed_out
    assert result.exit_code == 137
    assert elapsed < 10
    name = next(c[2] for c in fake_docker.calls() if c[0] == "run")
    assert ["kill", name] in fake_docker.calls()
    assert fake_docker.removed() == [name]


def test_timeout_when_docker_kill_is_ineffective_kills_client(
    backend: DockerSandboxBackend,
    settings: Settings,
    sandbox_dirs: SandboxDirs,
    fake_docker: FakeDocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker_backend, "_KILL_GRACE_SECONDS", 0.5)
    fake_docker.set_mode(run="sleep", kill="ineffective")
    limits = SandboxLimits.from_settings(settings).model_copy(update={"timeout_seconds": 1})
    started = time.monotonic()
    result = backend.run(_request(sandbox_dirs, limits=limits))
    assert result.timed_out
    assert time.monotonic() - started < 10
    assert len(fake_docker.removed()) == 1


def test_output_is_truncated_at_limit(
    backend: DockerSandboxBackend,
    settings: Settings,
    sandbox_dirs: SandboxDirs,
    fake_docker: FakeDocker,
) -> None:
    fake_docker.set_mode(run="flood")
    limits = SandboxLimits.from_settings(settings).model_copy(update={"output_max_bytes": 10_000})
    result = backend.run(_request(sandbox_dirs, limits=limits))
    assert result.stdout_truncated
    assert len(result.stdout) == 10_000
    assert result.exit_code == 0


def test_container_that_never_starts_raises(
    backend: DockerSandboxBackend, sandbox_dirs: SandboxDirs, fake_docker: FakeDocker
) -> None:
    fake_docker.set_mode(run="fail_create")
    with pytest.raises(SandboxError) as excinfo:
        backend.run(_request(sandbox_dirs))
    assert excinfo.value.details["docker_exit_code"] == 125
    assert len(fake_docker.removed()) == 1


def test_output_dir_must_be_empty(
    backend: DockerSandboxBackend, sandbox_dirs: SandboxDirs, fake_docker: FakeDocker
) -> None:
    (sandbox_dirs.output / "stale.json").write_text("{}")
    with pytest.raises(ValidationFailedError):
        backend.run(_request(sandbox_dirs))
    assert not [c for c in fake_docker.calls() if c[0] == "run"]


def test_docker_cli_env_does_not_leak_worker_secrets(
    backend: DockerSandboxBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PERFPILOT_LLM_API_KEY", "sk-secret-value-123456789012345")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    env = backend._cli_env()
    assert "PERFPILOT_LLM_API_KEY" not in env
    assert env["DOCKER_HOST"] == "unix:///var/run/docker.sock"


# ---------------------------------------------------------------------- output collection


def test_collect_output_files_rejects_symlinks_specials_and_oversize(tmp_path: Path) -> None:
    out = tmp_path / "out"
    (out / "sub").mkdir(parents=True)
    (out / "result.json").write_text("{}")
    (out / "sub" / "profile.prof").write_bytes(b"x" * 100)
    (out / "big.bin").write_bytes(b"x" * 5000)
    (out / "passwd").symlink_to("/etc/passwd")
    (out / "dirlink").symlink_to(tmp_path)
    os.mkfifo(out / "fifo")

    accepted, rejected = collect_output_files(out, max_bytes=1000)

    assert accepted == ["result.json", "sub/profile.prof"]
    assert set(rejected) == {"big.bin", "passwd", "dirlink", "fifo"}
    assert not (out / "passwd").is_symlink()
    assert not (out / "dirlink").exists()
    assert (tmp_path / "out").exists()
