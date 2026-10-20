"""Helpers for audit tests: build inventories and snapshot rows without the ingestion pipeline."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from backend.core.config import Settings
from backend.core.redaction import SENSITIVE_FILE_NAMES
from backend.models.audit_schemas import RepositoryInventory, SourceFileInfo
from backend.models.database import session_scope
from backend.models.enums import RepositorySourceType, SnapshotStatus
from backend.models.orm.audit import Repository, RepositorySnapshot

SHOPFAST = Path(__file__).parent / "fixtures" / "repos" / "shopfast"
_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"}


def scan_inventory(root: Path) -> RepositoryInventory:
    """Minimal inventory: every file, Python flagged, sensitive files marked skipped."""
    files: list[SourceFileInfo] = []
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for filename in sorted(filenames):
            path = Path(directory) / filename
            rel = path.relative_to(root).as_posix()
            sensitive = bool(SENSITIVE_FILE_NAMES.search(rel))
            files.append(
                SourceFileInfo(
                    path=rel,
                    size_bytes=path.stat().st_size,
                    is_python=filename.endswith(".py"),
                    skipped=sensitive,
                    skip_reason="sensitive file" if sensitive else None,
                )
            )
    return RepositoryInventory(root_name=root.name, files=files, total_files_seen=len(files))


def copy_into_workspace(settings: Settings, source: Path, name: str) -> Path:
    target = settings.workspaces_dir / name
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return target


def create_snapshot(
    workspace: Path | str | None,
    inventory: RepositoryInventory | None,
    *,
    status: SnapshotStatus = SnapshotStatus.READY,
) -> str:
    with session_scope() as session:
        repository = Repository(
            name="shopfast",
            source_type=RepositorySourceType.LOCAL_DIRECTORY.value,
            source_locator="fixture",
        )
        session.add(repository)
        session.flush()
        snapshot = RepositorySnapshot(
            repository_id=repository.id,
            status=status.value,
            workspace_path=str(workspace) if workspace is not None else None,
            inventory=inventory.model_dump(mode="json") if inventory else None,
        )
        session.add(snapshot)
        session.flush()
        return snapshot.id
