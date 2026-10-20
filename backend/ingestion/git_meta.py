"""Safe execution of the ``git`` executable and repository metadata capture.

Git always runs with a fixed executable, an argv list (never a shell), a timeout that kills the
whole process group, a minimal environment that ignores system/global config and disables every
credential prompt, and ``-c`` overrides that neutralize hooks, fsmonitor and non-HTTPS transports.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from backend.core.exceptions import UnavailableError
from backend.core.redaction import redact_text
from backend.ingestion.errors import GitCommandError
from backend.models.audit_schemas import GitMetadata

SAFE_GIT_CONFIG: tuple[str, ...] = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "protocol.allow=never",
    "-c", "protocol.https.allow=always",
    "-c", "core.symlinks=false",
    "-c", "core.fsmonitor=false",
    "-c", "credential.helper=",
    "-c", "http.followRedirects=false",
    "-c", "submodule.recurse=false",
)  # fmt: skip

_PASSTHROUGH_ENV = (
    "PATH",
    "SYSTEMROOT",
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "NO_PROXY",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "GIT_SSL_CAINFO",
)

METADATA_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


class GitTimeoutError(Exception):
    """Internal: a git command exceeded its timeout (callers map it to a typed error)."""


def git_executable() -> str:
    path = shutil.which("git")
    if path is None:
        raise UnavailableError("the git executable is not installed or not on PATH")
    return path


def safe_git_env(home: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in _PASSTHROUGH_ENV if key in os.environ}
    env.update(
        {
            "HOME": str(home),
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_PROTOCOL_FROM_USER": "0",
        }
    )
    return env


def run_git(
    argv: Sequence[str], *, timeout: float, home: Path, cwd: Path | None = None
) -> GitResult:
    """Run a fully-formed git argv. Raises :class:`GitTimeoutError` after killing the group."""
    if not argv or Path(argv[0]).name != "git":
        raise GitCommandError("refusing to run a non-git command")
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=safe_git_env(home),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise GitTimeoutError(f"git timed out after {timeout:.0f}s") from exc
    return GitResult(
        proc.returncode,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def strip_url_credentials(url: str) -> str:
    """Remove userinfo, query and fragment from a remote URL (scp-style URLs are kept)."""
    url = url.strip()
    if "://" not in url:
        return redact_text(url).text
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def summarize_stderr(stderr: str, limit: int = 500) -> str:
    """A short, credential-free excerpt of git's stderr suitable for error messages."""
    text = re.sub(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@", r"\1", stderr.strip())
    return redact_text(text).text[-limit:]


def read_git_metadata(repo_dir: Path, *, home: Path) -> GitMetadata:
    """Capture commit/branch/remote/date/dirty using read-only git commands.

    Returns empty metadata when ``repo_dir`` is not a git work tree or git is unavailable.
    """
    if not (repo_dir / ".git").exists():
        return GitMetadata()
    try:
        git = git_executable()
    except UnavailableError:
        return GitMetadata()
    base = [git, "--no-optional-locks", "-C", str(repo_dir), *SAFE_GIT_CONFIG]

    def query(*args: str) -> str | None:
        try:
            result = run_git([*base, *args], timeout=METADATA_TIMEOUT_SECONDS, home=home)
        except GitTimeoutError:
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    # git searches parent directories when .git is invalid; only trust this exact work tree.
    toplevel = query("rev-parse", "--show-toplevel")
    if not toplevel or Path(toplevel).resolve() != repo_dir.resolve():
        return GitMetadata()
    sha = query("rev-parse", "--verify", "--quiet", "HEAD")
    if not sha or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        return GitMetadata()
    branch = query("symbolic-ref", "--short", "--quiet", "HEAD") or None
    remote = query("config", "--get", "remote.origin.url")
    date_raw = query("log", "-1", "--format=%cI", "HEAD")
    status = query("status", "--porcelain=v1", "--untracked-files=no", "--ignore-submodules=all")
    commit_date: datetime | None = None
    if date_raw:
        try:
            commit_date = datetime.fromisoformat(date_raw)
        except ValueError:
            commit_date = None
    return GitMetadata(
        commit_sha=sha,
        branch=branch,
        remote_url=strip_url_credentials(remote) if remote else None,
        commit_date=commit_date,
        is_dirty=None if status is None else bool(status),
    )
