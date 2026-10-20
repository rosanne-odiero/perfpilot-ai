"""Fixtures for sandbox tests.

* ``@pytest.mark.docker`` tests in this directory are skipped (with the reason) unless a Docker
  daemon is reachable **and** the ``perfpilot-sandbox:latest`` image exists. Building the image is
  too slow for a test run; use ``make sandbox-image`` first.
* ``fake_docker`` installs a *test double* of the docker CLI on PATH. It only exercises PerfPilot's
  own process handling (timeouts, kill, inspect, cleanup); it never stands in for real sandbox
  results, which are covered by the docker-marked integration tests.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from backend.core.config import Settings
from backend.sandbox.docker_backend import DockerSandboxBackend

SANDBOX_IMAGE = "perfpilot-sandbox:latest"
_HERE = Path(__file__).parent


@cache
def docker_skip_reason() -> str | None:
    """Why docker-marked tests cannot run here, or ``None`` when they can."""
    status = DockerSandboxBackend(Settings(sandbox_image=SANDBOX_IMAGE)).availability()
    if status.available:
        return None
    return f"Docker sandbox unavailable: {status.reason}"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if item.get_closest_marker("docker") and item.path.is_relative_to(_HERE):
            reason = docker_skip_reason()
            if reason:
                item.add_marker(pytest.mark.skip(reason=reason))


@dataclass
class SandboxDirs:
    workspace: Path
    output: Path


@pytest.fixture
def sandbox_dirs(settings: Settings) -> SandboxDirs:
    workspace = settings.workspaces_dir / "snap-1" / "repo"
    (workspace / "pkg").mkdir(parents=True)
    (workspace / "pkg" / "__init__.py").write_text("")
    (workspace / "main.py").write_text("print('hi')\n")
    output = settings.artifacts_dir / "runs" / "run-1"
    output.mkdir(parents=True)
    return SandboxDirs(workspace=workspace, output=output)


_FAKE_DOCKER = r"""
import json, os, signal, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
STATE.mkdir(exist_ok=True)
MODE = json.loads((HERE / "mode.json").read_text())
args = sys.argv[1:]
with (STATE / "calls.jsonl").open("a") as fh:
    fh.write(json.dumps(args) + "\n")

def state_file(name):
    return STATE / f"{name}.json"

if args[0] == "info":
    if MODE.get("info") == "down":
        print("Cannot connect to the Docker daemon at unix:///var/run/docker.sock", file=sys.stderr)
        sys.exit(1)
    if MODE.get("info") == "server_errors":
        print(json.dumps({"ServerVersion": "", "ServerErrors": ["Cannot connect to the daemon"]}))
        sys.exit(0)
    if MODE.get("info") == "hang":
        time.sleep(60)
    info = {"ServerVersion": "test-double", "OperatingSystem": "fake", "CgroupVersion": "2"}
    print(json.dumps(info))
elif args[:2] == ["image", "inspect"]:
    if not MODE.get("image_present", True):
        print("Error: No such image", file=sys.stderr)
        sys.exit(1)
    print("sha256:" + "a" * 64)
elif args[0] == "run":
    name = args[args.index("--name") + 1]
    behaviour = MODE.get("run", "echo")
    if behaviour == "fail_create":
        print("docker: Error response from daemon: image not found.", file=sys.stderr)
        sys.exit(125)
    record = {"pid": os.getpid(), "State": {"ExitCode": MODE.get("exit_code", 0),
              "OOMKilled": MODE.get("oom", False)}}
    state_file(name).write_text(json.dumps(record))
    if behaviour == "sleep":
        time.sleep(600)
    elif behaviour == "flood":
        sys.stdout.write("x" * 300_000)
    else:
        print("hello from the test double")
        print("token ghp_" + "A" * 36)
        print("stderr line", file=sys.stderr)
    sys.exit(MODE.get("exit_code", 0))
elif args[0] == "kill":
    path = state_file(args[1])
    if MODE.get("kill") != "ineffective" and path.exists():
        record = json.loads(path.read_text())
        record["State"]["ExitCode"] = 137
        path.write_text(json.dumps(record))
        try:
            os.kill(record["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
elif args[:2] == ["container", "inspect"]:
    path = state_file(args[-1])
    if not path.exists():
        print("Error: No such container", file=sys.stderr)
        sys.exit(1)
    record = json.loads(path.read_text())
    print(json.dumps({"Id": "c0ffee" + args[-1][-6:], "Image": "sha256:" + "b" * 64,
                      "State": record["State"]}))
elif args[0] == "rm":
    with (STATE / "removed.txt").open("a") as fh:
        fh.write(args[-1] + "\n")
"""


class FakeDocker:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "docker"
        self.path.write_text(f"#!{sys.executable}\n{_FAKE_DOCKER}")
        self.path.chmod(self.path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self.set_mode()

    def set_mode(self, **mode: Any) -> None:
        (self.root / "mode.json").write_text(json.dumps(mode))

    def calls(self) -> list[list[str]]:
        log = self.root / "state" / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def removed(self) -> list[str]:
        path = self.root / "state" / "removed.txt"
        return path.read_text().split() if path.exists() else []


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    """Test double of the docker CLI placed first on PATH (process-handling tests only)."""
    root = tmp_path / "fake-docker-bin"
    root.mkdir()
    fake = FakeDocker(root)
    monkeypatch.setenv("PATH", f"{root}{os.pathsep}{os.environ.get('PATH', '')}")
    return fake
