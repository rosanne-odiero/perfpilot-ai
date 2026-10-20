# Frontend (Streamlit)

`make ui` runs `streamlit run frontend/app.py`. The UI only talks to the HTTP API through
`frontend/api_client.py` (base URL from `PERFPILOT_API_BASE_URL`, default `http://127.0.0.1:8000`);
it never imports backend code or opens the database. Theme settings live in
`.streamlit/config.toml` (dark base); custom CSS and the Plotly template `perfpilot` are in
`frontend/theme.py`.

## Layout

| Path | Contents |
|---|---|
| `frontend/app.py` | `st.navigation` with sections; landing page = Repository Audit |
| `frontend/app_pages/` | One thin script per page (calls `frontend/views/<page>.py`). Not named `pages/`: Streamlit would also auto-register that directory as a legacy multipage app on direct URL loads |
| `frontend/views/` | Page sections (audit tabs, Workload Lab tabs, history, status, sidebar) |
| `frontend/components/` | Reusable widgets with pure, unit-tested builders: `job_progress`, `findings_table`, `file_explorer`, `code_view`, `diff_view` (side-by-side + unified), `dependency_graph`, `benchmark_charts`, `metric_cards`, `provenance`, `notices`, `runtime` (approval body, tables), `optimization` (lifecycle steppers, allowed actions), `report_export` |
| `frontend/views/candidate_review.py` | Diff review, approve/reject, validate and check tables shared by workload and repository candidates (same lifecycle) |
| `frontend/data.py` | `st.cache_data` GET wrappers keyed by ids (5 s for lists/state, 30 s–5 min for immutable data). Job polling is never cached |
| `frontend/formatting.py` | Pure formatting helpers (units, dates, GitHub permalinks) |

## Pages and the API endpoints they use

| Page / section | Endpoints |
|---|---|
| **Repository Audit** (landing): hero, import tabs, analysis mode with real sandbox availability, AI toggle with a banner stating exactly what is sent (and to which host, within which budget) next to the consent checkbox, live progress, recent audits | `GET /api/health`, `GET /api/import/options`, `GET /api/ai/status`, `GET /api/runtime/status`, `POST /api/runtime/status/refresh`, `POST /api/repositories`, `POST /api/repositories/upload`, `POST /api/audits`, `GET /api/jobs/{id}`, `GET /api/audits` |
| **Audit Results · Overview**: summary cards, severity/category charts, top risks, technologies (evidence on hover), repository facts, languages, dependencies, skipped files, limits, warnings | `GET /api/audits/{id}`, `GET /api/snapshots/{id}`, `GET /api/repositories`, `GET /api/audits/{id}/findings` |
| **· Files**: inventory with per-file finding counts, filters, source with highlighted finding lines; citations from other tabs open a file here at the cited lines; sensitive files show "content withheld" | `GET /api/snapshots/{id}/files`, `GET /api/audits/{id}/files/findings-count`, `GET /api/snapshots/{id}/files/content` |
| **· Findings**: filters (severity, category, rule, provenance, path, min priority, module from the graph, with runtime evidence), detail panel with code role and static execution path, source, suggestion, trade-offs, complexity estimate, priority factors, MEASURED evidence next to the STATIC claim, GitHub permalink at the commit, **Optimize this finding** | `GET /api/audits/{id}/findings`, `POST /api/audits/{id}/optimizations` |
| **· Dependency graph**: Plotly network (seeded layout), size = fan-in, colour = findings, ◆ = entrypoint, red = cycle, top-N cap; clicking a module filters findings | `GET /api/audits/{id}/graph`, `GET /api/audits/{id}/modules` |
| **· AI insights**: recommendation cards (rationale, strategy, effort, uncertainty, qualitative effect, risks, clickable citations, suggested diff as a diff; STATIC = deterministic advisor, AI_HYPOTHESIS = model output with its id), architecture insights, LLM usage (calls, tokens, cost, budget, rejected citations/claims/items, fallbacks, stage status) | `GET /api/audits/{id}/recommendations`, `/insights`, `/llm-usage` |
| **· Runtime**: worker-observed sandbox status (re-check), discovered commands, approval form pinning the exact argv with image and tighter-only limits, optional networked dependency install (warned), live run progress, run results (exit codes, timeout/OOM, wall/CPU, RSS vs tracemalloc), MEASURED hotspots, correlated findings (static claim vs measured evidence, with Optimize this finding), runtime-only hotspots, environment and output tails | `GET /api/runtime/status`, `GET /api/audits/{id}/runtime/commands`, `POST /api/audits/{id}/runtime/approvals`, `GET /api/audits/{id}/runtime`, `GET /api/audits/{id}/runtime/runs/{run_id}` |
| **· Optimize** (repository candidates): which approved commands validation/benchmarking will reuse, lifecycle stepper (Finding → Patch → Approve → Validate → Benchmark → Done), measured evidence, no-safe-patch reasons, LLM proposer usage, side-by-side + unified diff, approve / reject with reason, sandbox validation (checks, original vs patched suites, regressions), sandbox benchmark (raw-sample box/strip and per-pair ratios, speedup with CI, classification, peak RSS, what is timed) | `GET /api/audits/{id}/optimizations`, `GET /api/repo-optimizations/{id}`, `/diff`, `POST .../approve`, `.../reject`, `.../validate`, `.../benchmark` |
| **· Export**: HTML/Markdown/JSON/CSV/PDF generation, live job progress, downloads, HTML preview | `POST /api/audits/{id}/reports`, `GET /api/audits/{id}/reports`, `GET /api/reports/{id}/download`, `GET /api/reports/{id}/preview` |
| **Workload Lab · Explorer**: entrypoint and fixture source, sizes, output contract, author's bottleneck note, baseline vs bundled reference | `GET /api/projects`, `GET /api/projects/{id}/workloads`, `GET /api/workloads/{id}`, `GET /api/workloads/{id}/reference` |
| **· Profile**: start a profile, hotspot table (exclusive vs cumulative), memory (tracemalloc vs RSS), warnings, environment, **Optimize this hotspot** (analysis with this profile as evidence) | `POST /api/profiles`, `GET /api/profiles/{id}`, `GET /api/profiles/{id}/hotspots`, `GET /api/jobs?job_type=workload.profile` |
| **· Benchmark**: A/B against the bundled reference, classification, speedup with CI, raw samples, per-pair ratios, memory, correctness, noise warnings, environment; experiment report for reference benchmarks | `POST /api/benchmarks`, `GET /api/benchmarks/{id}`, `GET /api/benchmarks/{id}/results`, `POST/GET /api/benchmarks/{id}/reports` |
| **· Optimize** (workload deep dive): analysis (new or reusing a profile), recommendation cards with MEASURED profiler evidence vs AI_HYPOTHESIS qualitative impact, candidate generation (LLM proposer only with explicit consent when no deterministic patch exists), diff review, approve / reject, validation with per-check results, benchmark of the **validated** candidate only (API 409s are shown), result linked to the candidate, lifecycle stepper and history, experiment report export | `POST /api/analyses`, `GET /api/analyses/{id}`, `/recommendations`, `GET /api/workloads/{id}/optimizations`, `POST /api/optimizations`, `GET /api/optimizations/{id}`, `/diff`, `POST .../approve`, `.../reject`, `.../validate`, `POST /api/benchmarks`, `POST/GET /api/optimizations/{id}/reports` |
| **Reports & History**: audits with their reports, workload experiments (state, validation, verdict, speedup with CI) with their reports, profiles, benchmarks | `GET /api/audits`, `GET /api/audits/{id}/reports`, `GET /api/repositories`, `GET /api/experiments`, `GET /api/optimizations/{id}/reports`, `GET /api/benchmarks/{id}/reports`, runs via `GET /api/jobs` |
| **System Status** | `GET /api/health`, `GET /api/import/options`, `GET /api/runtime/status`, `GET /api/ai/status`, `GET /api/jobs`, `POST /api/jobs/{id}/cancel` |

Sections backed by the AI, runtime, optimization, repository-optimization and experiment APIs keep
working against older API versions: the client treats a route-level
404 (`{"detail": "Not Found"}`, no error envelope) or 405 as "endpoint missing"
(`ApiError.endpoint_missing`, `api_client.get_if_supported`, `api_client.endpoint_available`) and
the section shows *"This capability is not available in this API version"*. A 404 **with** the
error envelope means a missing resource and is shown as an error.

## Conventions

- **Real data only.** Every number comes from an API response; empty states say what to do next.
- **Provenance** badges (MEASURED green, STATIC blue, AI_HYPOTHESIS purple) appear on every
  result block; the legend is on the landing page and in the sidebar. AI output is labelled
  "Hypothesis — not measured".
- **Errors** use the API error envelope (`code`, `message`, `details`); an unreachable API shows
  how to start it instead of a stack trace.
- **Jobs**: long-running calls return 202; `components/job_progress.py` polls `GET /api/jobs/{id}`
  from an `st.fragment(run_every=1s)` and reruns the page when the job is terminal. The import →
  audit pipeline is a pure state machine (`advance`) tested with fake callables.
- **Explicit approvals**: runtime runs pin the exact argv (copied verbatim from the discovered
  command), limits can only be tightened, and the confirmation checkbox is required. Candidates
  need an explicit approval and the UI never offers benchmarking a candidate that is not
  `VALIDATED` with validation `PASSED`; repository validation/benchmarking only reuse commands
  already approved in the Runtime tab.
- **Safety**: report previews render in an iframe with a `data:` URL (opaque origin, isolated from
  the app). Code views escape all content. External AI needs the server to have an LLM configured,
  the per-audit toggle, and an explicit consent checkbox.

## Tests

`tests/frontend/` contains unit tests for the pure helpers and `streamlit.testing.v1.AppTest`
smoke tests for every page. The tests replace `api_client.request`/`download` with a fake that
serves JSON recorded from the real API (`tests/frontend/fixtures/`, regenerate with
`scripts/capture_ui_fixtures.py --base-url <api>` against an API with a measured runtime audit,
a repository candidate and a workload deep dive; local paths are replaced by placeholders). The
`legacy_api` fixture simulates an API version without the newer areas to test the fallbacks.
