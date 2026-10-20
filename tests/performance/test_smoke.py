"""Performance smoke tests — NON-AUTHORITATIVE.

These guard against gross regressions (an accidental O(n²) endpoint, a startup that imports the
world, a report renderer that hangs). They run in-process on whatever machine executes the suite,
including noisy shared CI runners, so the thresholds are deliberately generous and the numbers
are never reported as PerfPilot measurements. Benchmarks of user code live in the sandboxed A/B
harness, not here.
"""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.jobs.worker import Worker

SHOPFAST = Path(__file__).resolve().parents[1] / "fixtures" / "repos" / "shopfast"

STARTUP_LIMIT_S = 15.0
REPORT_LIMIT_S = 30.0
GET_P95_LIMIT_MS = 750.0
CALLS_PER_ENDPOINT = 50

RecordProperty = Callable[[str, Any], None]


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20, method="inclusive")[18]


def _timed(fn: Callable[[], Any]) -> float:
    started = time.perf_counter()
    fn()
    return time.perf_counter() - started


@pytest.fixture
def audited(client: TestClient, settings: Settings) -> dict[str, str]:
    """A shopfast import plus a completed static audit, driven through the public API."""
    settings.local_allowlist = [SHOPFAST.parent]
    accepted = client.post(
        "/api/repositories", json={"source_type": "LOCAL_DIRECTORY", "local_path": str(SHOPFAST)}
    ).json()
    Worker(settings, isolate=False).drain()
    snapshot_id = accepted["subject_id"]
    audit = client.post("/api/audits", json={"snapshot_id": snapshot_id}).json()
    Worker(settings, isolate=False).drain()
    assert client.get(f"/api/jobs/{audit['job_id']}").json()["status"] == "SUCCEEDED"
    return {
        "snapshot_id": snapshot_id,
        "audit_id": audit["subject_id"],
        "repository_id": client.get(f"/api/snapshots/{snapshot_id}").json()["repository_id"],
    }


_COLD_START = """
from fastapi.testclient import TestClient
from backend.main import create_app
with TestClient(create_app()) as client:
    assert client.get("/api/health").json()["status"] == "ok"
"""


def test_cold_app_startup_time(settings: Settings, record_property: RecordProperty) -> None:
    """Fresh interpreter: imports, app factory, lifespan (schema + seeding), first request."""
    env = {**os.environ, "PERFPILOT_DATA_DIR": str(settings.data_dir)}
    elapsed = _timed(
        lambda: subprocess.run(
            [sys.executable, "-c", _COLD_START], env=env, check=True, timeout=120
        )
    )
    record_property("cold_startup_seconds", round(elapsed, 3))
    assert elapsed < STARTUP_LIMIT_S, (
        f"startup took {elapsed:.2f}s (smoke limit {STARTUP_LIMIT_S}s)"
    )


@pytest.mark.parametrize("fmt", ["html", "md", "json", "csv"])
def test_report_generation_time(
    client: TestClient,
    settings: Settings,
    audited: dict[str, str],
    fmt: str,
    record_property: RecordProperty,
) -> None:
    def generate() -> None:
        response = client.post(
            f"/api/audits/{audited['audit_id']}/reports", json={"formats": [fmt]}
        )
        assert response.status_code == 202, response.text
        Worker(settings, isolate=False).drain()
        [item] = response.json()["reports"]
        assert client.get(f"/api/jobs/{item['job_id']}").json()["status"] == "SUCCEEDED"

    elapsed = _timed(generate)
    record_property(f"report_{fmt}_seconds", round(elapsed, 3))
    assert elapsed < REPORT_LIMIT_S, f"{fmt} report took {elapsed:.2f}s (limit {REPORT_LIMIT_S}s)"


def test_get_endpoint_p95_latency(
    client: TestClient, audited: dict[str, str], record_property: RecordProperty
) -> None:
    audit, snapshot = audited["audit_id"], audited["snapshot_id"]
    endpoints = [
        "/api/health",
        "/api/jobs",
        "/api/repositories",
        f"/api/repositories/{audited['repository_id']}",
        f"/api/snapshots/{snapshot}",
        f"/api/snapshots/{snapshot}/files",
        f"/api/audits/{audit}",
        f"/api/audits/{audit}/findings",
        f"/api/audits/{audit}/graph",
        f"/api/audits/{audit}/modules",
        f"/api/audits/{audit}/recommendations",
        "/api/projects",
    ]
    slow: dict[str, float] = {}
    for url in endpoints:
        assert client.get(url).status_code == 200, url  # warm-up; must actually succeed
        samples = []
        for _ in range(CALLS_PER_ENDPOINT):
            started = time.perf_counter()
            response = client.get(url)
            samples.append((time.perf_counter() - started) * 1000)
            assert response.status_code == 200
        p95 = _p95(samples)
        record_property(f"p95_ms {url}", round(p95, 2))
        if p95 > GET_P95_LIMIT_MS:
            slow[url] = round(p95, 1)
    assert not slow, f"p95 above the {GET_P95_LIMIT_MS} ms smoke limit: {slow}"
