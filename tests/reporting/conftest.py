"""Fixtures for reporting tests: realistic audit rows written straight to the database."""

from __future__ import annotations

import pytest
from report_fixtures import AuditFactory, create_audit

from backend.core.config import Settings


@pytest.fixture
def make_audit(settings: Settings) -> AuditFactory:
    return create_audit
