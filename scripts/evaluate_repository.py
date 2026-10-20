"""Evaluate a repository end to end, exactly as an end user would, and save what they would see.

Drives the public HTTP API in-process with the persistent job worker (one child process per job):
import → static (or runtime) audit → recommendations → optional approved runtime profiling →
report export. Writes the downloaded reports plus ``summary.json`` to ``--out``.

Examples::

    # Local repository (the path must be inside PERFPILOT_LOCAL_ALLOWLIST; set automatically here)
    python scripts/evaluate_repository.py --local ~/code/myrepo --out data/evaluations/myrepo

    # Public GitHub repository at a ref, Markdown + PDF only
    python scripts/evaluate_repository.py --github https://github.com/pallets/itsdangerous \
        --formats md,pdf --out data/evaluations/itsdangerous

    # Runtime audit: approve one discovered command (exact argv) and run it in the Docker sandbox
    python scripts/evaluate_repository.py --local ./repo --mode RUNTIME \
        --approve "python -m pytest -q" --allow-dependency-install --out data/evaluations/repo

    # Shareable summary without paths, snippets or names (safe for private repositories)
    python scripts/evaluate_repository.py --local ./private-repo --anonymize --formats json

Nothing here bypasses the product: the same validation, sandbox and approval rules apply.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--local", type=Path, help="local repository directory")
    source.add_argument("--github", help="public GitHub repository URL")
    parser.add_argument("--ref", help="branch, tag or commit for --github")
    parser.add_argument("--mode", choices=["STATIC", "RUNTIME"], default="STATIC")
    parser.add_argument("--approve", help="exact discovered command to run (RUNTIME mode)")
    parser.add_argument("--allow-dependency-install", action="store_true")
    parser.add_argument("--use-llm", action="store_true", help="use the configured LLM (consent)")
    parser.add_argument("--formats", default="html,md,json,csv,pdf")
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--data-dir", type=Path, help="PerfPilot data dir (default: <out>/data)")
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="keep the imported working copy (deleted by default; results are kept)",
    )
    parser.add_argument(
        "--anonymize",
        action="store_true",
        help="summary without file paths, symbols, snippets or repository names",
    )
    return parser.parse_args(argv)


def _anon(value: str) -> str:
    return "h:" + hashlib.sha256(value.encode()).hexdigest()[:10]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    out: Path = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    data_dir = (args.data_dir or out / "data").expanduser().resolve()
    os.environ["PERFPILOT_DATA_DIR"] = str(data_dir)
    if args.local:
        local = args.local.expanduser().resolve()
        allow = os.environ.get("PERFPILOT_LOCAL_ALLOWLIST", "")
        os.environ["PERFPILOT_LOCAL_ALLOWLIST"] = ",".join(p for p in (allow, str(local)) if p)

    from fastapi.testclient import TestClient

    from backend.core.config import get_settings, reset_settings_cache
    from backend.jobs.worker import Worker
    from backend.main import create_app

    reset_settings_cache()
    settings = get_settings()
    timings: dict[str, float] = {}

    def drain() -> None:
        Worker(settings, isolate=True).drain()

    def timed(name: str, started: float) -> None:
        timings[name] = round(time.perf_counter() - started, 3)

    with TestClient(create_app()) as api:

        def ok(response: Any, expected: int = 202) -> dict[str, Any]:
            if response.status_code != expected:
                raise SystemExit(
                    f"{response.request.url.path}: {response.status_code} {response.text}"
                )
            body: dict[str, Any] = response.json()
            return body

        def job(job_id: str) -> dict[str, Any]:
            record: dict[str, Any] = api.get(f"/api/jobs/{job_id}").json()
            if record["status"] != "SUCCEEDED":
                raise SystemExit(f"job {record['job_type']} {record['status']}: {record['error']}")
            return record

        started = time.perf_counter()
        if args.local:
            ref = {
                "source_type": "LOCAL_DIRECTORY",
                "local_path": str(args.local.expanduser().resolve()),
            }
        else:
            ref = {"source_type": "GITHUB_PUBLIC", "url": args.github, "ref": args.ref}
        accepted = ok(api.post("/api/repositories", json=ref))
        drain()
        job(accepted["job_id"])
        snapshot_id = accepted["subject_id"]
        snapshot = api.get(f"/api/snapshots/{snapshot_id}").json()
        timed("import_s", started)

        started = time.perf_counter()
        body = {
            "snapshot_id": snapshot_id,
            "mode": args.mode,
            "use_llm": args.use_llm,
            "llm_consent": args.use_llm,
        }
        accepted = ok(api.post("/api/audits", json=body))
        drain()
        job(accepted["job_id"])
        audit_id = accepted["subject_id"]
        timed("audit_s", started)

        runtime: dict[str, Any] | None = None
        if args.mode == "RUNTIME":
            commands = api.get(f"/api/audits/{audit_id}/runtime/commands").json()
            runtime = {"discovered_commands": [shlex.join(c["argv"]) for c in commands]}
            if args.approve:
                wanted = shlex.split(args.approve)
                command = next((c for c in commands if c["argv"] == wanted), None)
                if command is None:
                    raise SystemExit(f"{args.approve!r} was not discovered: {runtime}")
                started = time.perf_counter()
                approval = {
                    "command_id": command["id"],
                    "argv": command["argv"],
                    "approved_by": "evaluate_repository.py",
                    "allow_dependency_install": args.allow_dependency_install,
                }
                accepted = ok(api.post(f"/api/audits/{audit_id}/runtime/approvals", json=approval))
                drain()
                run = api.get(
                    f"/api/audits/{audit_id}/runtime/runs/{accepted['subject_id']}"
                ).json()
                timed("runtime_profile_s", started)
                runtime["run"] = {
                    k: run.get(k)
                    for k in (
                        "status",
                        "failure_reason",
                        "exit_code",
                        "command_exit_code",
                        "timed_out",
                        "oom_killed",
                        "wall_time_ms",
                        "cpu_time_ms",
                        "memory",
                    )
                }
                runtime["run"]["hotspots"] = len(run.get("hotspots") or [])
                runtime["run"]["correlations"] = len(run.get("correlations") or [])

        audit = api.get(f"/api/audits/{audit_id}").json()
        findings: list[dict[str, Any]] = []
        while True:
            page = api.get(
                f"/api/audits/{audit_id}/findings", params={"limit": 200, "offset": len(findings)}
            ).json()
            findings.extend(page["items"])
            if not page["items"] or len(findings) >= page["total"]:
                break
        recommendations = api.get(f"/api/audits/{audit_id}/recommendations").json()
        insights = api.get(f"/api/audits/{audit_id}/insights").json()

        started = time.perf_counter()
        formats = [f.strip() for f in args.formats.split(",") if f.strip()]
        ok(api.post(f"/api/audits/{audit_id}/reports", json={"formats": formats}))
        drain()
        saved: list[str] = []
        for report in api.get(f"/api/audits/{audit_id}/reports").json():
            if report["status"] != "SUCCEEDED":
                continue
            download = api.get(f"/api/reports/{report['id']}/download")
            target = out / f"report.{report['format']}"
            target.write_bytes(download.content)
            saved.append(target.name)
        timed("reports_s", started)

    name = snapshot.get("root_name") or ""
    summary: dict[str, Any] = {
        "repository": _anon(name) if args.anonymize else name,
        "commit": None if args.anonymize else snapshot.get("commit_sha"),
        "mode": args.mode,
        "audit_status": audit["status"],
        "audit_summary": {
            k: v
            for k, v in audit["summary"].items()
            if not (args.anonymize and k in {"import_roots"})
        },
        "inventory": snapshot.get("counts"),
        "languages": snapshot.get("languages"),
        "technologies": sorted(t["name"] for t in snapshot.get("technologies", [])),
        "dependencies": len(snapshot.get("dependencies", [])),
        "findings_total": len(findings),
        "findings_by_rule": dict(Counter(f["rule_id"] for f in findings).most_common()),
        "findings_by_severity": dict(Counter(f["severity"] for f in findings).most_common()),
        "findings_by_role": dict(
            Counter(f.get("code_role") or "UNKNOWN" for f in findings).most_common()
        ),
        "findings_with_measured_evidence": sum(1 for f in findings if f.get("measured")),
        "recommendations_total": len(recommendations),
        "recommendations_by_effort": dict(
            Counter(r["effort"] for r in recommendations).most_common()
        ),
        "recommendations_by_provenance": dict(
            Counter(r["provenance"] for r in recommendations).most_common()
        ),
        "insights": [i["title"] for i in insights] if not args.anonymize else len(insights),
        "top_findings": [
            {
                "rule_id": f["rule_id"],
                "severity": f["severity"],
                "role": f.get("code_role"),
                "priority": f["priority_score"],
                "location": _anon(f["location"]["file_path"])
                if args.anonymize
                else f"{f['location']['file_path']}:{f['location']['line_start']}",
            }
            for f in findings[:10]
        ],
        "runtime": runtime,
        "timings_s": timings,
        "reports": saved,
    }
    if not args.keep_workspace:
        from backend.ingestion.workspace import cleanup_workspace

        cleanup_workspace(settings, snapshot_id)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "audit_status",
                    "findings_total",
                    "recommendations_total",
                    "timings_s",
                    "reports",
                )
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
