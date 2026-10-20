"""The argv policy for runnable commands: an allowlist, confined to the workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.exceptions import SecurityViolationError, ValidationFailedError
from backend.sandbox.command_discovery import CommandRejectedError, validate_command_argv


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    for rel in (
        "main.py",
        "tests/test_a.py",
        "pkg/__init__.py",
        "pkg/__main__.py",
        "lib/__init__.py",
        "tool.py",
        "notes.txt",
        "sub/tests/test_s.py",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_x.py").write_text("")
    (root / "link_out").symlink_to(outside, target_is_directory=True)
    (root / "evil.py").symlink_to(outside / "test_x.py")
    return root


ALLOWED = [
    (["python", "-m", "pytest"], "."),
    (["python", "-m", "pytest", "-q", "-x"], "."),
    (["python", "-m", "pytest", "-q", "tests"], "."),
    (["python", "-m", "pytest", "tests/test_a.py::TestX::test_y[param-1]"], "."),
    (["python", "-m", "pytest", "-k", "not slow and (fast or quick)", "tests"], "."),
    (["python", "-m", "pytest", "-q", "tests"], "sub"),
    (["python", "main.py"], "."),
    (["python", "-m", "pkg"], "."),
    (["python", "-m", "tool"], "."),
]

REJECTED = [
    (["pytest"], "."),
    (["python3", "main.py"], "."),
    (["/usr/bin/python", "main.py"], "."),
    (["python", "-c", "print(1)"], "."),
    (["python", "-I", "main.py"], "."),
    (["python", "-m", "pip", "install", "x"], "."),
    (["python", "-m", "pytest", "-p", "evil"], "."),
    (["python", "-m", "pytest", "-pevil"], "."),
    (["python", "-m", "pytest", "--rootdir=/"], "."),
    (["python", "-m", "pytest", "--rootdir", "/"], "."),
    (["python", "-m", "pytest", "-c", "/etc/pytest.ini"], "."),
    (["python", "-m", "pytest", "-o", "addopts=-p evil"], "."),
    (["python", "-m", "pytest", "--pyargs", "os"], "."),
    (["python", "-m", "pytest", "--confcutdir", "/"], "."),
    (["python", "-m", "pytest", "-W", "error"], "."),
    (["python", "-m", "pytest", "-k"], "."),
    (["python", "-m", "pytest", "-k", "-p evil"], "."),
    (["python", "-m", "pytest", "-k", "a; import os"], "."),
    (["python", "-m", "pytest", "tests/test_a.py::bad id;rm"], "."),
    (["python", "-m", "pytest", "missing_tests"], "."),
    (["python", "main.py", "--flag"], "."),
    (["python", "missing.py"], "."),
    (["python", "notes.txt"], "."),
    (["python", "-m", "lib"], "."),
    (["python", "-m", "http.server"], "."),
    (["python", "-m", "os"], "."),
    (["python", "-m", "import"], "."),
    (["python", "-m", "a-b"], "."),
    (["python"], "."),
    ([], "."),
    (["python", "-m", "pytest", "-q"], "nope"),
]

ESCAPES = [
    (["python", "-m", "pytest", "../outside"], "."),
    (["python", "-m", "pytest", "/etc"], "."),
    (["python", "/abs/script.py"], "."),
    (["python", "../x.py"], "."),
    (["python", "-m", "pytest", "link_out/test_x.py"], "."),
    (["python", "evil.py"], "."),
    (["python", "-m", "pytest", "-q"], "../"),
    (["python", "-m", "pytest", "-q"], "link_out"),
]


@pytest.mark.parametrize(("argv", "workdir"), ALLOWED)
def test_allowed_argv(workspace: Path, argv: list[str], workdir: str) -> None:
    validate_command_argv(argv, workspace_dir=workspace, workdir_rel=workdir)


@pytest.mark.parametrize(("argv", "workdir"), REJECTED)
def test_rejected_argv(workspace: Path, argv: list[str], workdir: str) -> None:
    with pytest.raises(ValidationFailedError):
        validate_command_argv(argv, workspace_dir=workspace, workdir_rel=workdir)


@pytest.mark.parametrize(("argv", "workdir"), ESCAPES)
def test_workspace_escapes_are_security_violations(
    workspace: Path, argv: list[str], workdir: str
) -> None:
    with pytest.raises(SecurityViolationError):
        validate_command_argv(argv, workspace_dir=workspace, workdir_rel=workdir)


def test_rejection_type_is_specific(workspace: Path) -> None:
    with pytest.raises(CommandRejectedError) as excinfo:
        validate_command_argv(
            ["python", "-m", "pytest", "-p", "x"], workspace_dir=workspace, workdir_rel="."
        )
    assert "-p" in excinfo.value.message
