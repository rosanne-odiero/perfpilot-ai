# Architecture

> Living document. The full roadmap is in [IMPLEMENTATION_PLAN.md](../IMPLEMENTATION_PLAN.md).

## Processes

| Process | Command | Responsibility |
|---|---|---|
| API | `make api` → `uvicorn backend.main:app` | Validate input, persist records, enqueue jobs, serve results. Never executes repository code. |
| Worker | `make worker` → `python -m backend.jobs.worker` | Claims jobs from the DB and runs each in a fresh child process (timeouts, cancellation, crash isolation). |
| UI | `make ui` → `streamlit run frontend/app.py` | Talks to the API only (`frontend/api_client.py`). |
| Sandbox | Docker container started by the worker | Runs imported (untrusted) repository code for Runtime Audits. No network, resource-limited. |

## Extension points (no central registries to edit)

| To add… | Put it in… | Discovery |
|---|---|---|
| ORM tables | a new module in `backend/models/orm/` | `import_all_models()` imports every module |
| API routes | a module in `backend/api/routes/` exposing `router = APIRouter(...)` | mounted under `/api` automatically |
| Background work | a module in `backend/jobs/handlers/` using `@job_handler("domain.action")` | `discover_handlers()` |
| Streamlit pages | `frontend/app_pages/<name>.py` (thin) + an entry in `PAGES` in `frontend/app.py`; sections in `frontend/views/`, widgets in `frontend/components/` | `st.navigation` (see `docs/frontend.md`) |

## Data contracts

`backend/models/audit_schemas.py` defines the Pydantic models passed between pipeline stages:

```
RepositoryRef ─► provider ─► AcquiredSource ─► inventory ─► RepositoryInventory
                                                  │
                                    indexer ─► CodeIndex (ModuleInfo, SymbolInfo, DependencyGraph)
                                                  │
                                 rule engine ─► Finding[]  (provenance STATIC)
                                                  │
                                     advisor ─► Recommendation[] / ArchitectureInsight[]
                                                  │        (STATIC or AI_HYPOTHESIS)
                               runtime audit ─► Finding.measured (provenance MEASURED)
                                                  │
                                     reports ─► HTML / Markdown / JSON / CSV / PDF
```

ORM rows store these models as JSON (`model_dump(mode="json")`). Columns that are filtered often
(severity, file path, priority) are promoted to real columns.

The Static Audit (indexing, rules, prioritization, the `audit.run` stage pipeline, and
`/api/audits`) is described in [static-analysis.md](static-analysis.md).

## Jobs

`enqueue_job(session, job_type, payload, idempotency_key=...)` creates a `QUEUED` row. The worker
claims it with a conditional `UPDATE` and holds a lease that it renews with a heartbeat. If a worker
dies, the next one requeues the job (up to `max_attempts` attempts) or marks it `FAILED`. Handlers report
progress through `ctx.progress(stage, fraction, message)`. Each progress call also checks whether the job
was cancelled. Long-running endpoints return `202` with a `JobAccepted` body, and the UI polls
`GET /api/jobs/{id}`.

## Provenance

Every finding, recommendation, and metric carries a `Provenance`:
`MEASURED` (this environment ran the code), `STATIC` (deterministic analysis), or
`AI_HYPOTHESIS` (LLM or heuristic suggestion). Reports and the UI render these labels everywhere.
