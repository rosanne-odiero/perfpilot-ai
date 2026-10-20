"""Capture real API responses as JSON fixtures for the frontend tests.

Run against a live API that has at least one finished audit (with reports), one profile and one
benchmark of the same workload::

    .venv/bin/python scripts/capture_ui_fixtures.py --base-url http://127.0.0.1:8000

Responses are stored verbatim except that local paths (home directory, repository root) are
replaced by neutral placeholders. ``manifest.json`` maps ``"<path>[?<query>]"`` to a file name.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests" / "frontend" / "fixtures"


class Capture:
    def __init__(self, base_url: str) -> None:
        self.client = httpx.Client(base_url=f"{base_url.rstrip('/')}/api", timeout=60)
        self.manifest: dict[str, str] = {}
        self.replacements = [(str(ROOT), "/srv/perfpilot"), (str(Path.home()), "/home/user")]

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, str):
            for needle, placeholder in self.replacements:
                value = value.replace(needle, placeholder)
            return value
        if isinstance(value, list):
            return [self._sanitize(v) for v in value]
        if isinstance(value, dict):
            return {k: self._sanitize(v) for k, v in value.items()}
        return value

    def get(self, route: str, key: str | None = None, **params: Any) -> Any:
        response = self.client.get(route, params=params or None)
        response.raise_for_status()
        body = response.json() if "json" in response.headers["content-type"] else response.text
        body = self._sanitize(body)
        manifest_key = key or route
        name = re.sub(r"[^A-Za-z0-9]+", "_", manifest_key).strip("_")[:120] + ".json"
        (OUT / name).write_text(json.dumps(body, indent=1, sort_keys=True) + "\n", "utf-8")
        self.manifest[manifest_key] = name
        return body


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--audit-id", help="Defaults to the newest succeeded audit")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.json"):
        old.unlink()
    cap = Capture(args.base_url)

    cap.get("/health")
    cap.get("/import/options")
    cap.get("/repositories")
    audits = cap.get("/audits")
    succeeded = [a for a in audits if a["status"] == "SUCCEEDED"]
    audit = (
        next(a for a in succeeded if a["id"] == args.audit_id) if args.audit_id else succeeded[0]
    )
    audit_id, snapshot_id = audit["id"], audit["snapshot_id"]
    cap.get(f"/audits/{audit_id}")
    findings = cap.get(f"/audits/{audit_id}/findings", limit=500, offset=0)
    for suffix in ("graph", "modules", "files/findings-count", "reports"):
        cap.get(f"/audits/{audit_id}/{suffix}")
    cap.get(f"/snapshots/{snapshot_id}")
    cap.get(f"/snapshots/{snapshot_id}/files")
    for path in sorted({f["location"]["file_path"] for f in findings["items"]}):
        cap.get(
            f"/snapshots/{snapshot_id}/files/content",
            key=f"/snapshots/{snapshot_id}/files/content?path={path}",
            path=path,
        )
    html = next(r for r in cap.get(f"/audits/{audit_id}/reports") if r["format"] == "html")
    cap.get(f"/reports/{html['id']}/preview")

    projects = cap.get("/projects")
    workloads = cap.get(f"/projects/{projects[0]['id']}/workloads")
    jobs = cap.get("/jobs", limit=200)
    profiled = [
        j["subject_id"]
        for j in jobs
        if j["job_type"] == "workload.profile" and j["status"] == "SUCCEEDED"
    ]
    for job in jobs:
        if job["job_type"] == "workload.benchmark" and job["status"] == "SUCCEEDED":
            cap.get(f"/benchmarks/{job['subject_id']}")
            cap.get(f"/benchmarks/{job['subject_id']}/results")
    for profile_id in profiled:
        cap.get(f"/profiles/{profile_id}")
        cap.get(f"/profiles/{profile_id}/hotspots", limit=50, sort="exclusive", scope="all")
    for workload in workloads:
        cap.get(f"/workloads/{workload['id']}")
        if workload["has_reference_candidate"]:
            cap.get(f"/workloads/{workload['id']}/reference")
    (OUT / "manifest.json").write_text(json.dumps(cap.manifest, indent=1, sort_keys=True) + "\n")
    print(f"captured {len(cap.manifest)} responses into {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
