"""Approval rules: exact argv, configured image, tighter-only limits, expiry, single use."""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from backend.core.config import Settings
from backend.core.exceptions import (
    ConflictError,
    NotFoundError,
    SecurityViolationError,
    ValidationFailedError,
)
from backend.models.database import session_scope, utcnow
from backend.models.orm.audit import AuditRun, Repository, RepositorySnapshot
from backend.models.orm.runtime import RuntimeApproval, RuntimeApprovalStatus, RuntimeCommand
from backend.sandbox import approvals
from backend.sandbox.backend import SandboxLimits
from backend.sandbox.command_discovery import CommandRejectedError, discover_commands

PYTEST_ARGV = ["python", "-m", "pytest", "-q", "tests"]


def _setup(settings: Settings) -> tuple[str, str, Path]:
    workspace = settings.workspaces_dir / "snap" / "repo"
    (workspace / "tests").mkdir(parents=True)
    (workspace / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    (workspace / "Makefile").write_text("test:\n\tpytest\n")
    with session_scope() as session:
        repo = Repository(name="demo", source_type="LOCAL_DIRECTORY", source_locator="/x")
        session.add(repo)
        session.flush()
        snapshot = RepositorySnapshot(repository_id=repo.id, workspace_path=str(workspace))
        session.add(snapshot)
        session.flush()
        audit = AuditRun(snapshot_id=snapshot.id, mode="RUNTIME")
        session.add(audit)
        session.flush()
        return audit.id, snapshot.id, workspace


def _record(settings: Settings) -> tuple[str, dict[str, str]]:
    audit_id, snapshot_id, workspace = _setup(settings)
    commands = discover_commands(workspace).commands
    with session_scope() as session:
        records = approvals.record_discovered_commands(session, audit_id, snapshot_id, commands)
        by_kind = {r.kind: r.id for r in records}
    return audit_id, by_kind


def _approve(audit_id: str, command_id: str, **kwargs: object) -> str:
    with session_scope() as session:
        options: dict[str, object] = {"argv": list(PYTEST_ARGV), "approved_by": "alice"}
        options.update(kwargs)
        return approvals.approve_command(session, audit_id, command_id, **options).id  # type: ignore[arg-type]


def _approval(session: Session, approval_id: str) -> RuntimeApproval:
    approval = session.get(RuntimeApproval, approval_id)
    assert approval is not None
    return approval


def test_record_is_idempotent_and_checks_audit(settings: Settings) -> None:
    audit_id, snapshot_id, workspace = _setup(settings)
    commands = discover_commands(workspace).commands
    with session_scope() as session:
        first = approvals.record_discovered_commands(session, audit_id, snapshot_id, commands)
        second = approvals.record_discovered_commands(session, audit_id, snapshot_id, commands)
        assert [r.id for r in first] == [r.id for r in second]
        assert session.query(RuntimeCommand).count() == len(commands) == 2
        with pytest.raises(ValidationFailedError):
            approvals.record_discovered_commands(session, audit_id, "other-snapshot", commands)
        with pytest.raises(NotFoundError):
            approvals.record_discovered_commands(session, "missing", snapshot_id, commands)


def test_approve_pins_exact_argv_image_and_default_limits(settings: Settings) -> None:
    audit_id, ids = _record(settings)
    approval_id = _approve(audit_id, ids["pytest"])
    with session_scope() as session:
        approval = _approval(session, approval_id)
        assert approval.status == RuntimeApprovalStatus.APPROVED
        assert approval.argv == PYTEST_ARGV
        assert approval.image == settings.sandbox_image
        assert approval.limits == SandboxLimits.from_settings(settings).model_dump()
        assert approval.approved_by == "alice"
        assert approval.approved_at is not None
        assert not approval.allow_dependency_install


@pytest.mark.parametrize(
    "argv",
    [
        [*PYTEST_ARGV, "-p", "evil"],
        ["python", "-m", "pytest", "tests", "-q"],
        ["python", "-m", "pytest", "-q"],
        ["python", "-m", "pytest", "-q", "tests", "--rootdir=/"],
        tuple(PYTEST_ARGV),
    ],
)
def test_argv_must_match_discovered_command_exactly(settings: Settings, argv: object) -> None:
    audit_id, ids = _record(settings)
    with pytest.raises(SecurityViolationError):
        _approve(audit_id, ids["pytest"], argv=argv)


def test_information_only_commands_cannot_be_approved(settings: Settings) -> None:
    audit_id, ids = _record(settings)
    with pytest.raises(ConflictError, match="not runnable"):
        _approve(audit_id, ids["info_only"], argv=[])


def test_command_from_another_audit_is_not_found(settings: Settings) -> None:
    _, ids = _record(settings)
    with pytest.raises(NotFoundError):
        _approve("another-audit", ids["pytest"])


def test_tampered_stored_command_is_revalidated(settings: Settings) -> None:
    audit_id, ids = _record(settings)
    evil = ["python", "-m", "pytest", "-p", "evil"]
    with session_scope() as session:
        record = session.get(RuntimeCommand, ids["pytest"])
        assert record is not None
        record.command = {**record.command, "argv": evil}
    with pytest.raises(CommandRejectedError):
        _approve(audit_id, ids["pytest"], argv=evil)


def test_missing_workspace_blocks_approval(settings: Settings) -> None:
    audit_id, ids = _record(settings)
    shutil.rmtree(settings.workspaces_dir / "snap")
    with pytest.raises(ConflictError, match="workspace"):
        _approve(audit_id, ids["pytest"])


def test_limits_may_only_be_tightened(settings: Settings) -> None:
    audit_id, ids = _record(settings)
    defaults = SandboxLimits.from_settings(settings)
    tighter = defaults.model_copy(update={"memory_mb": 256, "timeout_seconds": 30})
    approval_id = _approve(audit_id, ids["pytest"], limits_override=tighter)
    with session_scope() as session:
        assert _approval(session, approval_id).limits["memory_mb"] == 256
    looser = defaults.model_copy(
        update={"memory_mb": defaults.memory_mb * 2, "cpus": defaults.cpus + 1}
    )
    with pytest.raises(ValidationFailedError) as excinfo:
        _approve(audit_id, ids["pytest"], limits_override=looser)
    assert set(excinfo.value.details["looser_than_default"]) == {"memory_mb", "cpus"}


@pytest.mark.parametrize("label", ["", "   ", "x" * 256, "bad\nname"])
def test_approved_by_label_is_validated(settings: Settings, label: str) -> None:
    audit_id, ids = _record(settings)
    with pytest.raises(ValidationFailedError):
        _approve(audit_id, ids["pytest"], approved_by=label)


def test_sdist_builds_require_dependency_install(settings: Settings) -> None:
    audit_id, ids = _record(settings)
    with pytest.raises(ValidationFailedError):
        _approve(audit_id, ids["pytest"], allow_sdist_builds=True)


def test_consume_is_single_use(settings: Settings) -> None:
    audit_id, ids = _record(settings)
    approval_id = _approve(audit_id, ids["pytest"])
    with session_scope() as session:
        execution = approvals.consume_approval(session, approval_id)
    assert execution.argv == PYTEST_ARGV
    assert execution.workdir_rel == "."
    assert execution.limits == SandboxLimits.from_settings(settings)
    with session_scope() as session:
        assert _approval(session, approval_id).status == RuntimeApprovalStatus.CONSUMED
        assert _approval(session, approval_id).consumed_at is not None
        with pytest.raises(ConflictError, match="already been used"):
            approvals.consume_approval(session, approval_id)


def test_expired_approval_cannot_be_consumed(settings: Settings) -> None:
    audit_id, ids = _record(settings)
    approval_id = _approve(audit_id, ids["pytest"])
    with session_scope() as session:
        _approval(session, approval_id).expires_at = utcnow() - timedelta(seconds=1)
    with session_scope() as session, pytest.raises(ConflictError, match="expired"):
        approvals.consume_approval(session, approval_id)
    with session_scope() as session, pytest.raises(ConflictError, match="expired"):
        approvals.consume_dependency_install(session, approval_id)


def test_ttl_comes_from_settings(settings: Settings) -> None:
    settings.sandbox_approval_ttl_seconds = 60
    audit_id, ids = _record(settings)
    with session_scope() as session:
        approval = approvals.approve_command(
            session,
            audit_id,
            ids["pytest"],
            argv=list(PYTEST_ARGV),
            approved_by="a",
            settings=settings,
        )
        assert approval.approved_at is not None
        assert approval.expires_at - approval.approved_at == timedelta(seconds=60)


def test_revoked_approval_cannot_be_used(settings: Settings) -> None:
    audit_id, ids = _record(settings)
    approval_id = _approve(audit_id, ids["pytest"])
    with session_scope() as session:
        approvals.revoke_approval(session, approval_id, reason="changed my mind")
    with session_scope() as session:
        approval = _approval(session, approval_id)
        assert approval.status == RuntimeApprovalStatus.REVOKED
        assert approval.revoke_reason == "changed my mind"
        with pytest.raises(ConflictError, match="revoked"):
            approvals.consume_approval(session, approval_id)
        with pytest.raises(ConflictError, match="revoked"):
            approvals.revoke_approval(session, approval_id)
        with pytest.raises(NotFoundError):
            approvals.revoke_approval(session, "missing")


def test_dependency_install_is_separately_approved_and_single_use(settings: Settings) -> None:
    audit_id, ids = _record(settings)
    plain = _approve(audit_id, ids["pytest"])
    with session_scope() as session, pytest.raises(ConflictError, match="not approved"):
        approvals.consume_dependency_install(session, plain)

    with_install = _approve(
        audit_id, ids["pytest"], allow_dependency_install=True, allow_sdist_builds=True
    )
    with session_scope() as session:
        grant = approvals.consume_dependency_install(session, with_install)
    assert grant.allow_sdist_builds
    assert grant.image == settings.sandbox_image
    with session_scope() as session:
        with pytest.raises(ConflictError, match="already been performed"):
            approvals.consume_dependency_install(session, with_install)
        # Installing does not consume the execution approval...
        approvals.consume_approval(session, with_install)
    # ...but once executed, a later install is refused.
    another = _approve(audit_id, ids["pytest"], allow_dependency_install=True)
    with session_scope() as session:
        approvals.consume_approval(session, another)
        with pytest.raises(ConflictError, match="already been used"):
            approvals.consume_dependency_install(session, another)
