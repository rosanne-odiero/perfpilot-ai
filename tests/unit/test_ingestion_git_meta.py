from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.ingestion.git_meta import read_git_metadata

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

_ENV = {
    "PATH": os.environ.get("PATH", ""),
    "HOME": "/nonexistent",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "GIT_AUTHOR_DATE": "2024-05-01T12:00:00+00:00",
    "GIT_COMMITTER_DATE": "2024-05-01T12:00:00+00:00",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], env=_ENV, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "--quiet", "--initial-branch=trunk")
    (path / "app.py").write_text("x = 1\n")
    _git(path, "add", "app.py")
    _git(path, "commit", "--quiet", "-m", "init")
    _git(path, "remote", "add", "origin", "https://bob:ghp_secret123@github.com/o/r.git")
    return path


def test_metadata_of_clean_repository(repo: Path, tmp_path: Path) -> None:
    meta = read_git_metadata(repo, home=tmp_path)
    assert meta.commit_sha == _git(repo, "rev-parse", "HEAD")
    assert meta.branch == "trunk"
    assert meta.remote_url == "https://github.com/o/r.git"
    assert meta.commit_date is not None
    assert meta.commit_date.isoformat() == "2024-05-01T12:00:00+00:00"
    assert meta.is_dirty is False


def test_dirty_flag_and_detached_head(repo: Path, tmp_path: Path) -> None:
    (repo / "app.py").write_text("x = 2\n")
    assert read_git_metadata(repo, home=tmp_path).is_dirty is True
    _git(repo, "checkout", "--quiet", "--", "app.py")
    _git(repo, "checkout", "--quiet", "--detach", "HEAD")
    meta = read_git_metadata(repo, home=tmp_path)
    assert meta.branch is None
    assert meta.is_dirty is False


def test_untracked_files_do_not_make_repository_dirty(repo: Path, tmp_path: Path) -> None:
    (repo / "notes.txt").write_text("scratch")
    assert read_git_metadata(repo, home=tmp_path).is_dirty is False


def test_non_repository_and_subdirectory(tmp_path: Path, repo: Path) -> None:
    assert read_git_metadata(tmp_path / "missing", home=tmp_path).commit_sha is None
    sub = repo / "pkg"
    sub.mkdir()
    (sub / ".git").mkdir()
    assert read_git_metadata(sub, home=tmp_path).commit_sha is None


def test_repository_fsmonitor_hook_is_never_executed(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    hook = tmp_path / "evil.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(hook))
    (repo / "app.py").write_text("x = 3\n")
    _git(repo, "status", "--porcelain")
    assert marker.exists(), "sanity check: plain git runs the repository's fsmonitor hook"
    marker.unlink()
    meta = read_git_metadata(repo, home=tmp_path)
    assert meta.is_dirty is True
    assert not marker.exists()
