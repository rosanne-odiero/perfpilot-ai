"""Cached read access to the API for pages. Writes go through :mod:`frontend.api_client`.

GETs are cached briefly (keyed by ids) so reruns triggered by widgets do not refetch; anything
whose state changes while a job runs uses a short TTL, and job polling is never cached.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from frontend import api_client as api

SHORT = 5
MEDIUM = 30
LONG = 300
FINDINGS_PAGE = 500
MAX_FINDINGS = 10_000


@st.cache_data(ttl=SHORT, show_spinner=False)
def health() -> dict[str, Any]:
    return dict(api.get("/health"))


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def import_options() -> Any:
    return api.get_if_supported("/import/options")


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def runtime_status() -> Any:
    return api.get_if_supported("/runtime/status")


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def ai_status() -> Any:
    return api.get_if_supported("/ai/status")


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def optimization_api_available() -> bool:
    return api.endpoint_available("/optimizations/capability-probe")


def job(job_id: str) -> dict[str, Any]:
    return dict(api.get(f"/jobs/{job_id}"))


@st.cache_data(ttl=SHORT, show_spinner=False)
def jobs(job_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": limit}
    if job_type:
        params["job_type"] = job_type
    return list(api.get("/jobs", params=params))


@st.cache_data(ttl=SHORT, show_spinner=False)
def repositories() -> list[dict[str, Any]]:
    return list(api.get("/repositories", params={"limit": 500}))


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def repository(repository_id: str) -> dict[str, Any]:
    return dict(api.get(f"/repositories/{repository_id}"))


@st.cache_data(ttl=SHORT, show_spinner=False)
def snapshot(snapshot_id: str) -> dict[str, Any]:
    return dict(api.get(f"/snapshots/{snapshot_id}"))


@st.cache_data(ttl=LONG, show_spinner=False)
def snapshot_files(snapshot_id: str) -> list[dict[str, Any]]:
    return list(api.get(f"/snapshots/{snapshot_id}/files")["files"])


@st.cache_data(ttl=LONG, show_spinner=False)
def file_content(snapshot_id: str, path: str) -> dict[str, Any]:
    return dict(api.get(f"/snapshots/{snapshot_id}/files/content", params={"path": path}))


@st.cache_data(ttl=SHORT, show_spinner=False)
def audits(limit: int = 100) -> list[dict[str, Any]]:
    return list(api.get("/audits", params={"limit": limit}))


@st.cache_data(ttl=SHORT, show_spinner=False)
def audit(audit_id: str) -> dict[str, Any]:
    return dict(api.get(f"/audits/{audit_id}"))


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def findings(audit_id: str) -> tuple[list[dict[str, Any]], int]:
    """All findings of an audit (paged through the API), plus the server-side total."""
    items: list[dict[str, Any]] = []
    total = 0
    while len(items) < MAX_FINDINGS:
        page = api.get(
            f"/audits/{audit_id}/findings", params={"limit": FINDINGS_PAGE, "offset": len(items)}
        )
        total = int(page["total"])
        items.extend(page["items"])
        if not page["items"] or len(items) >= total:
            break
    return items, total


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def graph(audit_id: str) -> dict[str, Any]:
    return dict(api.get(f"/audits/{audit_id}/graph"))


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def modules(audit_id: str) -> list[dict[str, Any]]:
    return list(api.get(f"/audits/{audit_id}/modules"))


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def file_finding_counts(audit_id: str) -> list[dict[str, Any]]:
    return list(api.get(f"/audits/{audit_id}/files/findings-count"))


@st.cache_data(ttl=SHORT, show_spinner=False)
def reports(audit_id: str) -> list[dict[str, Any]]:
    return list(api.get(f"/audits/{audit_id}/reports"))


@st.cache_data(ttl=LONG, show_spinner=False)
def report_preview(report_id: str) -> str:
    content = api.get(f"/reports/{report_id}/preview")
    return content.decode("utf-8") if isinstance(content, bytes) else str(content)


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def audit_optional(audit_id: str, suffix: str) -> Any:
    """Audit sub-resources served by API areas that may not exist yet in this version."""
    return api.get_if_supported(f"/audits/{audit_id}/{suffix}")


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def projects() -> list[dict[str, Any]]:
    return list(api.get("/projects"))


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def workloads(project_id: str) -> list[dict[str, Any]]:
    return list(api.get(f"/projects/{project_id}/workloads"))


@st.cache_data(ttl=LONG, show_spinner=False)
def workload(workload_id: str) -> dict[str, Any]:
    return dict(api.get(f"/workloads/{workload_id}"))


@st.cache_data(ttl=LONG, show_spinner=False)
def workload_reference(workload_id: str) -> Any:
    return api.get_if_supported(f"/workloads/{workload_id}/reference")


@st.cache_data(ttl=SHORT, show_spinner=False)
def profile(profile_id: str) -> dict[str, Any]:
    return dict(api.get(f"/profiles/{profile_id}"))


@st.cache_data(ttl=LONG, show_spinner=False)
def finished_profile(profile_id: str) -> dict[str, Any]:
    """A profile whose job is terminal (immutable), cached longer for history lists."""
    return dict(api.get(f"/profiles/{profile_id}"))


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def hotspots(profile_id: str, sort: str, scope: str, limit: int = 100) -> list[dict[str, Any]]:
    params = {"sort": sort, "scope": scope, "limit": limit}
    return list(api.get(f"/profiles/{profile_id}/hotspots", params=params))


@st.cache_data(ttl=SHORT, show_spinner=False)
def benchmark(benchmark_id: str) -> dict[str, Any]:
    return dict(api.get(f"/benchmarks/{benchmark_id}"))


@st.cache_data(ttl=LONG, show_spinner=False)
def finished_benchmark(benchmark_id: str) -> dict[str, Any]:
    """A benchmark whose job is terminal (immutable), cached longer for history lists."""
    return dict(api.get(f"/benchmarks/{benchmark_id}"))


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def benchmark_samples(benchmark_id: str) -> list[dict[str, Any]]:
    return list(api.get(f"/benchmarks/{benchmark_id}/results"))


def all_workloads() -> list[dict[str, Any]]:
    return [w for p in projects() for w in workloads(p["id"])]


def clear_audit_caches() -> None:
    for fn in (audits, audit, reports, repositories, jobs, snapshot):
        fn.clear()


def clear_runtime_caches() -> None:
    for fn in (audit_optional, runtime_status, runtime_run, audit, findings, jobs):
        fn.clear()


def clear_optimization_caches() -> None:
    for fn in (analysis, candidate, workload_candidates, jobs, benchmark):
        fn.clear()


@st.cache_data(ttl=SHORT, show_spinner=False)
def runtime_run(audit_id: str, run_id: str) -> dict[str, Any]:
    return dict(api.get(f"/audits/{audit_id}/runtime/runs/{run_id}"))


@st.cache_data(ttl=SHORT, show_spinner=False)
def analysis(analysis_id: str) -> dict[str, Any]:
    return dict(api.get(f"/analyses/{analysis_id}"))


@st.cache_data(ttl=MEDIUM, show_spinner=False)
def analysis_recommendations(analysis_id: str) -> list[dict[str, Any]]:
    return list(api.get(f"/analyses/{analysis_id}/recommendations"))


@st.cache_data(ttl=SHORT, show_spinner=False)
def candidate(candidate_id: str) -> dict[str, Any]:
    return dict(api.get(f"/optimizations/{candidate_id}"))


@st.cache_data(ttl=LONG, show_spinner=False)
def candidate_diff(candidate_id: str) -> dict[str, Any]:
    return dict(api.get(f"/optimizations/{candidate_id}/diff"))


@st.cache_data(ttl=SHORT, show_spinner=False)
def workload_candidates(workload_id: str) -> Any:
    return api.get_if_supported(f"/workloads/{workload_id}/optimizations")


@st.cache_data(ttl=SHORT, show_spinner=False)
def subject_reports(path: str) -> list[dict[str, Any]]:
    """Reports of an experiment subject (``/optimizations/{id}/reports``, ``/benchmarks/...``)."""
    return list(api.get(path))


@st.cache_data(ttl=SHORT, show_spinner=False)
def experiments(limit: int = 100, workload_id: str | None = None) -> Any:
    params: dict[str, Any] = {"limit": limit}
    if workload_id:
        params["workload_id"] = workload_id
    return api.get_if_supported("/experiments", params=params)


@st.cache_data(ttl=SHORT, show_spinner=False)
def repo_candidates(audit_id: str) -> Any:
    return api.get_if_supported(f"/audits/{audit_id}/optimizations")


@st.cache_data(ttl=SHORT, show_spinner=False)
def repo_candidate(candidate_id: str) -> dict[str, Any]:
    return dict(api.get(f"/repo-optimizations/{candidate_id}"))


@st.cache_data(ttl=LONG, show_spinner=False)
def repo_diff(candidate_id: str) -> dict[str, Any]:
    return dict(api.get(f"/repo-optimizations/{candidate_id}/diff"))


def clear_repo_optimization_caches() -> None:
    for fn in (repo_candidates, repo_candidate, jobs):
        fn.clear()
