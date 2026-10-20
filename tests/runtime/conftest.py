"""Fixtures for Runtime Audit tests (test doubles live in ``tests/runtime_support.py``).

``@pytest.mark.docker`` tests in this directory use the real Docker sandbox backend and are skipped
with the reason when Docker or the sandbox image is unavailable.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import cache
from pathlib import Path

import pytest
from runtime_support import SANDBOX_IMAGE, BackendSetter, SubprocessHarnessBackend

from backend.core.config import Settings
from backend.runtime.backends import set_backend_factory
from backend.sandbox.backend import ExecutionBackend
from backend.sandbox.docker_backend import DockerSandboxBackend

_HERE = Path(__file__).parent


@cache
def docker_skip_reason() -> str | None:
    status = DockerSandboxBackend(Settings(sandbox_image=SANDBOX_IMAGE)).availability()
    return None if status.available else f"Docker sandbox unavailable: {status.reason}"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if item.get_closest_marker("docker") and item.path.is_relative_to(_HERE):
            reason = docker_skip_reason()
            if reason:
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture
def use_backend() -> Iterator[BackendSetter]:
    def install(backend: ExecutionBackend) -> ExecutionBackend:
        set_backend_factory(lambda _settings: backend)
        return backend

    yield install
    set_backend_factory(None)


@pytest.fixture
def harness_backend(tmp_path: Path, use_backend: BackendSetter) -> SubprocessHarnessBackend:
    backend = SubprocessHarnessBackend(scratch=tmp_path / "double-scratch")
    use_backend(backend)
    return backend
