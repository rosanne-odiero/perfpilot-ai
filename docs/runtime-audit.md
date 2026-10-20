# Runtime Audit

A Runtime Audit adds real measurements to a Repository Audit. PerfPilot proposes test and benchmark
commands found by static discovery. A person approves one exact command. The worker runs it once
in the Docker sandbox under the profiling harness. The measured hotspots are then correlated with
the static findings. Repository code never runs on the host or in the API process
([ADR-004, ADR-005](decisions.md)); see [sandbox.md](sandbox.md) for the isolation itself.

| Module | Responsibility |
|---|---|
| `backend/runtime/discovery.py` | `RUNTIME_DISCOVERY` stage; parks the audit at `AWAITING_RUNTIME_APPROVAL` |
| `backend/runtime/backends.py` | How the worker builds the backend; the availability it records for the API |
| `backend/runtime/service.py` | Approvals → runs and jobs, correlation persistence, API queries (no sandbox access) |
| `backend/runtime/execution.py` | The `runtime.profile` job: dependency step, sandbox run, outcome, artifacts, hotspots |
| `backend/runtime/profile_stats.py` | Validates the harness's JSON pstats table; container path mapping; attributed time |
| `backend/audit/correlation.py` | Hotspot → symbol → finding mapping, `measured_factor`, runtime-only hotspots |
| `backend/models/orm/runtime_profiles.py` | `runtime_profile_runs`, `runtime_hotspots`, `runtime_hotspot_correlations`, `runtime_backend_status` |
| `backend/api/routes/runtime.py` | The HTTP API below |
| `backend/jobs/handlers/runtime.py` | `runtime.profile` and `runtime.check_backend` |
| `backend/reporting/providers/runtime.py` | Report section 7 |

## Flow

1. `POST /api/audits {"snapshot_id": …, "mode": "RUNTIME"}` runs the static stages, then
   `RUNTIME_DISCOVERY`. Discovery only reads files (`backend/sandbox/command_discovery.py`); the
   proposals are stored as `RuntimeCommand` rows with `STATIC` provenance. The worker also checks
   the sandbox and records the result. The job then finishes with audit `status=SUCCEEDED` (or
   `PARTIAL`) and `stage=AWAITING_RUNTIME_APPROVAL`; `summary.runtime` holds
   `runtime_available`, `unavailable_reason` and the command counts.
2. `GET /api/audits/{id}/runtime/commands` lists the proposals (argv, working dir, evidence,
   risk, whether dependencies are needed, runnable or not and why).
3. `POST /api/audits/{id}/runtime/approvals` approves **one** command for **one** run and returns
   HTTP 202 with `subject_type="runtime_run"`. The job `runtime.profile` is queued with
   `max_attempts=1`.
4. The worker runs the job. On success the audit goes `RUNTIME_PROFILING` → `CORRELATING` →
   `COMPLETED`. A failed run puts it back at `AWAITING_RUNTIME_APPROVAL` (or keeps `COMPLETED` if an
   earlier run succeeded) and adds an audit warning.
5. `GET /api/audits/{id}/runtime` and `GET /api/audits/{id}/runtime/runs/{run_id}` return the
   results; reports gain a measured section 7.

If the sandbox is unavailable, step 1 still completes. Discovery runs, the static results are
complete, `runtime_available=false` with the reason is recorded, and a warning says so. Approvals
are then refused with **503 `unavailable`** and the reason. There is no host fallback.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/runtime/status` | Availability **as last observed by a worker**, reason, image, default limits (`network: none`), dependency-install timeout, approval TTL, `checked_at`. Only safe details are exposed (server version, OS, cgroup version, image id); host paths such as the docker CLI location are dropped. |
| `POST /api/runtime/status/refresh` | 202; queues `runtime.check_backend` so a worker re-checks the sandbox |
| `GET /api/audits/{id}/runtime/commands` | Discovered commands |
| `POST /api/audits/{id}/runtime/approvals` | `{command_id, argv, approved_by, allow_dependency_install=false, allow_sdist_builds=false, limits_override?, trace_memory=true}` → 202 |
| `GET /api/audits/{id}/runtime` | Runs, availability, the measured run's top repository hotspots, correlations, runtime-only hotspots, warnings |
| `GET /api/audits/{id}/runtime/runs/{run_id}` | One run: status, exit codes, timeout/OOM flags, durations, memory, environment, stdout/stderr tails, artifact ids, top 100 hotspots by exclusive time, correlations |

The API process never constructs or calls a sandbox backend: status comes from the
`runtime_backend_status` row written by the worker, and approving only writes rows and enqueues a
job. A test replaces `DockerSandboxBackend`'s methods with ones that fail and exercises every
endpoint to check this.

## Approvals

Approval rules come from `backend/sandbox/approvals.py`:

* `argv` must equal the discovered argv exactly (no extra, missing or reordered arguments: 400
  `security_violation`), and the command must be runnable (409 otherwise). The stored command is
  re-validated against the snapshot workspace.
* The approval pins the argv, working directory, the configured image and the limits, records
  `approved_by` and expires (`PERFPILOT_SANDBOX_APPROVAL_TTL_SECONDS`).
* `limits_override` may only **tighten** the defaults (422 with `looser_than_default` otherwise).
* Approvals are single use. The worker consumes the approval atomically right before execution; a
  consumed, revoked or expired approval fails the job (`approval has already been used`, …).
  Only RUNTIME audits whose static stages finished accept approvals, and only one run per audit
  can be queued or running at a time (409).
* `allow_dependency_install` approves the separate, network-enabled dependency step. The worker
  installs from `requirements.txt` in the command's working directory or the repository root, or
  else from PEP 621 `[project.dependencies]` plus the `test`/`tests`/`testing`/`dev` extras (see
  [sandbox.md](sandbox.md#dependency-preparation)). The prepared `/deps` dir is per snapshot, so
  later runs of the same snapshot mount it read-only even without a new install. If the step
  fails, or no manifest exists, the run fails and the approval is revoked before anything ran.
* When a run cannot start (sandbox unavailable at run time, dependency failure) its approval is
  revoked, so it can never be used later.

## Execution

The worker writes a harness request and runs the trusted harness
(`backend/profiling/harness_entry.py`, see [methodology.md](methodology.md)) in `command` mode
(simplified; the full flag set is in [sandbox.md](sandbox.md#container-hardening)):

```
docker run … --network none --read-only … \
  --mount <workspace>:/workspace:ro  --mount <run>/out:/out \
  --mount harness_entry.py:/perfpilot/harness_entry.py:ro --mount <run>/in/request.json:/perfpilot/request.json:ro \
  [--mount <deps>:/deps:ro] --workdir /workspace/<workdir> perfpilot-sandbox:latest \
  python -P -s -B /perfpilot/harness_entry.py /perfpilot/request.json /out/result.json
```

The approved argv is mapped to a harness command: `python -m pytest [args]` → `pytest` (first
positional argument as target, `.` if none), `python -m <module>` → `module`, `python <file.py>` →
`script`. `PYTHONHASHSEED=0` is set. The harness profiles the command with cProfile and, by default,
tracemalloc concurrently (`trace_memory=false` gives cleaner CPU timings without the traced-memory
figure). It writes `result.json`, `profile.prof` and `profile_stats.json` to `/out`.

**Outcome.** Classified in this order: OOM-killed → `OOM_KILLED`; wall-clock limit → `TIMED_OUT`;
no harness result → `FAILED`; the command raised → `FAILED` (exception type and redacted message);
non-zero command exit code (pytest exit 1 = test failures) → `FAILED`; non-zero container exit →
`FAILED`; otherwise `SUCCEEDED`. A `SUCCEEDED` run without a valid stats table also becomes
`FAILED`. **Only `SUCCEEDED` runs get hotspots and correlations.** A failed command is reported
as a successful job with a failed run; the job itself fails only when it could not do its work
(sandbox unavailable, sandbox error, unusable approval).

**Stored per run** (`runtime_profile_runs`): status and reason, exact argv, working dir,
`approved_by`, limits, exit code of the container and of the command, `timed_out`, `oom_killed`,
duration including container start/stop, wall and CPU time under the profiler, total profiled time,
peak RSS (`getrusage` `ru_maxrss` inside the container), RSS at exit, tracemalloc peak and retained
bytes, the environment fingerprint (backend, server version, image, image digest, Python version
and implementation inside the container, platform, `network: none`, working dir, limits, profiler,
dependencies, hash seed), redacted stdout/stderr tails (last 8,000 characters), warnings and the
artifact ids of `profile.prof`, `profile_stats.json` and `result.json` (marked untrusted: they are
written by the profiled process).

**Hotspots.** The worker never unmarshals `profile.prof` (`marshal` is unsafe for untrusted
input). It validates `profile_stats.json` (format, types, finite non-negative times, name lengths,
at most 200,000 functions and 2,000,000 call edges) and builds hotspots with
`backend/profiling/hotspots.py`. Container paths are mapped: `/workspace/<p>` → `<p>` (repository
code), `/perfpilot/*` → `<harness>`, `/deps/*` and `…/site-packages/*` → `<site-packages>/…`,
`/usr/local/lib/python3.X/*` → `<stdlib>/…`; anything else is reduced to `<external>/<name>`. All
repository hotspots (up to 2,000) and the top 200 others by exclusive time are stored. Each
hotspot also gets an **attributed time**:

```
attributed_time(F) = min(cumtime(F), tottime(F) + Σ cumtime(C ← F) for callees C that are not repository code)
attributed_share   = attributed_time / Σ tottime(all functions)
```

This is the time spent on `F`'s own lines, including library calls made from them, e.g.
`cursor.execute` in an N+1 loop or `list.count` in a quadratic condition, whose exclusive time
belongs to a builtin. Attributed shares overlap and are never summed.

## Correlation

`backend/audit/correlation.py`, run after every successful run:

1. **Hotspot → symbol.** A repository hotspot maps to the indexed symbol with the same file, first
   line (`SymbolInfo.line_start` includes decorators, like `co_firstlineno`) and name.
   `<lambda>`, `<genexpr>` and similar code objects map to the innermost enclosing function.
   `<module>` code maps to the module. Hotspots that match nothing are counted as unmapped.
2. **Symbol → findings.** A finding belongs to the innermost indexed symbol containing its first
   line; findings outside any symbol belong to the module.
3. **Evidence and re-ranking.** For every finding whose symbol was measured:

   ```
   measured_factor = 1 + 2.0 × attributed_share          (1.0 … 3.0, MEASURED_WEIGHT = 2.0)
   priority_score  = static_priority_score × measured_factor
   ```

   `attributed_share` sums the attributed time of every hotspot mapped to the symbol (capped at
   the function's cumulative time). The finding **keeps its own provenance (`STATIC`)** for the
   static claim. A `MeasuredEvidence` block with provenance `MEASURED` is attached (run id,
   function, internal/cumulative/attributed time, calls, exclusive and attributed share,
   `measured_factor`, hotspot file and line). `priority_factors` gains `static_priority_score`,
   `measured_factor` and `measured_attributed_share`, and the finding rows are re-sorted.
4. **Runtime-only hotspots.** Repository functions with at least 1% attributed share and no static
   finding (import-time `<module>` code excluded), up to 25.

Findings in functions the command never executed keep their static score and get no evidence; not
being exercised is not evidence of being cold. Evidence always reflects the **most recent
successful run**: every finding is reset to its static score before new evidence is applied.
Correlations are stored per run.

On the ShopFast fixture, `python benchmarks/run_workload.py` makes `reconcile` (nested loop, rule
`PP-LOOP-002`) the top measured hotspot and the top-ranked finding. `order_summaries` (N+1 query),
`duplicate_skus`, `restock_order`, `filter_discontinued` and `bundle_combinations` are correlated
to their findings, and `build_catalog` is a runtime-only hotspot.

## Reporting

The `runtime-audit` report provider fills section 7 for each successful run: command, profiler,
wall and CPU time, **peak RSS** and **peak traced memory (tracemalloc)** as separately labelled
figures with notes on what each includes, the top 15 repository hotspots with their correlated
finding ids, the environment and limits, the `measured_factor` formula and the runtime-only
hotspots. Correlated finding cards in section 5 show a `MEASURED` evidence line next to the
finding's own `STATIC` badge. Failed, timed-out, OOM-killed or unavailable runs are only listed as
limitations, without numbers. Audits without a successful run, including all static audits, keep
"Not measured — static audit only." in section 7.

## Testing

* Unit and API tests inject test doubles through `backend.runtime.backends.set_backend_factory`, a
  worker-side test seam with no configuration equivalent. `SubprocessHarnessBackend`
  (`tests/runtime_support.py`) simulates only the container boundary: it maps `/workspace`, `/out`
  and `/perfpilot/*` to their host sources and runs the real harness on the trusted ShopFast
  fixture, so assertions are made on real cProfile and tracemalloc output. `ScriptedBackend`
  returns fixed OOM/timeout/failure results and `UnavailableBackend` reports no sandbox.
* A test builds the real `docker run` argv (without executing it) for the request the worker
  sends, so the worker's mounts pass `DockerSandboxBackend`'s path policy.
* `tests/runtime/test_runtime_docker_e2e.py` (`@pytest.mark.docker`) runs everything against the
  real sandbox. It is skipped with the reason when Docker or `perfpilot-sandbox:latest` is
  missing: `make sandbox-image && make test-docker`.

## Limitations

* **One run, one process.** Each approval executes the command once. Timings include cProfile
  (and, by default, tracemalloc) overhead and Docker/VM effects; use shares, not absolute times,
  and use the A/B benchmark for "did it get faster".
* **Profiled code can lie.** The harness runs in the same process as the repository code, which
  can tamper with the profiler or its output. Results are validated as data and labelled
  `MEASURED` from the sandbox, but they are not attested.
* **Attribution is approximate.** Library calls that call back into repository code (e.g.
  `sorted(key=…)`, `asyncio.run`) count for both the caller and the callee. cProfile aggregates by
  function, so the time cannot be split between findings in the same function.
* **Coverage.** Only code paths the approved command exercises are measured. Code reached only
  through subprocesses or threads that cProfile does not follow is not profiled.
* **Mapping.** Functions defined dynamically (`exec`, decorators that replace code objects,
  generated code) do not map to indexed symbols and are counted as unmapped.
* **Status freshness.** `GET /api/runtime/status` shows the worker's last observation. The job
  re-checks the sandbox before running and fails with `unavailable` if it has gone away.
* **Cancellation** is checked between steps. Cancelling while the container runs terminates the
  job process, which can leave the container behind (labelled `perfpilot.sandbox=1`, see
  [sandbox.md](sandbox.md)); the run then stays in its last state.
