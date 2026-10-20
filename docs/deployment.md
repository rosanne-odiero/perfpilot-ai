# Deployment

PerfPilot is a single-user, local-first tool. The supported deployment is the Docker Compose stack
in [`docker-compose.yml`](../docker-compose.yml): the **API**, the **job worker** and the
**Streamlit UI** from one application [`Dockerfile`](../Dockerfile), plus the separate **sandbox
image** the worker starts for runtime audits. Running the three processes directly with `make dev`
works too (see the README).

## Quick start

```bash
cp .env.example .env                              # optional; every variable has a default
docker compose --profile sandbox build sandbox-image   # or: make sandbox-image
make compose-up                                   # docker compose up --build -d
open http://127.0.0.1:8501                        # UI   (API: http://127.0.0.1:8000/docs)
make compose-down                                 # stop; data is kept
```

`make compose-up` exports `PERFPILOT_HOST_DATA_DIR=$(pwd)/data` and creates the directory. When
calling `docker compose` directly, set it yourself (it must be an **absolute** path and must exist):

```bash
export PERFPILOT_HOST_DATA_DIR="$PWD/data" && mkdir -p "$PERFPILOT_HOST_DATA_DIR"
docker compose up --build -d
```

Behind a proxy or on a network that cannot reach PyPI, pass a package mirror at **build time**
only (it is not stored in the image): `UV_INDEX_URL=https://mirror.example/simple make compose-build`
for the app image and `PIP_INDEX_URL=… make sandbox-image` for the sandbox image.

## Services

| Service | Image target | Command | Ports (host) | Docker socket |
|---|---|---|---|---|
| `api` | `app` | `uvicorn backend.main:app` | `127.0.0.1:${PERFPILOT_API_PORT:-8000}` | **no** |
| `worker` | `worker` (= `app` + docker CLI) | `python -m backend.jobs.worker` | none | **yes** |
| `ui` | `app` | `streamlit run frontend/app.py` (`PERFPILOT_API_BASE_URL=http://api:8000`) | `127.0.0.1:${PERFPILOT_UI_PORT:-8501}` | **no** |
| `sandbox-image` | `docker/sandbox.Dockerfile` | build-only (`--profile sandbox`) | — | — |

Image properties: `python:3.12-slim`, dependencies installed from the committed `uv.lock`
(`uv export --locked`, `--require-hashes`), non-root user `10001:10001`, pango/harfbuzz and a font
so PDF reports use **WeasyPrint**, `git` for GitHub imports, and a `HEALTHCHECK`
(`perfpilot-healthcheck api|ui|worker`). Every service runs with a read-only root filesystem,
`cap_drop: ALL`, `no-new-privileges` and a tmpfs `/tmp`. Ports bind to `127.0.0.1` only: the API
has no authentication (see [security.md](security.md)). The worker waits for the API to be
healthy; the API applies database migrations on startup (`alembic upgrade head`, serialised with a
lock file so the worker can start at the same time).

## Data and the "same path" rule

| What | Where | Why |
|---|---|---|
| Artifacts (`artifacts/`), repository workspaces (`workspaces/`), upload staging | `PERFPILOT_HOST_DATA_DIR` on the host, bind-mounted at the **same absolute path** in `api` and `worker`, with `PERFPILOT_DATA_DIR` set to that path | see below |
| SQLite database | named volume `perfpilot-db` at `/var/lib/perfpilot/db` (`PERFPILOT_DATABASE_URL`) | SQLite's WAL locking and shared memory are not reliable over the file-sharing layer Docker Desktop uses for bind mounts; a named volume is a native filesystem |
| Local-directory imports (optional) | `PERFPILOT_IMPORT_DIR` on the host → `/imports` read-only in `api` and `worker`; allowlisted via `PERFPILOT_LOCAL_ALLOWLIST=/imports` | the importer only reads allowlisted directories; read-only so nothing can modify your checkout |

**Why the same path?** The worker does not run sandboxes itself: it asks the **host's** Docker
daemon (through the socket) to start a sibling container with bind mounts such as
`--mount type=bind,source=<data>/workspaces/<id>/repo,target=/workspace,readonly`. The daemon
resolves `source` on the **host**, not inside the worker container. If the worker saw the data at
`/data` while the host has it at `/home/me/perfpilot/data`, every sandbox mount would point at a
non-existent (or, worse, a different) host directory. Mounting the data directory at its host path
inside the worker, and setting `PERFPILOT_DATA_DIR` to it, makes every path the worker computes a
valid host path. For the same reason, anything mounted into a sandbox must live under the data dir;
the runtime profiler stages its harness under `artifacts/runtime-runs/<run>/in/` instead of
mounting it from the installed package. The API shares the mount so both processes agree on the
absolute paths stored in the database.

Import a directory from the host:

```bash
PERFPILOT_IMPORT_DIR="$HOME/code" make compose-up
curl -X POST http://127.0.0.1:8000/api/repositories -H 'content-type: application/json' \
  -d '{"source_type": "LOCAL_DIRECTORY", "local_path": "/imports/my-repo"}'
```

On **Linux** the containers' user must be able to write the bind-mounted data dir: build with your
ids (`PERFPILOT_UID=$(id -u) PERFPILOT_GID=$(id -g) make compose-build`) or `chown 10001:10001`
the directory. Docker Desktop (macOS/Windows) maps ownership automatically.

## The Docker socket trade-off

Access to `/var/run/docker.sock` is **root-equivalent on the host**: anyone who controls the worker
process can start a privileged container that mounts `/`. PerfPilot limits the exposure
(ADR-005): only the worker mounts the socket; the API and UI, which handle untrusted HTTP input,
never do; the worker only runs fixed, validated `docker` argv lists built by
`backend/sandbox/docker_backend.py`; untrusted code only ever runs inside the hardened sandbox
container (no network, read-only, non-root, no capabilities, resource limits — see
[sandbox.md](sandbox.md)). The worker is added to the socket's group via
`PERFPILOT_DOCKER_GID` (default `0`, which matches Docker Desktop's `root:root` socket; on Linux use
`$(stat -c %g /var/run/docker.sock)`, usually the `docker` group).

Ways to reduce the risk further, strongest first:

1. **Rootless Docker** (or `userns-remap`) on the host: the daemon, and therefore the socket, is not
   host root. Set `DOCKER_SOCKET=$XDG_RUNTIME_DIR/docker.sock`.
2. **A socket proxy** that only allows the endpoints the worker uses (`containers/create`,
   `start`, `wait`, `kill`, `inspect`, `delete`, `images/*/json`, `info`, `version`), e.g.
   `tecnativa/docker-socket-proxy` with `CONTAINERS=1 IMAGES=1 INFO=1 POST=1`. Remove the socket
   mount from `worker` and set `DOCKER_HOST=tcp://socket-proxy:2375` (the backend passes
   `DOCKER_HOST` through to the CLI). Note that a proxy that allows `containers/create` still lets
   a compromised worker create a container with arbitrary mounts; it narrows, not removes, the risk.
3. **Run the worker on the host** (`make worker`) and keep only the API and UI in containers. Then
   no container has the socket, and the "same path" rule is trivially satisfied.

Without the socket (e.g. you delete the mount), everything except runtime audits keeps working:
the runtime status endpoint reports the sandbox as unavailable with the reason.

## Configuration

Settings are environment variables with the `PERFPILOT_` prefix, read from the process environment
and from `.env` (git-ignored; `api` and `worker` load it via `env_file`, the UI does not, because it
never needs secrets). Compose-level variables:

| Variable | Default | Purpose |
|---|---|---|
| `PERFPILOT_HOST_DATA_DIR` | `$PWD/data` | Absolute host path of the data directory (same path inside containers) |
| `PERFPILOT_IMPORT_DIR` | `./docker/imports` (empty) | Host directory exposed read-only at `/imports` for local imports |
| `PERFPILOT_API_PORT` / `PERFPILOT_UI_PORT` | `8000` / `8501` | Host ports (bound to 127.0.0.1) |
| `PERFPILOT_DOCKER_GID` | `0` | Group that owns the Docker socket |
| `DOCKER_SOCKET` | `/var/run/docker.sock` | Host socket mounted into the worker |
| `PERFPILOT_UID` / `PERFPILOT_GID` | `10001` | Build-time uid/gid of the container user |
| `UV_INDEX_URL` / `PIP_INDEX_URL` | empty (PyPI) | Build-time package mirrors (app / sandbox image) |

Application settings (generated from `backend/core/config.py`; regenerate with `make openapi`):

<!-- settings-table:start (generated by scripts/export_settings.py) -->
| Variable | Type | Default | Description |
|---|---|---|---|
| `PERFPILOT_ENVIRONMENT` | str | `development` | Free-form deployment label (informational). |
| `PERFPILOT_LOG_LEVEL` | str | `INFO` | Root log level for the JSON logs (DEBUG, INFO, WARNING, ...). |
| `PERFPILOT_DATA_DIR` | path | `data` | Artifacts, workspaces and (by default) the SQLite database. |
| `PERFPILOT_DATABASE_URL` | str |  | SQLAlchemy URL; defaults to `sqlite:///<data_dir>/perfpilot.sqlite3`. |
| `PERFPILOT_DATABASE_SCHEMA_MODE` | migrate \| create_all | `migrate` | `migrate` applies Alembic migrations at startup; `create_all` is for throwaway test databases. |
| `PERFPILOT_API_BASE_URL` | str | `http://127.0.0.1:8000` | URL the Streamlit UI uses to reach the API. |
| `PERFPILOT_MAX_REQUEST_BYTES` | int | `1048576` | Body limit for every API request except ZIP uploads (413 above it). |
| `PERFPILOT_CORS_ALLOWED_ORIGINS` | list | `http://localhost:8501,http://127.0.0.1:8501` | Comma-separated browser origins allowed to call the API. |
| `PERFPILOT_OTEL_ENABLED` | bool | `false` | Emit OpenTelemetry spans if the `opentelemetry` API package is installed. |
| `PERFPILOT_LOCAL_ALLOWLIST` | list |  | Comma-separated directories the local-directory importer may read. |
| `PERFPILOT_MAX_UPLOAD_BYTES` | int | `104857600` | Largest accepted ZIP upload. |
| `PERFPILOT_MAX_REPO_FILES` | int | `20000` | Stop ingesting after this many files. |
| `PERFPILOT_MAX_REPO_BYTES` | int | `524288000` | Stop ingesting after this many bytes. |
| `PERFPILOT_MAX_FILE_BYTES` | int | `2097152` | Files larger than this are skipped. |
| `PERFPILOT_MAX_PYTHON_FILES` | int | `5000` | Python files analysed per snapshot. |
| `PERFPILOT_MAX_ARCHIVE_ENTRIES` | int | `50000` | ZIP entries accepted before the archive is refused. |
| `PERFPILOT_MAX_COMPRESSION_RATIO` | float | `200.0` | ZIP-bomb guard: maximum uncompressed/compressed ratio. |
| `PERFPILOT_CLONE_TIMEOUT_SECONDS` | int | `180` | Timeout for `git clone` of GitHub repositories. |
| `PERFPILOT_GITHUB_ALLOWED_HOSTS` | list | `github.com` | Hosts accepted in GitHub URLs (exact match). |
| `PERFPILOT_WORKER_POLL_SECONDS` | float | `1.0` | Idle poll interval of the job worker. |
| `PERFPILOT_JOB_LEASE_SECONDS` | int | `60` | Job lease length; expired leases are reclaimed by another worker. |
| `PERFPILOT_JOB_DEFAULT_TIMEOUT_SECONDS` | int | `1800` | Default per-job timeout. |
| `PERFPILOT_JOB_MAX_ATTEMPTS` | int | `2` | Attempts before a job whose worker died is marked failed. |
| `PERFPILOT_LLM_BASE_URL` | str |  | OpenAI-compatible endpoint. Unset = deterministic advisor only. |
| `PERFPILOT_LLM_API_KEY` | secret |  | API key for the LLM endpoint (secret; never logged or exported). |
| `PERFPILOT_LLM_MODEL` | str |  | Model or deployment name. |
| `PERFPILOT_LLM_TIMEOUT_SECONDS` | float | `60.0` | Per-request LLM timeout. |
| `PERFPILOT_LLM_MAX_RETRIES` | int | `2` | Retries for transient LLM failures. |
| `PERFPILOT_LLM_MAX_TOKENS_PER_AUDIT` | int | `60000` | Token budget for one audit's AI stage. |
| `PERFPILOT_LLM_MAX_TOKENS_PER_CALL` | int | `6000` | Token cap for a single LLM call. |
| `PERFPILOT_LLM_INPUT_COST_PER_1K` | float | `0.0` | USD per 1K input tokens (cost accounting only). |
| `PERFPILOT_LLM_OUTPUT_COST_PER_1K` | float | `0.0` | USD per 1K output tokens (cost accounting only). |
| `PERFPILOT_SANDBOX_BACKEND` | str | `docker` | Execution backend for runtime audits (`docker`). |
| `PERFPILOT_SANDBOX_IMAGE` | str | `perfpilot-sandbox:latest` | Sandbox image the worker runs untrusted code in. |
| `PERFPILOT_SANDBOX_CPUS` | float | `2.0` | CPU limit per sandbox run. |
| `PERFPILOT_SANDBOX_MEMORY_MB` | int | `2048` | Memory limit per sandbox run. |
| `PERFPILOT_SANDBOX_PIDS_LIMIT` | int | `256` | Process limit per sandbox run. |
| `PERFPILOT_SANDBOX_TIMEOUT_SECONDS` | int | `600` | Wall-clock limit per sandbox run. |
| `PERFPILOT_SANDBOX_TMPFS_MB` | int | `256` | Size of the sandbox's writable /tmp. |
| `PERFPILOT_SANDBOX_OUTPUT_MAX_BYTES` | int | `16777216` | Captured stdout/stderr/output cap per run. |
| `PERFPILOT_SANDBOX_DOCKER_PATH` | path |  | Absolute path of the docker CLI (default: fixed lookup). |
| `PERFPILOT_SANDBOX_INSTALL_TIMEOUT_SECONDS` | int | `900` | Timeout of the separately approved dependency install. |
| `PERFPILOT_SANDBOX_PIP_INDEX_URL` | str |  | Package index for the approved dependency-install step. |
| `PERFPILOT_SANDBOX_APPROVAL_TTL_SECONDS` | int | `86400` | How long a runtime approval stays valid. |
<!-- settings-table:end -->

## Database migrations

The schema is managed with Alembic (`alembic.ini`, `migrations/`). The API, the worker and
`perfpilot init-db` run `alembic upgrade head` on startup. A database created before migrations
existed is stamped at the baseline if (and only if) its tables match it exactly.

| Command | Purpose |
|---|---|
| `perfpilot init-db` / `make init-db` | Apply pending migrations to the configured database |
| `make migration m="add foo"` | Autogenerate the next revision from the ORM models |
| `make migration-check` | Fail if the models and the migration head differ (also run by CI and `tests/unit/test_migrations.py`) |
| `make migration-baseline` | Regenerate `0001` from the current models — only while it is the only revision (before the first release); refuses otherwise |

The test suite builds its throwaway per-test databases with `create_all`
(`PERFPILOT_DATABASE_SCHEMA_MODE=create_all`) for speed; `test_migrations.py` proves the migration
head produces the identical schema.

## Backups and persistence

All state is in two places: the `perfpilot-db` volume and the host data directory. Back up both
while the stack is stopped (or at least while no job is running):

```bash
docker compose stop api worker
docker run --rm -v perfpilot-ai_perfpilot-db:/db -v "$PWD/backup:/backup" python:3.12-slim \
  python -c "import sqlite3; sqlite3.connect('/db/perfpilot.sqlite3').backup(sqlite3.connect('/backup/perfpilot.sqlite3'))"
tar -C "$PERFPILOT_HOST_DATA_DIR" -czf backup/artifacts.tgz artifacts
docker compose start api worker
```

(The volume name is prefixed with the compose project name, i.e. the checkout's directory name;
`docker volume ls | grep perfpilot-db` shows it.) Workspaces need no backup: they are re-created by
re-importing. `docker compose down` keeps both; `docker compose down -v` deletes the database
volume.

## Reclaiming disk space

Imported repositories are kept as read-only working copies under `workspaces/`. Delete old ones
(audit results, findings and reports are kept; a pruned snapshot must be re-imported before it can
be audited again):

```bash
docker compose exec worker perfpilot prune-workspaces --older-than-days 7
docker compose exec worker perfpilot prune-workspaces --all
```

Leftover sandbox containers (only possible if the worker was killed mid-run) carry a label:
`docker ps -a --filter label=perfpilot.sandbox=1`.

## Observability

Every process logs one JSON object per line to stderr (`docker compose logs`). Records emitted
while a job runs carry `job_id`, `job_type` and the subject IDs from the job (`audit_id`,
`snapshot_id`, `run_id`, …). Job stages log `job stage finished` with `stage`, `status` and
`duration_ms`; instrumented operations (each audit stage, runtime/workload profiling, LLM calls,
patch generation, validation, benchmarking, report rendering) log `operation finished` with
`operation`, `duration_ms` and `status`. LLM calls add `model`, `endpoint_host`, `input_tokens`,
`output_tokens`, `usage_estimated` and `cost_usd` — never prompts, completions or keys. Log text is
scrubbed for common credential formats.

**OpenTelemetry (optional).** PerfPilot has no OpenTelemetry dependency. If you install
`opentelemetry-api` (plus an SDK/exporter of your choice, configured the standard way, e.g.
`opentelemetry-instrument` or `OTEL_*` variables) and set `PERFPILOT_OTEL_ENABLED=true`, the same
operations are emitted as spans named after `operation`, with `perfpilot.*` attributes. Without the
package the flag only logs a warning.
