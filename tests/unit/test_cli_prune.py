from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.cli import main
from backend.core.config import Settings
from backend.ingestion.workspace import create_workspace, make_read_only, remove_tree
from backend.models.database import session_scope
from backend.models.orm.audit import Repository, RepositorySnapshot


def _read_only_workspace(settings: Settings, snapshot_id: str) -> Path:
    ws = create_workspace(settings, snapshot_id)
    (ws.src / "pkg").mkdir()
    (ws.src / "pkg" / "mod.py").write_text("x = 1\n")
    make_read_only(ws.src)
    assert not os.access(ws.src / "pkg", os.W_OK) or os.geteuid() == 0
    return ws.root


def test_prune_removes_read_only_and_orphan_workspaces(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with session_scope() as session:
        repo = Repository(name="r", source_type="LOCAL_DIRECTORY", source_locator="/x")
        session.add(repo)
        session.flush()
        snap = RepositorySnapshot(repository_id=repo.id, status="READY")
        session.add(snap)
        session.flush()
        snap_id = snap.id
    tracked = _read_only_workspace(settings, snap_id)
    orphan = _read_only_workspace(settings, "orphan-dir")

    assert main(["prune-workspaces", "--all"]) == 0

    removed = json.loads(capsys.readouterr().out)["removed_workspaces"]
    assert set(removed) == {snap_id, "orphan-dir"}
    assert not tracked.exists()
    assert not orphan.exists()
    with session_scope() as session:
        snapshot = session.get(RepositorySnapshot, snap_id)
        assert snapshot is not None
        assert snapshot.workspace_path is None


def test_prune_keeps_recent_workspaces_by_default(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with session_scope() as session:
        repo = Repository(name="r", source_type="LOCAL_DIRECTORY", source_locator="/x")
        session.add(repo)
        session.flush()
        snap = RepositorySnapshot(repository_id=repo.id, status="READY")
        session.add(snap)
        session.flush()
        snap_id = snap.id
    root = _read_only_workspace(settings, snap_id)
    assert main(["prune-workspaces"]) == 0
    assert json.loads(capsys.readouterr().out)["removed_workspaces"] == []
    assert root.exists()
    remove_tree(root)
