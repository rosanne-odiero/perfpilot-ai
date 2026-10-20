"""Integration tests against a real Docker daemon and the ``perfpilot-sandbox:latest`` image.

Skipped automatically (see conftest) when Docker or the image is unavailable. Build the image with
``make sandbox-image`` and run ``make test-docker``.
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from backend.core.config import Settings
from backend.sandbox.approvals import DependencyInstallGrant
from backend.sandbox.backend import (
    ReadOnlyMount,
    SandboxLimits,
    SandboxRunRequest,
    SandboxRunResult,
)
from backend.sandbox.docker_backend import CONTAINER_LABEL, DockerSandboxBackend
from backend.sandbox.environment import RequirementsSource, prepare_environment

if TYPE_CHECKING:
    from conftest import SandboxDirs

pytestmark = pytest.mark.docker


def _run(
    settings: Settings,
    dirs: SandboxDirs,
    code: str,
    *,
    limits: dict[str, object] | None = None,
    **request: object,
) -> SandboxRunResult:
    backend = DockerSandboxBackend(settings)
    base = SandboxLimits.from_settings(settings).model_copy(
        update={"timeout_seconds": 60, "memory_mb": 256, **(limits or {})}
    )
    return backend.run(
        SandboxRunRequest(
            workspace_dir=dirs.workspace,
            argv=["python", "-c", textwrap.dedent(code)],
            output_dir=dirs.output,
            limits=base,
            **request,  # type: ignore[arg-type]
        )
    )


def _leftover_containers(settings: Settings) -> list[str]:
    backend = DockerSandboxBackend(settings)
    result = backend._cli(
        ["ps", "--all", "--quiet", "--filter", f"label={CONTAINER_LABEL}"], timeout=30
    )
    return result.stdout.split()


def test_runs_as_non_root_without_capabilities(
    settings: Settings, sandbox_dirs: SandboxDirs
) -> None:
    before = _leftover_containers(settings)
    result = _run(
        settings,
        sandbox_dirs,
        """
        import os
        status = dict(line.split(":", 1) for line in open("/proc/self/status"))
        print(os.getuid(), os.getgid(), status["CapEff"].strip(), status["NoNewPrivs"].strip())
        """,
    )
    assert result.exit_code == 0, result.stderr
    assert result.stdout.split() == ["65534", "65534", "0000000000000000", "1"]
    assert result.image_digest
    assert result.image_digest.startswith("sha256:")
    assert _leftover_containers(settings) == before


def test_network_is_blocked(settings: Settings, sandbox_dirs: SandboxDirs) -> None:
    result = _run(
        settings,
        sandbox_dirs,
        """
        import socket
        interfaces = sorted(l.split(":")[0].strip() for l in open("/proc/net/dev").readlines()[2:])
        print("IFACES", ",".join(interfaces))
        for target in [("1.1.1.1", 53), ("8.8.8.8", 443)]:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3)
            try:
                s.connect(target)
                print("CONNECTED", target)
            except OSError as exc:
                print("BLOCKED", type(exc).__name__)
        try:
            socket.getaddrinfo("pypi.org", 443)
            print("DNS_OK")
        except OSError:
            print("DNS_BLOCKED")
        """,
    )
    assert result.exit_code == 0, result.stderr
    assert "IFACES lo" in result.stdout
    assert "CONNECTED" not in result.stdout
    assert result.stdout.count("BLOCKED") == 3
    assert "DNS_BLOCKED" in result.stdout


def test_filesystem_is_read_only_except_out_and_tmp(
    settings: Settings, sandbox_dirs: SandboxDirs
) -> None:
    result = _run(
        settings,
        sandbox_dirs,
        """
        import os, subprocess
        for path in ["/workspace/main.py", "/workspace/new.txt", "/etc/evil", "/usr/evil", "/evil"]:
            try:
                with open(path, "w") as fh:
                    fh.write("x")
                print("WROTE", path)
            except OSError:
                print("DENIED", path)
        with open("/tmp/scratch.txt", "w") as fh:
            fh.write("ok")
        with open("/out/result.json", "w") as fh:
            fh.write('{"ok": true}')
        with open("/tmp/run.sh", "w") as fh:
            fh.write("#!/bin/sh\\necho EXEC_OK\\n")
        os.chmod("/tmp/run.sh", 0o755)
        try:
            subprocess.run(["/tmp/run.sh"], check=True)
        except OSError:
            print("NOEXEC_TMP")
        """,
    )
    assert result.exit_code == 0, result.stderr
    assert "WROTE" not in result.stdout
    assert result.stdout.count("DENIED") == 5
    assert "NOEXEC_TMP" in result.stdout
    assert "EXEC_OK" not in result.stdout
    assert result.output_files == ["result.json"]
    assert (sandbox_dirs.output / "result.json").read_text() == '{"ok": true}'
    assert (sandbox_dirs.workspace / "main.py").read_text() == "print('hi')\n"
    assert not (sandbox_dirs.workspace / "new.txt").exists()


def test_memory_limit_triggers_oom_kill(settings: Settings, sandbox_dirs: SandboxDirs) -> None:
    result = _run(
        settings,
        sandbox_dirs,
        """
        chunks = []
        for _ in range(100):
            chunks.append(b"x" * (16 * 1024 * 1024))
        print("SURVIVED", len(chunks))
        """,
        limits={"memory_mb": 64},
    )
    assert "SURVIVED" not in result.stdout
    assert result.oom_killed
    assert result.exit_code == 137
    assert not result.timed_out


def test_wall_clock_timeout_kills_the_container(
    settings: Settings, sandbox_dirs: SandboxDirs
) -> None:
    before = _leftover_containers(settings)
    result = _run(
        settings,
        sandbox_dirs,
        "import time\nprint('START', flush=True)\ntime.sleep(120)\nprint('END')\n",
        limits={"timeout_seconds": 3},
    )
    assert result.timed_out
    assert result.exit_code == 137
    assert "START" in result.stdout
    assert "END" not in result.stdout
    assert result.duration_s < 30
    assert _leftover_containers(settings) == before


def test_pids_limit_caps_process_creation(settings: Settings, sandbox_dirs: SandboxDirs) -> None:
    result = _run(
        settings,
        sandbox_dirs,
        """
        import threading, time
        threads = []
        try:
            for _ in range(500):
                t = threading.Thread(target=time.sleep, args=(5,), daemon=True)
                t.start()
                threads.append(t)
            print("STARTED", len(threads))
        except RuntimeError:
            print("CAPPED", len(threads))
        """,
        limits={"pids": 32},
    )
    assert result.exit_code == 0, result.stderr
    assert "CAPPED" in result.stdout
    assert int(result.stdout.split()[-1]) < 32


def test_extra_mount_and_deps_are_read_only(settings: Settings, sandbox_dirs: SandboxDirs) -> None:
    deps = settings.workspaces_dir / "runtime-envs" / "snap-1" / "deps"
    deps.mkdir(parents=True)
    (deps / "vendored_dep.py").write_text("VALUE = 42\n")
    harness = settings.artifacts_dir / "harness_entry.py"
    harness.write_text("print('HARNESS')\n")
    result = _run(
        settings,
        sandbox_dirs,
        """
        import runpy, vendored_dep
        print("DEP", vendored_dep.VALUE)
        runpy.run_path("/perfpilot/harness_entry.py")
        for path in ["/perfpilot/harness_entry.py", "/deps/vendored_dep.py"]:
            try:
                open(path, "a").write("x")
                print("WROTE", path)
            except OSError:
                print("DENIED", path)
        """,
        deps_dir=deps,
        extra_mounts=[ReadOnlyMount(source=harness, target="/perfpilot/harness_entry.py")],
    )
    assert result.exit_code == 0, result.stderr
    assert "DEP 42" in result.stdout
    assert "HARNESS" in result.stdout
    assert result.stdout.count("DENIED") == 2


@pytest.mark.network
@pytest.mark.skipif(
    os.environ.get("PERFPILOT_RUN_NETWORK_TESTS") != "1",
    reason="needs outbound access to a package index (set PERFPILOT_RUN_NETWORK_TESTS=1)",
)
def test_prepare_environment_installs_wheels_for_offline_runs(
    settings: Settings, sandbox_dirs: SandboxDirs
) -> None:
    (sandbox_dirs.workspace / "requirements.txt").write_text("six==1.16.0\n")
    grant = DependencyInstallGrant(
        approval_id="a1",
        snapshot_id="snap-1",
        image=settings.sandbox_image,
        allow_sdist_builds=False,
    )
    prepared = prepare_environment(
        DockerSandboxBackend(settings),
        grant,
        workspace_dir=sandbox_dirs.workspace,
        source=RequirementsSource(kind="requirements_file", path_rel="requirements.txt"),
        settings=settings,
    )
    assert prepared.succeeded, prepared.result.stderr
    assert prepared.deps_dir is not None
    result = _run(
        settings,
        sandbox_dirs,
        "import six\nprint('SIX', six.__version__)\n",
        deps_dir=Path(prepared.deps_dir),
    )
    assert "SIX 1.16.0" in result.stdout
