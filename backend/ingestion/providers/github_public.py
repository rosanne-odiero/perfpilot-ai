"""Public GitHub repositories: strict URL validation and a hardened shallow clone."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from backend.core.config import Settings
from backend.core.exceptions import (
    ExternalServiceError,
    PerfPilotError,
    SecurityViolationError,
    ValidationFailedError,
)
from backend.ingestion.errors import (
    CloneTimeoutError,
    GitCommandError,
    RefNotFoundError,
    RepositoryNotFoundError,
)
from backend.ingestion.git_meta import (
    SAFE_GIT_CONFIG,
    GitTimeoutError,
    git_executable,
    read_git_metadata,
    run_git,
    summarize_stderr,
)
from backend.models.audit_schemas import AcquiredSource, RepositoryRef
from backend.models.enums import RepositorySourceType

_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_FORBIDDEN_URL_CHARS = re.compile(r"[\s\\%\x00-\x1f\x7f]")
_MAX_URL_LENGTH = 2048


@dataclass(frozen=True)
class GitHubLocation:
    host: str
    owner: str
    repo: str
    ref: str | None = None

    @property
    def canonical_url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.repo}"

    @property
    def clone_url(self) -> str:
        return f"{self.canonical_url}.git"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


def validate_git_ref(ref: str) -> str:
    """Accept branch/tag names and commit SHAs; reject anything git or a shell could misread."""
    if (
        not _REF.match(ref)
        or ref.startswith(("-", "/", "."))
        or ref.endswith(("/", ".", ".lock"))
        or ".." in ref
        or "//" in ref
        or "/." in ref
    ):
        raise ValidationFailedError("invalid git ref", details={"ref": ref[:200]})
    return ref


def is_commit_sha(ref: str) -> bool:
    return bool(_COMMIT_SHA.match(ref))


def parse_github_url(
    url: str, allowed_hosts: list[str], explicit_ref: str | None = None
) -> GitHubLocation:
    """Validate ``https://<allowed-host>/<owner>/<repo>[.git][/tree/<ref>]`` and nothing else."""
    if len(url) > _MAX_URL_LENGTH or _FORBIDDEN_URL_CHARS.search(url):
        raise SecurityViolationError("repository URL contains forbidden characters")
    if "?" in url or "#" in url:
        raise SecurityViolationError("query strings and fragments are not allowed in the URL")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise SecurityViolationError("only https:// GitHub URLs are allowed")
    if "@" in parts.netloc:
        raise SecurityViolationError("credentials in the repository URL are not allowed")
    host = (parts.hostname or "").lower()
    if parts.netloc.lower() != host:
        raise SecurityViolationError("ports are not allowed in the repository URL")
    if host not in {h.lower() for h in allowed_hosts}:
        raise SecurityViolationError(
            "repository host is not allowed", details={"allowed_hosts": allowed_hosts}
        )
    path = parts.path
    if path.endswith("/"):
        path = path[:-1]
    segments = path.split("/")[1:] if path.startswith("/") else []
    if any(seg in ("", ".", "..") for seg in segments):
        raise SecurityViolationError("malformed repository path")
    if len(segments) == 2:
        owner, repo = segments
        url_ref = None
        repo = repo.removesuffix(".git")
    elif len(segments) >= 4 and segments[2] == "tree":
        owner, repo = segments[0], segments[1]
        url_ref = "/".join(segments[3:])
    else:
        raise ValidationFailedError("expected https://github.com/<owner>/<repo>[.git][/tree/<ref>]")
    if (
        not _OWNER.match(owner)
        or not _REPO.match(repo)
        or repo in (".", "..")
        or repo.endswith(".git")
    ):
        raise ValidationFailedError("invalid GitHub owner or repository name")
    if url_ref is not None:
        validate_git_ref(url_ref)
    if explicit_ref is not None:
        validate_git_ref(explicit_ref)
        if url_ref is not None and url_ref != explicit_ref:
            raise ValidationFailedError("ref in the URL does not match the ref field")
    return GitHubLocation(host=host, owner=owner, repo=repo, ref=explicit_ref or url_ref)


def build_clone_commands(git: str, location: GitHubLocation, dest: Path) -> list[list[str]]:
    """The exact argv lists used to fetch ``location`` into the empty directory ``dest``."""
    base = [git, *SAFE_GIT_CONFIG]
    ref = location.ref
    if ref is not None and is_commit_sha(ref):
        in_repo = [git, "-C", str(dest), *SAFE_GIT_CONFIG]
        return [
            [*base, "init", "--quiet", "--template=", str(dest)],
            [
                *in_repo,
                "fetch",
                "--quiet",
                "--depth",
                "1",
                "--no-tags",
                "--no-recurse-submodules",
                "--",
                location.clone_url,
                ref,
            ],
            [*in_repo, "checkout", "--quiet", "--detach", "FETCH_HEAD"],
        ]
    clone = [
        *base,
        "clone",
        "--quiet",
        "--template=",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
        "--no-recurse-submodules",
    ]
    if ref is not None:
        clone.append(f"--branch={ref}")
    return [[*clone, "--", location.clone_url, str(dest)]]


def map_git_failure(stderr: str, location: GitHubLocation) -> PerfPilotError:
    lowered = stderr.lower()
    details = {"repository": location.full_name, "stderr": summarize_stderr(stderr)}
    if (
        "repository not found" in lowered
        or "could not read username" in lowered
        or "authentication failed" in lowered
        or "terminal prompts disabled" in lowered
    ):
        return RepositoryNotFoundError(
            f"repository {location.full_name} was not found or is not public", details=details
        )
    if (
        ("remote branch" in lowered and "not found" in lowered)
        or "couldn't find remote ref" in lowered
        or "not our ref" in lowered
        or "no such remote ref" in lowered
        or "unadvertised object" in lowered
    ):
        return RefNotFoundError(
            f"ref {location.ref!r} was not found in {location.full_name}", details=details
        )
    if "could not resolve host" in lowered or "failed to connect" in lowered:
        return ExternalServiceError(f"could not reach {location.host}", details=details)
    return GitCommandError("git failed to fetch the repository", details=details)


class PublicGitHubProvider:
    source_type = RepositorySourceType.GITHUB_PUBLIC

    def acquire(
        self, ref: RepositoryRef, workspace_dir: Path, settings: Settings
    ) -> AcquiredSource:
        if not ref.url:
            raise ValidationFailedError("a GitHub URL is required")
        location = parse_github_url(ref.url, settings.github_allowed_hosts, ref.ref)
        git = git_executable()
        home = workspace_dir.parent
        deadline = time.monotonic() + settings.clone_timeout_seconds
        for argv in build_clone_commands(git, location, workspace_dir):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CloneTimeoutError(
                    f"clone exceeded {settings.clone_timeout_seconds}s",
                    details={"repository": location.full_name},
                )
            try:
                result = run_git(argv, timeout=remaining, home=home)
            except GitTimeoutError as exc:
                raise CloneTimeoutError(
                    f"clone exceeded {settings.clone_timeout_seconds}s",
                    details={"repository": location.full_name},
                ) from exc
            if result.returncode != 0:
                raise map_git_failure(result.stderr, location)
        git_meta = read_git_metadata(workspace_dir, home=home)
        if location.ref and is_commit_sha(location.ref) and git_meta.commit_sha != location.ref:
            raise GitCommandError("checked-out commit does not match the requested SHA")
        git_meta.remote_url = location.canonical_url
        return AcquiredSource(
            workspace_path=str(workspace_dir),
            source_type=self.source_type,
            display_name=ref.display_name or location.full_name,
            git=git_meta,
        )
