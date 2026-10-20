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
| `frontend/components/` | Reusable widgets with pure, unit-tested builders: `job_progress`, `findings_table`, `file_explorer`, `code_view`, `diff_view`, `dependency_graph`, `benchmark_charts`, `metric_cards`, `provenance`, `notices` |
| `frontend/data.py` | `st.cache_data` GET wrappers keyed by ids (5 s for lists/state, 30 s–5 min for immutable data). Job polling is never cached |
| `frontend/formatting.py` | Pure formatting helpers (units, dates, GitHub permalinks) |

## Pages and the API endpoints they use

| Page / section | Endpoints |
|---|---|
| **Repository Audit** (landing): hero, import tabs, analysis mode, AI toggle, live progress, recent audits | `GET /api/health`, `GET /api/import/options`, `POST /api/repositories`, `POST /api/repositories/upload`, `POST /api/audits`, `GET /api/jobs/{id}`, `GET /api/audits`, `GET /api/runtime/status`* |
| **Audit Results · Overview**: summary cards, severity/category charts, top risks, technologies (evidence on hover), repository facts, languages, dependencies, skipped files, limits, warnings | `GET /api/audits/{id}`, `GET /api/snapshots/{id}`, `GET /api/repositories`, `GET /api/audits/{id}/findings` |
| **· Files**: inventory with per-file finding counts, folder/language/search filters, source with highlighted finding lines; sensitive files show "content withheld" | `GET /api/snapshots/{id}/files`, `GET /api/audits/{id}/files/findings-count`, `GET /api/snapshots/{id}/files/content` |
| **· Findings**: filters (severity, category, rule, provenance, path, min priority, module from the graph), detail panel with source, suggestion, trade-offs, complexity estimate, priority factors, GitHub permalink pinned to the commit | `GET /api/audits/{id}/findings` (paged, filtered client-side) |
| **· Dependency graph**: Plotly network (seeded spring / Kamada-Kawai layout), size = fan-in, colour = findings, ◆ = entrypoint, red = cycle, top-N cap; clicking a module filters findings; module table; external imports | `GET /api/audits/{id}/graph`, `GET /api/audits/{id}/modules` |
| **· AI insights** | `GET /api/audits/{id}/recommendations`*, `/insights`*, `/llm-usage`* |
| **· Runtime**: sandbox status, discovered commands, explicit approval (exact argv + limits), MEASURED hotspots and correlations | `GET /api/runtime/status`*, `GET /api/audits/{id}/runtime/commands`*, `POST /api/audits/{id}/runtime/approvals`*, `GET /api/audits/{id}/runtime`* |
| **· Export**: HTML/Markdown/JSON/CSV/PDF generation, live job progress, downloads, HTML preview | `POST /api/audits/{id}/reports`, `GET /api/audits/{id}/reports`, `GET /api/reports/{id}/download`, `GET /api/reports/{id}/preview` |
| **Workload Lab · Explorer**: entrypoint and fixture source, sizes, output contract, author's bottleneck note, baseline vs bundled reference (side-by-side + unified diff) | `GET /api/projects`, `GET /api/projects/{id}/workloads`, `GET /api/workloads/{id}`, `GET /api/workloads/{id}/reference` |
| **· Profile**: start a profile, hotspot table (exclusive vs cumulative, user/all), memory (tracemalloc vs RSS, labelled separately), warnings, environment | `POST /api/profiles`, `GET /api/profiles/{id}`, `GET /api/profiles/{id}/hotspots`, `GET /api/jobs?job_type=workload.profile` |
| **· Benchmark**: A/B against the bundled reference (or a validated candidate once available), classification, speedup with CI, raw-sample box/strip plot, per-pair ratios, memory, correctness, noise warnings, environment | `POST /api/benchmarks`, `GET /api/benchmarks/{id}`, `GET /api/benchmarks/{id}/results`, `GET /api/jobs?job_type=workload.benchmark` |
| **· Optimize** | `POST /api/analyses`*, `/api/optimizations/...`* |
| **Reports & History** | `GET /api/audits`, `GET /api/audits/{id}/reports`, `GET /api/repositories`, profile/benchmark runs via `GET /api/jobs` |
| **System Status** | `GET /api/health`, `GET /api/import/options`, `GET /api/runtime/status`*, `GET /api/ai/status`*, `GET /api/jobs`, `POST /api/jobs/{id}/cancel` |

\* Served by API areas that may not exist in a given API version. The client treats a route-level
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
- **Safety**: report previews render in an iframe with a `data:` URL (opaque origin, isolated from
  the app). Code views escape all content. External AI needs the server to have an LLM configured,
  the per-audit toggle, and an explicit consent checkbox.

## Tests

`tests/frontend/` contains unit tests for the pure helpers and `streamlit.testing.v1.AppTest`
smoke tests for every page. The tests replace `api_client.request`/`download` with a fake that
serves JSON recorded from the real API (`tests/frontend/fixtures/`, regenerate with
`scripts/capture_ui_fixtures.py --base-url <api>`; local paths are replaced by placeholders).
