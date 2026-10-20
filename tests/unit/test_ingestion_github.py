from __future__ import annotations

import socket
from pathlib import Path

import pytest

from backend.core.config import Settings
from backend.core.exceptions import (
    ExternalServiceError,
    SecurityViolationError,
    ValidationFailedError,
)
from backend.ingestion.errors import GitCommandError, RefNotFoundError, RepositoryNotFoundError
from backend.ingestion.git_meta import SAFE_GIT_CONFIG, safe_git_env, strip_url_credentials
from backend.ingestion.providers.github_public import (
    GitHubLocation,
    PublicGitHubProvider,
    build_clone_commands,
    map_git_failure,
    parse_github_url,
    validate_git_ref,
)
from backend.ingestion.workspace import create_workspace
from backend.models.audit_schemas import RepositoryRef
from backend.models.enums import RepositorySourceType

HOSTS = ["github.com"]
SHA = "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d"


@pytest.mark.parametrize(
    ("url", "owner", "repo", "ref"),
    [
        ("https://github.com/octocat/Hello-World", "octocat", "Hello-World", None),
        ("https://github.com/octocat/Hello-World.git", "octocat", "Hello-World", None),
        ("https://github.com/octocat/Hello-World/", "octocat", "Hello-World", None),
        ("HTTPS://GitHub.com/octocat/hello_world.py", "octocat", "hello_world.py", None),
        ("https://github.com/o/r/tree/main", "o", "r", "main"),
        ("https://github.com/o/r/tree/feature/x-1", "o", "r", "feature/x-1"),
        (f"https://github.com/o/r/tree/{SHA}", "o", "r", SHA),
    ],
)
def test_valid_github_urls(url: str, owner: str, repo: str, ref: str | None) -> None:
    location = parse_github_url(url, HOSTS)
    assert (location.owner, location.repo, location.ref) == (owner, repo, ref)
    assert location.host == "github.com"
    assert location.clone_url == f"https://github.com/{owner}/{repo}.git"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/o/r",
        "ssh://git@github.com/o/r.git",
        "git://github.com/o/r.git",
        "file:///etc/passwd",
        "git@github.com:o/r.git",
        "ftp://github.com/o/r",
        "https://user:token@github.com/o/r",
        "https://token@github.com/o/r",
        "https://gitlab.com/o/r",
        "https://github.com.evil.com/o/r",
        "https://evil.com/github.com/o/r",
        "https://github.com:8443/o/r",
        "https://github.com/o/r?token=abc",
        "https://github.com/o/r#readme",
        "https://github.com/o/../../etc",
        "https://github.com/o/r/tree/..",
        "https://github.com//o/r",
        "https://github.com/o/%2e%2e",
        "https://github.com/o/r\\..\\x",
        "https://github.com/o/r\n",
        "https://github.com/o/ r",
        "",
    ],
)
def test_rejected_urls_are_security_violations(url: str) -> None:
    with pytest.raises(SecurityViolationError):
        parse_github_url(url, HOSTS)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octocat",
        "https://github.com/o/r/blob/main/x.py",
        "https://github.com/o/r/tree",
        "https://github.com/-o/r",
        "https://github.com/o/r/tree/-upload-pack",
        "https://github.com/o/r/tree/a..b",
        "https://github.com/o/r/tree/x.lock",
        "https://github.com/o/r.git/tree/main",
    ],
)
def test_malformed_urls_fail_validation(url: str) -> None:
    with pytest.raises(ValidationFailedError):
        parse_github_url(url, HOSTS)


def test_allowed_hosts_are_configurable() -> None:
    assert parse_github_url("https://ghe.example.com/o/r", ["ghe.example.com"]).host == (
        "ghe.example.com"
    )
    with pytest.raises(SecurityViolationError):
        parse_github_url("https://github.com/o/r", ["ghe.example.com"])


def test_explicit_ref_must_match_url_ref() -> None:
    assert parse_github_url("https://github.com/o/r", HOSTS, "v1.0").ref == "v1.0"
    assert parse_github_url("https://github.com/o/r/tree/v1", HOSTS, "v1").ref == "v1"
    with pytest.raises(ValidationFailedError):
        parse_github_url("https://github.com/o/r/tree/v1", HOSTS, "v2")
    with pytest.raises(ValidationFailedError):
        parse_github_url("https://github.com/o/r", HOSTS, "--upload-pack=evil")


@pytest.mark.parametrize("ref", ["main", "release/1.x", "v1.2.3", SHA, "feature_x"])
def test_valid_refs(ref: str) -> None:
    assert validate_git_ref(ref) == ref


@pytest.mark.parametrize("ref", ["-x", "/x", "x/", "a..b", "a//b", "x.lock", "a b", "a;rm", "$(x)"])
def test_invalid_refs(ref: str) -> None:
    with pytest.raises(ValidationFailedError):
        validate_git_ref(ref)


def test_clone_command_default_branch(tmp_path: Path) -> None:
    location = GitHubLocation("github.com", "o", "r")
    [argv] = build_clone_commands("/usr/bin/git", location, tmp_path)
    assert argv[0] == "/usr/bin/git"
    assert argv[1 : 1 + len(SAFE_GIT_CONFIG)] == list(SAFE_GIT_CONFIG)
    tail = argv[1 + len(SAFE_GIT_CONFIG) :]
    assert tail == [
        "clone",
        "--quiet",
        "--template=",
        "--depth",
        "1",
        "--single-branch",
        "--no-tags",
        "--no-recurse-submodules",
        "--",
        "https://github.com/o/r.git",
        str(tmp_path),
    ]


def test_clone_command_with_branch(tmp_path: Path) -> None:
    [argv] = build_clone_commands("git", GitHubLocation("github.com", "o", "r", "dev"), tmp_path)
    assert "--branch=dev" in argv
    assert argv.index("--branch=dev") < argv.index("--")


def test_clone_commands_for_commit_sha_use_shallow_fetch(tmp_path: Path) -> None:
    commands = build_clone_commands("git", GitHubLocation("github.com", "o", "r", SHA), tmp_path)
    assert [c[c.index("-c") + len(SAFE_GIT_CONFIG)] for c in commands] == [
        "init",
        "fetch",
        "checkout",
    ]
    fetch = commands[1]
    assert fetch[-3:] == ["--", "https://github.com/o/r.git", SHA]
    assert {"--depth", "1", "--no-tags", "--no-recurse-submodules"} <= set(fetch)
    assert commands[2][-2:] == ["--detach", "FETCH_HEAD"]


def test_safe_git_config_hardening() -> None:
    pairs = dict(SAFE_GIT_CONFIG[i + 1].split("=", 1) for i in range(0, len(SAFE_GIT_CONFIG), 2))
    assert pairs["core.hooksPath"] == "/dev/null"
    assert pairs["protocol.allow"] == "never"
    assert pairs["protocol.https.allow"] == "always"
    assert pairs["core.symlinks"] == "false"
    assert pairs["core.fsmonitor"] == "false"


def test_safe_git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_DIR", "/somewhere/else")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak")
    env = safe_git_env(tmp_path)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_ASKPASS"] == ""
    assert env["HOME"] == str(tmp_path)
    assert "GIT_DIR" not in env
    assert "GITHUB_TOKEN" not in env


@pytest.mark.parametrize(
    ("stderr", "error"),
    [
        ("remote: Repository not found.\nfatal: repository not found", RepositoryNotFoundError),
        ("fatal: could not read Username for 'https://github.com'", RepositoryNotFoundError),
        ("fatal: Remote branch x not found in upstream origin", RefNotFoundError),
        ("fatal: couldn't find remote ref deadbeef", RefNotFoundError),
        ("fatal: unable to access: Could not resolve host: github.com", ExternalServiceError),
        ("fatal: something odd", GitCommandError),
    ],
)
def test_git_error_mapping(stderr: str, error: type[Exception]) -> None:
    assert type(map_git_failure(stderr, GitHubLocation("github.com", "o", "r", "x"))) is error


def test_git_error_details_strip_credentials() -> None:
    exc = map_git_failure(
        "fatal: unable to access 'https://user:pw@github.com/o/r/'",
        GitHubLocation("github.com", "o", "r"),
    )
    assert "pw" not in str(exc.details)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:token@github.com/o/r.git", "https://github.com/o/r.git"),
        ("https://x-access-token:ghs_abc@github.com/o/r", "https://github.com/o/r"),
        ("https://github.com/o/r.git?token=abc#frag", "https://github.com/o/r.git"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("https://example.com:8443/o/r", "https://example.com:8443/o/r"),
    ],
)
def test_strip_url_credentials(url: str, expected: str) -> None:
    assert strip_url_credentials(url) == expected


def _github_reachable() -> bool:
    try:
        with socket.create_connection(("github.com", 443), timeout=3):
            return True
    except OSError:
        return False


@pytest.mark.network
@pytest.mark.skipif(not _github_reachable(), reason="github.com is not reachable")
def test_clone_hello_world(settings: Settings) -> None:
    workspace = create_workspace(settings, "clone-test")
    ref = RepositoryRef(
        source_type=RepositorySourceType.GITHUB_PUBLIC, url="https://github.com/octocat/Hello-World"
    )
    acquired = PublicGitHubProvider().acquire(ref, workspace.src, settings)
    assert (workspace.src / "README").is_file()
    assert acquired.display_name == "octocat/Hello-World"
    assert acquired.git.commit_sha
    assert len(acquired.git.commit_sha) == 40
    assert acquired.git.remote_url == "https://github.com/octocat/Hello-World"
    assert acquired.git.is_dirty is False
