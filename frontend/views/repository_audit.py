"""Repository Audit landing page: import a repository, pick the mode, start and follow the audit."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import streamlit as st

from frontend import api_client as api
from frontend import data, routes
from frontend.api_client import ApiError, Unsupported
from frontend.components.job_progress import AuditPipeline, Phase, render_failure, render_pipeline
from frontend.components.notices import NOT_AVAILABLE, show_api_error
from frontend.components.provenance import render_legend
from frontend.formatting import fmt_dt, human_bytes
from frontend.theme import esc, status_badge
from frontend.views.catalog import audit_rows

PIPELINE_KEY = "audit_pipeline"
DEFAULT_UPLOAD_LIMIT = 100 * 1024 * 1024
SOURCE_GITHUB, SOURCE_ZIP, SOURCE_LOCAL = "GitHub URL", "ZIP upload", "Local directory"

SourceKind = Literal["github", "zip", "local"]


@dataclass(frozen=True)
class ImportRequest:
    kind: SourceKind
    url: str = ""
    ref: str = ""
    local_path: str = ""
    filename: str = ""
    payload: bytes = b""
    display_name: str = ""

    def problem(self) -> str | None:
        if self.kind == "github" and not self.url.strip().startswith("https://github.com/"):
            return "Enter a public repository URL like https://github.com/owner/repo"
        if self.kind == "local" and not self.local_path.strip():
            return "Enter an absolute directory path on the API server"
        if self.kind == "zip" and not self.payload:
            return "Choose a .zip archive to upload"
        return None

    def body(self) -> dict[str, Any]:
        body: dict[str, Any]
        if self.kind == "github":
            body = {"source_type": "GITHUB_PUBLIC", "url": self.url.strip()}
            if self.ref.strip():
                body["ref"] = self.ref.strip()
        else:
            body = {"source_type": "LOCAL_DIRECTORY", "local_path": self.local_path.strip()}
        if self.display_name.strip():
            body["display_name"] = self.display_name.strip()
        return body


def render_hero() -> None:
    chips = "".join(
        f'<span class="pp-chip">{icon} {esc(text)}</span>'
        for icon, text in (
            ("🧭", "Static analysis of every Python file"),
            ("🕸️", "Module dependency graph"),
            ("🏷️", "Every result labeled by provenance"),
            ("📄", "HTML · Markdown · JSON · CSV · PDF reports"),
        )
    )
    st.markdown(
        '<div class="pp-hero"><h1>Audit a Python repository for <em>performance risks</em></h1>'
        "<p>No API key required — import a repository, get prioritized findings with exact "
        "source locations, and export a report. Runtime measurements happen only with your "
        f"explicit approval.</p>{chips}</div>",
        unsafe_allow_html=True,
    )


def _upload_limit(options: Any) -> int | None:
    if isinstance(options, dict):
        return int(options["limits"]["max_upload_bytes"])
    return None


def _source_form(options: Any) -> ImportRequest:
    local_known = isinstance(options, dict)
    local_enabled = bool(options.get("local_directory_enabled")) if local_known else True
    labels = [SOURCE_GITHUB, SOURCE_ZIP] + ([SOURCE_LOCAL] if local_enabled else [])
    tabs = st.tabs(labels, key="import_source", on_change="rerun")
    active = dict(zip(labels, tabs, strict=True))
    request = ImportRequest(kind="github")
    with active[SOURCE_GITHUB]:
        if active[SOURCE_GITHUB].open:
            url = st.text_input("Repository URL", placeholder="https://github.com/owner/repo")
            left, right = st.columns(2)
            ref = left.text_input("Branch, tag or commit (optional)", placeholder="default branch")
            name = right.text_input("Display name (optional)", key="gh_name")
            st.caption("Public repositories only; the clone is shallow and never runs hooks.")
            request = ImportRequest(kind="github", url=url, ref=ref, display_name=name)
    with active[SOURCE_ZIP]:
        if active[SOURCE_ZIP].open:
            limit = _upload_limit(options)
            shown = human_bytes(limit if limit is not None else DEFAULT_UPLOAD_LIMIT)
            note = (
                "" if limit is not None else " (server default; it may be configured differently)"
            )
            upload = st.file_uploader(
                f"Repository archive (.zip, up to {shown}{note})", type=["zip"], key="zip_file"
            )
            name = st.text_input("Display name (optional)", key="zip_name")
            st.caption(
                "Archives are extracted safely: absolute paths, `..`, links and zip bombs are "
                "rejected."
            )
            if upload is not None:
                request = ImportRequest(
                    kind="zip", filename=upload.name, payload=upload.getvalue(), display_name=name
                )
            else:
                request = ImportRequest(kind="zip")
    if local_enabled:
        with active[SOURCE_LOCAL]:
            if active[SOURCE_LOCAL].open:
                path = st.text_input("Directory on the API server", placeholder="/path/to/repo")
                name = st.text_input("Display name (optional)", key="local_name")
                if local_known:
                    roots = options.get("local_allowlist_roots", 0)
                    st.caption(
                        f"The server allowlists {roots} director{'y' if roots == 1 else 'ies'}; "
                        "the path must be inside one of them. The directory is copied, not mounted."
                    )
                else:
                    st.caption(
                        "This API version does not report its allowlist: the path must be inside "
                        "a directory listed in the server's `PERFPILOT_LOCAL_ALLOWLIST`."
                    )
                request = ImportRequest(kind="local", local_path=path, display_name=name)
    if local_known and not local_enabled:
        st.caption(
            "Local directory import is disabled on this server "
            "(set `PERFPILOT_LOCAL_ALLOWLIST` to enable it)."
        )
    return request


def _runtime_availability() -> tuple[bool, str]:
    try:
        status = data.runtime_status()
    except ApiError as exc:
        return False, f"runtime status unavailable: {exc.message}"
    if isinstance(status, Unsupported):
        return False, f"{NOT_AVAILABLE}."
    available = bool(status.get("available"))
    reason = status.get("reason") or status.get("detail") or ""
    return available, str(reason)


def _mode_selector() -> tuple[str, bool]:
    """Return (mode, allowed)."""
    choice = st.radio(
        "Analysis mode",
        ["Static Audit", "Runtime Audit"],
        horizontal=True,
        captions=[
            "Reads code only. Nothing is executed. Results are STATIC.",
            "Runs approved commands in a Docker sandbox to MEASURE hotspots.",
        ],
        key="audit_mode",
    )
    if choice == "Static Audit":
        return "STATIC", True
    available, reason = _runtime_availability()
    st.info(
        "Runtime audits never execute anything automatically: after the static audit, you review "
        "the discovered commands and approve one exact command line and its resource limits. It "
        "then runs in an isolated Docker sandbox (no network, read-only, CPU/memory/time caps).",
        icon="🛡️",
    )
    if not available:
        st.warning(f"Runtime audit is unavailable: {reason or 'no sandbox backend'}", icon="🚫")
    return "RUNTIME", available


def _ai_selector(health: dict[str, Any]) -> tuple[bool, bool, bool]:
    """Return (use_llm, consent, allowed)."""
    configured = bool(health.get("llm_configured"))
    model = health.get("llm_model")
    use_llm = st.toggle(
        "Use AI (sends redacted snippets to your configured LLM)",
        value=False,
        disabled=not configured,
        key="use_llm",
        help=(
            f"Model: {model}"
            if configured
            else "No LLM is configured on the server (PERFPILOT_LLM_BASE_URL and "
            "PERFPILOT_LLM_MODEL). Audits run fully deterministic and send nothing externally."
        ),
    )
    if not configured:
        st.caption("AI is off: no LLM configured on the server. Audits are fully deterministic.")
        return False, False, True
    if not use_llm:
        return False, False, True
    consent = st.checkbox(
        f"I consent to sending redacted code snippets (secrets replaced with [REDACTED]) to "
        f"`{model}`. AI output is labeled AI_HYPOTHESIS and is never presented as measured.",
        key="llm_consent",
    )
    return True, consent, consent


def _submit(request: ImportRequest) -> dict[str, Any]:
    if request.kind == "zip":
        return dict(api.upload_zip(request.filename, request.payload, request.display_name or None))
    return dict(api.post("/repositories", json=request.body()))


def _start(request: ImportRequest, mode: str, use_llm: bool, consent: bool) -> None:
    problem = request.problem()
    if problem:
        st.warning(problem, icon="✋")
        return
    try:
        accepted = _submit(request)
    except ApiError as exc:
        show_api_error(exc, "Import rejected")
        return
    pipeline = AuditPipeline(
        phase=Phase.IMPORTING,
        snapshot_id=accepted["subject_id"],
        mode=mode,
        use_llm=use_llm,
        llm_consent=consent,
        import_job_id=accepted["job_id"],
    )
    st.session_state[PIPELINE_KEY] = pipeline.to_state()
    data.clear_audit_caches()
    st.rerun()


def render_start_form(health: dict[str, Any]) -> None:
    with st.container(border=True):
        st.markdown("##### 1 · Choose a source")
        request = _source_form(data.import_options())
        st.markdown("##### 2 · Analysis")
        mode, mode_ok = _mode_selector()
        use_llm, consent, ai_ok = _ai_selector(health)
        busy = (
            PIPELINE_KEY in st.session_state
            and not AuditPipeline.from_state(st.session_state[PIPELINE_KEY]).finished
        )
        if st.button(
            "Start audit",
            type="primary",
            icon="🚀",
            disabled=busy or not mode_ok or not ai_ok,
            help="An audit is already running" if busy else None,
        ):
            _start(request, mode, use_llm, consent)


def render_progress() -> None:
    pipeline = render_pipeline(PIPELINE_KEY)
    if pipeline is None:
        return
    # The audit list on this page must include the run that just finished.
    data.clear_audit_caches()
    if pipeline.phase is Phase.DONE and pipeline.audit_id:
        st.success("Audit finished.", icon="✅")
        if st.button("Open audit results", type="primary", icon="📊"):
            st.session_state.pop(PIPELINE_KEY, None)
            st.switch_page(
                routes.AUDIT_RESULTS, query_params={routes.AUDIT_PARAM: pipeline.audit_id}
            )
    else:
        render_failure(pipeline)
    if st.button("Dismiss", key="dismiss_pipeline"):
        st.session_state.pop(PIPELINE_KEY, None)
        st.rerun()


def render_recent_audits(limit: int = 8) -> None:
    st.markdown("##### Recent audits")
    try:
        rows = audit_rows(limit)
    except ApiError as exc:
        show_api_error(exc, "Recent audits")
        return
    if not rows:
        st.caption("No audits yet. Import a repository on the left to run the first one.")
        return
    for row in rows:
        with st.container(border=True):
            findings = "" if row.findings_total is None else f" · {row.findings_total} findings"
            st.markdown(
                f"**{esc(row.repo_name)}** {status_badge(row.status)}<br>"
                f"<span class='pp-muted'>{esc(row.source_label)} · {row.mode.lower()}"
                f"{findings}<br>{fmt_dt(row.created_at)}</span>",
                unsafe_allow_html=True,
            )
            st.page_link(
                routes.AUDIT_RESULTS,
                label="Open results",
                icon="➡️",
                query_params={routes.AUDIT_PARAM: row.id},
            )


def render_page() -> None:
    render_hero()
    try:
        health = data.health()
    except ApiError as exc:
        show_api_error(exc)
        return
    main, side = st.columns([2.2, 1], gap="large")
    with main:
        render_start_form(health)
        render_progress()
    with side:
        render_recent_audits()
        with st.container(border=True):
            st.markdown("##### How results are labeled")
            render_legend()
