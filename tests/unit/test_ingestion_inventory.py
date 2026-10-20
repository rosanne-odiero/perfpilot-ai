from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest
from ingest_helpers import EXPECTED_KEPT_PYTHON, make_sample_repo, write_tree, zip_tree

from backend.core.config import Settings
from backend.infrastructure.artifact_store import ArtifactStore
from backend.ingestion.detection import TechnologyDetector
from backend.ingestion.inventory import build_inventory, inventory_content_hash, skipped_counts
from backend.ingestion.providers import LocalDirectoryProvider, ZipUploadProvider
from backend.ingestion.providers.zip_upload import UPLOAD_ARTIFACT_KIND
from backend.ingestion.workspace import create_workspace
from backend.models.audit_schemas import RepositoryInventory, RepositoryRef
from backend.models.database import session_scope
from backend.models.enums import RepositorySourceType


def _tech(inventory: RepositoryInventory) -> dict[str, list[str]]:
    return {t.name: t.evidence for t in inventory.technologies}


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    return make_sample_repo(tmp_path / "sample")


def test_inventory_classifies_every_file(sample: Path, settings: Settings) -> None:
    inventory = build_inventory(sample, settings)
    files = {f.path: f for f in inventory.files}
    assert {f.path for f in inventory.python_files} == EXPECTED_KEPT_PYTHON
    expected_skips = {
        ".env": "sensitive",
        "config/secrets.json": "sensitive",
        "assets/logo.png": "binary",
        "data/blob.dat": "binary",
        "app/proto/service_pb2.py": "generated",
        "app/gen_client.py": "generated",
        "static/app.min.js": "minified",
        "static/bundle.js": "minified",
        "node_modules/": "node_modules",
        ".venv/": "virtualenv",
        "env/": "virtualenv",
        "app/__pycache__/": "cache",
        "build/": "build_output",
        "dist/": "build_output",
        "sample.egg-info/": "build_output",
        ".mypy_cache/": "cache",
        ".git/": "vcs",
    }
    for path, reason in expected_skips.items():
        assert files[path].skipped, path
        assert files[path].skip_reason == reason, path
    assert not any(p.startswith("node_modules/pkg") for p in files)
    assert files["migrations/0001_initial.py"].skipped is False
    assert files["migrations/0001_initial.py"].language == "Python"
    assert files["app/main.py"].sha256 is not None
    assert files[".env"].sha256 is None
    assert files["config/secrets.json"].sha256 is None
    assert inventory.limits_hit == []
    assert inventory.languages["Python"] == 4
    assert inventory.total_files_seen == sum(1 for p in files if not p.endswith("/"))
    assert skipped_counts(inventory)["sensitive"] == 2


def test_inventory_detects_technologies_with_evidence(sample: Path, settings: Settings) -> None:
    tech = _tech(build_inventory(sample, settings))
    assert any("app/main.py:2" in e for e in tech["FastAPI"])
    assert any("pyproject.toml" in e for e in tech["FastAPI"])
    assert any("app/main.py" in e for e in tech["asyncio"])
    assert any("app/models.py" in e for e in tech["pandas"])
    assert any("app/models.py" in e for e in tech["SQLAlchemy"])
    assert any("package.json" in e for e in tech["Next.js"])
    assert "React" in tech
    assert tech["Docker"] == ["Dockerfile"]
    assert tech["Python"] == ["4 Python files"]
    assert "Django" not in tech


def test_inventory_dependencies(sample: Path, settings: Settings) -> None:
    deps = {
        (d.name, d.source_file, d.group) for d in build_inventory(sample, settings).dependencies
    }
    assert ("fastapi", "pyproject.toml", None) in deps
    assert ("pytest", "pyproject.toml", "optional:dev") in deps
    assert ("ruff", "pyproject.toml", "lint") in deps
    assert ("requests", "requirements.txt", None) in deps
    assert ("sqlalchemy", "requirements-base.txt", None) in deps
    assert "editable" not in {d[0] for d in deps}


def test_detector_evidence_is_capped() -> None:
    detector = TechnologyDetector()
    for i in range(8):
        detector.observe_python(f"m{i}.py", b"import numpy as np\n")
    [numpy] = detector.result()
    assert len(numpy.evidence) == 6
    assert numpy.evidence[-1] == "... and 3 more"


def test_detector_scans_imports_without_false_positives() -> None:
    detector = TechnologyDetector()
    detector.observe_python(
        "a.py",
        b"from flask import Flask\nimport os, celery.app\n"
        b"from .django import x\n# import django\ntext = 'import streamlit'\n",
    )
    names = {t.name for t in detector.result()}
    assert names == {"Flask", "Celery"}


def test_django_manage_py_and_alembic(tmp_path: Path, settings: Settings) -> None:
    write_tree(
        tmp_path,
        {
            "manage.py": b"import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 's')\n",
            "alembic.ini": b"[alembic]\n",
            "poetry.lock": b"# lock\n",
            ".github/workflows/ci.yml": b"on: push\n",
        },
    )
    tech = _tech(build_inventory(tmp_path, settings))
    assert tech["Django"] == ["manage.py sets DJANGO_SETTINGS_MODULE"]
    assert tech["Alembic"] == ["alembic.ini"]
    assert tech["Poetry"] == ["lock file poetry.lock"]
    assert tech["GitHub Actions"] == [".github/workflows/ci.yml"]


def test_limits_produce_partial_inventory(tmp_path: Path, settings: Settings) -> None:
    write_tree(tmp_path, {f"m{i:02}.py": b"x = 1\n" for i in range(20)})
    limited = settings.model_copy(update={"max_repo_files": 5})
    inventory = build_inventory(tmp_path, limited)
    assert len(inventory.files) == 5
    assert inventory.limits_hit[0].startswith("max_repo_files (5)")


def test_python_file_limit_skips_extra_files(tmp_path: Path, settings: Settings) -> None:
    write_tree(tmp_path, {f"m{i}.py": b"x = 1\n" for i in range(4)} | {"README.md": b"hi"})
    inventory = build_inventory(tmp_path, settings.model_copy(update={"max_python_files": 2}))
    assert len(inventory.python_files) == 2
    assert [f.skip_reason for f in inventory.files].count("python_file_limit") == 2
    assert any(h.startswith("max_python_files (2)") for h in inventory.limits_hit)
    assert not any(f.path == "README.md" and f.skipped for f in inventory.files)


def test_byte_and_file_size_limits(tmp_path: Path, settings: Settings) -> None:
    write_tree(tmp_path, {"a.py": b"a" * 100, "b.py": b"b" * 100, "c.py": b"c" * 5000})
    inventory = build_inventory(
        tmp_path, settings.model_copy(update={"max_file_bytes": 1000, "max_repo_bytes": 150})
    )
    files = {f.path: f for f in inventory.files}
    assert files["a.py"].skipped is False
    assert "b.py" not in files
    assert inventory.limits_hit[0].startswith("max_repo_bytes (150)")


def test_oversized_file_is_skipped_but_reported(tmp_path: Path, settings: Settings) -> None:
    write_tree(tmp_path, {"big.py": b"x" * 2000, "small.py": b"y = 1\n"})
    inventory = build_inventory(tmp_path, settings.model_copy(update={"max_file_bytes": 1000}))
    files = {f.path: f for f in inventory.files}
    assert files["big.py"].skip_reason == "oversized"
    assert files["big.py"].size_bytes == 2000
    assert files["big.py"].sha256 is None
    assert inventory.limits_hit == []


def test_content_hash_ignores_skipped_files(tmp_path: Path, settings: Settings) -> None:
    write_tree(tmp_path, {"a.py": b"x = 1\n", "logo.png": b"\x89PNG"})
    first = inventory_content_hash(build_inventory(tmp_path, settings))
    (tmp_path / "logo.png").write_bytes(b"\x89PNG changed")
    assert inventory_content_hash(build_inventory(tmp_path, settings)) == first
    (tmp_path / "a.py").write_bytes(b"x = 2\n")
    assert inventory_content_hash(build_inventory(tmp_path, settings)) != first


def _acquire_local(sample: Path, settings: Settings, snapshot_id: str) -> RepositoryInventory:
    cfg = settings.model_copy(update={"local_allowlist": [sample.parent]})
    workspace = create_workspace(cfg, snapshot_id)
    ref = RepositoryRef(source_type=RepositorySourceType.LOCAL_DIRECTORY, local_path=str(sample))
    acquired = LocalDirectoryProvider().acquire(ref, workspace.src, cfg)
    return build_inventory(
        workspace.src,
        cfg,
        prefiltered=acquired.skipped,
        limits_hit=acquired.limits_hit,
        root_name=acquired.display_name,
    )


def _acquire_zip(archive: Path, settings: Settings, snapshot_id: str) -> RepositoryInventory:
    with session_scope() as session:
        artifact_id = (
            ArtifactStore(settings).save_file(session, kind=UPLOAD_ARTIFACT_KIND, source=archive).id
        )
    workspace = create_workspace(settings, snapshot_id)
    ref = RepositoryRef(source_type=RepositorySourceType.ZIP_UPLOAD, upload_artifact_id=artifact_id)
    acquired = ZipUploadProvider().acquire(ref, workspace.src, settings)
    return build_inventory(
        workspace.src,
        settings,
        prefiltered=acquired.skipped,
        limits_hit=acquired.limits_hit,
        root_name=acquired.display_name,
    )


def test_local_and_zip_providers_yield_identical_inventories(
    sample: Path, settings: Settings, tmp_path: Path
) -> None:
    direct = build_inventory(sample, settings)
    local = _acquire_local(sample, settings, "local-1")
    wrapped = _acquire_zip(zip_tree(sample, tmp_path / "w.zip", "sample/"), settings, "zip-1")
    flat = _acquire_zip(zip_tree(sample, tmp_path / "f.zip"), settings, "zip-2")
    assert local.root_name == wrapped.root_name == "sample"
    for other in (local, wrapped, flat):
        assert other.model_dump(exclude={"root_name"}) == direct.model_dump(exclude={"root_name"})


def test_local_provider_never_copies_filtered_content(sample: Path, settings: Settings) -> None:
    cfg = settings.model_copy(update={"local_allowlist": [sample.parent]})
    workspace = create_workspace(cfg, "local-copy")
    ref = RepositoryRef(source_type=RepositorySourceType.LOCAL_DIRECTORY, local_path=str(sample))
    acquired = LocalDirectoryProvider().acquire(ref, workspace.src, cfg)
    copied = {
        p.relative_to(workspace.src).as_posix() for p in workspace.src.rglob("*") if p.is_file()
    }
    assert ".env" not in copied
    assert "config/secrets.json" not in copied
    assert not any(p.startswith(("node_modules/", ".venv/", "env/", ".git/")) for p in copied)
    assert "app/main.py" in copied
    assert (workspace.src / "app/main.py").read_bytes() == (sample / "app/main.py").read_bytes()
    skipped = {s.path: s.skip_reason for s in acquired.skipped}
    assert skipped[".env"] == "sensitive"
    assert skipped["node_modules/"] == "node_modules"
    assert acquired.git.commit_sha is None


def test_local_provider_skips_huge_trees_inside_pruned_dirs(
    tmp_path: Path, settings: Settings
) -> None:
    repo = tmp_path / "repo"
    write_tree(repo, {"app.py": b"import flask\n"})
    for i in range(300):
        (repo / "node_modules" / f"p{i}").mkdir(parents=True)
        (repo / "node_modules" / f"p{i}" / "index.js").write_bytes(b"x")
    inventory = _acquire_local(repo, settings, "local-big")
    assert inventory.total_files_seen == 1
    assert [f.path for f in inventory.files] == ["app.py", "node_modules/"]


def test_local_provider_limits_are_reported(tmp_path: Path, settings: Settings) -> None:
    repo = tmp_path / "repo"
    write_tree(repo, {f"m{i:02}.py": b"x = 1\n" for i in range(10)})
    inventory = _acquire_local(repo, settings.model_copy(update={"max_repo_files": 3}), "lim")
    assert len(inventory.python_files) == 3
    assert inventory.limits_hit[0].startswith("max_repo_files (3)")


def test_workspace_is_read_only_after_make_read_only(tmp_path: Path, settings: Settings) -> None:
    from backend.ingestion.workspace import cleanup_workspace, make_read_only

    workspace = create_workspace(settings, "ro-test")
    write_tree(workspace.src, {"pkg/a.py": b"x"})
    make_read_only(workspace.src)
    assert not os.access(workspace.src / "pkg" / "a.py", os.W_OK)
    with pytest.raises(PermissionError):
        (workspace.src / "pkg" / "new.py").write_text("x")
    assert cleanup_workspace(settings, "ro-test") is True
    assert not workspace.root.exists()
    assert cleanup_workspace(settings, "ro-test") is False


def test_zip_without_wrapper_uses_artifact_name(tmp_path: Path, settings: Settings) -> None:
    archive = tmp_path / "my-project.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("main.py", "print('hi')\n")
        zf.writestr("lib/util.py", "x = 1\n")
    inventory = _acquire_zip(archive, settings, "zip-name")
    assert inventory.root_name == "my-project"
    assert {f.path for f in inventory.python_files} == {"main.py", "lib/util.py"}
