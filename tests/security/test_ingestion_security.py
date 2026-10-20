from __future__ import annotations

import io
import os
import stat
import struct
import warnings
import zipfile
from pathlib import Path

import pytest
from ingest_helpers import write_tree, zip_entries

from backend.core.config import Settings
from backend.core.exceptions import (
    LimitExceededError,
    NotConfiguredError,
    NotFoundError,
    SecurityViolationError,
    ValidationFailedError,
)
from backend.ingestion.errors import UnsafeArchiveError
from backend.ingestion.extraction import RATIO_MIN_BYTES, safe_extract_zip
from backend.ingestion.providers import LocalDirectoryProvider, PrivateGitHubProvider
from backend.ingestion.providers.local_dir import resolve_allowed_local_path
from backend.ingestion.workspace import normalize_rel_path, resolve_in_workspace
from backend.models.audit_schemas import RepositoryRef
from backend.models.enums import RepositorySourceType


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    path = tmp_path / "dest"
    path.mkdir()
    return path


def _cfg(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(data_dir=tmp_path / "data", **overrides)  # type: ignore[arg-type]


# --- ZIP extraction ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../evil.py",
        "a/../../evil.py",
        "/etc/evil.py",
        "C:/evil.py",
        "c:evil.py",
        "..\\evil.py",
        "a\\..\\..\\evil.py",
        "ok/\x01name.py",
    ],
)
def test_zip_slip_and_absolute_paths_are_rejected(tmp_path: Path, dest: Path, name: str) -> None:
    archive = zip_entries(tmp_path / "a.zip", {"ok.py": b"x", name: b"pwned"})
    with pytest.raises(UnsafeArchiveError):
        safe_extract_zip(archive, dest, _cfg(tmp_path))
    assert list(dest.iterdir()) == []
    assert not (tmp_path / "evil.py").exists()


def _zip_with_mode(path: Path, name: str, mode: int, data: bytes = b"target") -> Path:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = mode << 16
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ok.py", b"x")
        zf.writestr(info, data)
    return path


def test_symlink_entries_are_rejected(tmp_path: Path, dest: Path) -> None:
    archive = _zip_with_mode(tmp_path / "s.zip", "link", stat.S_IFLNK | 0o777, b"/etc/passwd")
    with pytest.raises(UnsafeArchiveError, match="symbolic link"):
        safe_extract_zip(archive, dest, _cfg(tmp_path))
    assert not (dest / "link").exists()


@pytest.mark.parametrize("kind", [stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK])
def test_device_and_special_entries_are_rejected(tmp_path: Path, dest: Path, kind: int) -> None:
    archive = _zip_with_mode(tmp_path / "d.zip", "dev", kind | 0o644)
    with pytest.raises(UnsafeArchiveError, match="special"):
        safe_extract_zip(archive, dest, _cfg(tmp_path))


def test_duplicate_and_colliding_names_are_rejected(tmp_path: Path, dest: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        dup = zip_entries(tmp_path / "dup.zip", {"a.py": b"1", "./a.py": b"2"})
    case = zip_entries(tmp_path / "case.zip", {"App.py": b"1", "app.py": b"2"})
    clash = zip_entries(tmp_path / "clash.zip", {"pkg": b"file", "pkg/mod.py": b"x"})
    for archive in (dup, case, clash):
        with pytest.raises(UnsafeArchiveError):
            safe_extract_zip(archive, dest, _cfg(tmp_path))


def test_encrypted_entries_are_rejected(tmp_path: Path, dest: Path) -> None:
    archive = tmp_path / "enc.zip"
    zip_entries(archive, {"a.py": b"x"})
    raw = bytearray(archive.read_bytes())
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        index = raw.find(signature)
        raw[index + flag_offset] |= 0x1
    archive.write_bytes(bytes(raw))
    with pytest.raises(UnsafeArchiveError, match="encrypted"):
        safe_extract_zip(archive, dest, _cfg(tmp_path))


def test_entry_count_limit(tmp_path: Path, dest: Path) -> None:
    archive = zip_entries(tmp_path / "many.zip", {f"f{i}.py": b"x" for i in range(11)})
    with pytest.raises(LimitExceededError):
        safe_extract_zip(archive, dest, _cfg(tmp_path, max_archive_entries=10))


def test_zip_bomb_by_declared_ratio(tmp_path: Path, dest: Path) -> None:
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.txt", b"\x00" * (RATIO_MIN_BYTES * 4))
    with pytest.raises(UnsafeArchiveError, match="ratio"):
        safe_extract_zip(archive, dest, _cfg(tmp_path))
    assert list(dest.iterdir()) == []


def test_highly_compressible_small_files_are_allowed(tmp_path: Path, dest: Path) -> None:
    archive = tmp_path / "ok.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("blank.py", b" " * 50_000)
    result = safe_extract_zip(archive, dest, _cfg(tmp_path))
    assert result.files_extracted == 1


def _patch_sizes(archive: Path, fake_size: int) -> None:
    """Rewrite the uncompressed size in both the local and central headers (a lying header)."""
    raw = bytearray(archive.read_bytes())
    local = raw.find(b"PK\x03\x04")
    struct.pack_into("<I", raw, local + 22, fake_size)
    central = raw.find(b"PK\x01\x02")
    struct.pack_into("<I", raw, central + 24, fake_size)
    archive.write_bytes(bytes(raw))


def test_zip_bomb_by_actual_bytes_with_lying_header(tmp_path: Path, dest: Path) -> None:
    archive = tmp_path / "liar.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("payload.txt", b"A" * (RATIO_MIN_BYTES * 8))
    _patch_sizes(archive, 100)
    with pytest.raises(UnsafeArchiveError):
        safe_extract_zip(archive, dest, _cfg(tmp_path))
    payload = dest / "payload.txt"
    assert not payload.exists() or payload.stat().st_size <= 100


def test_total_bytes_limit_stops_extraction_cleanly(tmp_path: Path, dest: Path) -> None:
    archive = zip_entries(tmp_path / "big.zip", {f"f{i}.py": b"x" * 400 for i in range(5)})
    result = safe_extract_zip(archive, dest, _cfg(tmp_path, max_repo_bytes=1000))
    assert result.files_extracted == 2
    assert result.limits_hit[0].startswith("max_repo_bytes (1000)")


def test_per_file_limit_skips_entry(tmp_path: Path, dest: Path) -> None:
    archive = zip_entries(tmp_path / "f.zip", {"big.py": b"x" * 5000, "ok.py": b"y"})
    result = safe_extract_zip(archive, dest, _cfg(tmp_path, max_file_bytes=1000))
    assert {s.path: s.skip_reason for s in result.skipped} == {"big.py": "oversized"}
    assert not (dest / "big.py").exists()
    assert (dest / "ok.py").read_bytes() == b"y"


def test_sensitive_and_filtered_entries_are_never_written(tmp_path: Path, dest: Path) -> None:
    archive = zip_entries(
        tmp_path / "s.zip",
        {
            "proj/.env": b"SECRET=1",
            "proj/node_modules/x.js": b"x",
            "proj/.git/config": b"[core]\n\tfsmonitor = /bin/evil\n",
            "proj/app.py": b"x = 1",
            "__MACOSX/proj/._app.py": b"meta",
        },
    )
    result = safe_extract_zip(archive, dest, _cfg(tmp_path))
    assert result.root_name == "proj"
    assert sorted(p.name for p in dest.iterdir()) == ["app.py"]
    assert {s.path: s.skip_reason for s in result.skipped} == {
        ".env": "sensitive",
        ".git/": "vcs",
        "node_modules/": "node_modules",
        "__MACOSX/": "os_metadata",
    }


def test_not_a_zip(tmp_path: Path, dest: Path) -> None:
    archive = tmp_path / "x.zip"
    archive.write_bytes(b"not a zip")
    with pytest.raises(ValidationFailedError):
        safe_extract_zip(archive, dest, _cfg(tmp_path))


def test_corrupt_entry_data(tmp_path: Path, dest: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.py", b"print('hello world')\n" * 50)
    raw = bytearray(buffer.getvalue())
    data_start = 30 + len("a.py")
    raw[data_start + 5 : data_start + 15] = b"\xff" * 10
    archive = tmp_path / "corrupt.zip"
    archive.write_bytes(bytes(raw))
    with pytest.raises(UnsafeArchiveError):
        safe_extract_zip(archive, dest, _cfg(tmp_path))


# --- Local directory allowlist ----------------------------------------------------------


@pytest.fixture
def allowed(tmp_path: Path) -> Path:
    root = tmp_path / "allowed"
    write_tree(root, {"proj/app.py": b"x = 1\n"})
    write_tree(tmp_path / "outside", {"secret.py": b"TOKEN = 'x'\n"})
    return root


def test_allowlisted_path_resolves(tmp_path: Path, allowed: Path) -> None:
    cfg = _cfg(tmp_path, local_allowlist=[allowed])
    assert resolve_allowed_local_path(str(allowed / "proj"), cfg) == (allowed / "proj").resolve()
    assert resolve_allowed_local_path(str(allowed), cfg) == allowed.resolve()


@pytest.mark.parametrize(
    "raw",
    [
        "{outside}",
        "{allowed}/../outside",
        "{allowed}/proj/../../outside",
        "{allowed}x",
        "/",
        "/etc",
    ],
)
def test_allowlist_escapes_are_rejected(tmp_path: Path, allowed: Path, raw: str) -> None:
    cfg = _cfg(tmp_path, local_allowlist=[allowed])
    path = raw.format(allowed=allowed, outside=tmp_path / "outside")
    with pytest.raises(SecurityViolationError):
        resolve_allowed_local_path(path, cfg)


def test_symlink_to_outside_is_rejected(tmp_path: Path, allowed: Path) -> None:
    os.symlink(tmp_path / "outside", allowed / "sneaky")
    cfg = _cfg(tmp_path, local_allowlist=[allowed])
    with pytest.raises(SecurityViolationError):
        resolve_allowed_local_path(str(allowed / "sneaky"), cfg)


def test_relative_missing_and_file_paths(tmp_path: Path, allowed: Path) -> None:
    cfg = _cfg(tmp_path, local_allowlist=[allowed])
    with pytest.raises(ValidationFailedError):
        resolve_allowed_local_path("allowed/proj", cfg)
    with pytest.raises(ValidationFailedError):
        resolve_allowed_local_path(str(allowed / "proj" / "app.py"), cfg)
    with pytest.raises(NotFoundError):
        resolve_allowed_local_path(str(allowed / "nope"), cfg)
    with pytest.raises(SecurityViolationError):
        resolve_allowed_local_path("/x\x00y", cfg)


def test_empty_allowlist_and_data_dir_are_rejected(tmp_path: Path, allowed: Path) -> None:
    with pytest.raises(SecurityViolationError):
        resolve_allowed_local_path(str(allowed), _cfg(tmp_path))
    cfg = _cfg(tmp_path, local_allowlist=[tmp_path])
    (tmp_path / "data").mkdir()
    with pytest.raises(SecurityViolationError):
        resolve_allowed_local_path(str(tmp_path / "data"), cfg)


def test_symlinked_dir_inside_repo_is_not_followed(tmp_path: Path, allowed: Path) -> None:
    os.symlink(tmp_path / "outside", allowed / "proj" / "linked")
    cfg = _cfg(tmp_path, local_allowlist=[allowed])
    workspace = tmp_path / "ws" / "src"
    workspace.mkdir(parents=True)
    ref = RepositoryRef(
        source_type=RepositorySourceType.LOCAL_DIRECTORY, local_path=str(allowed / "proj")
    )
    acquired = LocalDirectoryProvider().acquire(ref, workspace, cfg)
    assert {s.path: s.skip_reason for s in acquired.skipped} == {"linked": "symlink"}
    assert sorted(p.name for p in workspace.rglob("*")) == ["app.py"]


def test_local_provider_rejects_dotdot(tmp_path: Path, allowed: Path) -> None:
    cfg = _cfg(tmp_path, local_allowlist=[allowed])
    ref = RepositoryRef(
        source_type=RepositorySourceType.LOCAL_DIRECTORY, local_path=f"{allowed}/proj/../proj"
    )
    with pytest.raises(SecurityViolationError):
        LocalDirectoryProvider().acquire(ref, tmp_path, cfg)


# --- Paths inside the workspace, private GitHub -----------------------------------------


@pytest.mark.parametrize("raw", ["../x.py", "a/../../x.py", "/etc/passwd", "C:/x.py"])
def test_workspace_relative_paths_reject_traversal(raw: str) -> None:
    with pytest.raises(SecurityViolationError):
        normalize_rel_path(raw)


@pytest.mark.parametrize("raw", ["", "a\\b.py", "a\x00.py", "./"])
def test_workspace_relative_paths_reject_garbage(raw: str) -> None:
    with pytest.raises(ValidationFailedError):
        normalize_rel_path(raw)


def test_normalize_rel_path() -> None:
    assert normalize_rel_path("./app//main.py") == "app/main.py"


def test_resolve_in_workspace_refuses_symlinks(tmp_path: Path) -> None:
    src = tmp_path / "src"
    write_tree(src, {"a.py": b"x"})
    os.symlink(tmp_path, src / "up")
    assert resolve_in_workspace(src, "a.py") == (src / "a.py").resolve()
    with pytest.raises(SecurityViolationError):
        resolve_in_workspace(src, "up/src/a.py")


def test_private_github_is_not_configured(tmp_path: Path) -> None:
    ref = RepositoryRef(
        source_type=RepositorySourceType.GITHUB_PRIVATE, url="https://github.com/o/private"
    )
    with pytest.raises(NotConfiguredError, match="GitHub App"):
        PrivateGitHubProvider().acquire(ref, tmp_path, _cfg(tmp_path))
