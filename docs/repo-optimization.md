# Repository optimization (Runtime Audit deep dive)

> Phase 8 "Hotspot → deep-dive" and the Phase 6 LLM proposer. Code:
> `backend/optimization/repo_{candidates,workspace,sandbox,validation,benchmark}.py`,
> `backend/optimization/{junit,llm_proposer}.py`, ORM `backend/models/orm/repo_optimization.py`,
> API models `backend/models/repo_optimization_api.py`, routes
> `backend/api/routes/repo_optimizations.py`, jobs `backend/jobs/handlers/repo_optimization.py`,
> report provider `backend/reporting/providers/repo_optimization.py`.

```
finding (ideally MEASURED) -> candidate patch -> (explicit) approve
    -> validate: repository tests in the sandbox, original vs. patched
    -> benchmark: approved workload in the sandbox, paired A/B -> report sections 10-12
```

This closes the loop of a [Runtime Audit](runtime-audit.md) for imported repositories. It reuses
the generic patch engine, validator and state machine of the
[workload deep dive](optimization-workflow.md), the [sandbox](sandbox.md), and the benchmark
statistics and classifier ([methodology.md](methodology.md)). Repository code only ever runs in the
Docker sandbox, and only through commands the user approved for the audit. The API process never
talks to Docker (ADR-005): validation and benchmarks are worker jobs.

## 1. Lifecycle

The candidate uses the same state machine as workload candidates
(`backend/optimization/state_machine.py`), with the same guards:

```
RECOMMENDED -> PATCH_PROPOSED -> AWAITING_APPROVAL -(approve)-> VALIDATING -> VALIDATED
            -> BENCHMARKING -> COMPLETED                  REJECTED / FAILED are terminal
```

* No safe patch: `RECOMMENDED -> FAILED` with the reason (`outcome: "no_safe_patch"`).
* The patch validator refuses the diff: `PATCH_PROPOSED -> REJECTED` (`rejected_by:
  "patch-validator"`, one reason per violation, `outcome: "rejected_by_validator"`).
* Validation `PASSED` → `VALIDATED`; `FAILED` → `FAILED`; `INCONCLUSIVE` → back to
  `AWAITING_APPROVAL` (still approved, so it can be retried).
* Benchmark: `SUCCEEDED` with identical inputs → `COMPLETED` (whatever the classification); only
  the candidate's runs failed → `FAILED`; the original's run failed or the sandbox broke →
  back to `VALIDATED` for a retry.
* The snapshot's tree hash is re-checked before approval, validation and benchmarking, and in the
  worker before and after every sandbox step. A change fails the candidate
  (`original_tree_changed`).

**Provenance** of a candidate: `STATIC` when the deterministic engine produced it,
`AI_HYPOTHESIS` for any other proposer (the LLM, or a registered proposer), and `MEASURED` only
once it is `COMPLETED` (validated *and* benchmarked). The finding's own evidence is reported
separately as `evidence: "MEASURED" | "STATIC"`.

## 2. Candidate generation

`POST /api/audits/{audit_id}/optimizations {"finding_id"}` (`repo_candidates.create_candidate`):

1. The audit must have finished (`SUCCEEDED`/`PARTIAL`); the finding must belong to it (404
   otherwise). Findings with `MEASURED` evidence are the intended input; static-only findings are
   allowed and labelled `evidence: "STATIC"`.
2. The target file must be in the snapshot inventory with a content hash. The patch engine verifies
   the file's SHA-256 against it before reading (`409 source_changed`).
3. Proposers: the deterministic AST templates (`PP-LOOP-001`, `PP-LOOP-002` joins and
   `next(...)`/`any(...)` lookups, `PP-COLL-001`, `PP-STR-001`, `PP-REGEX-001`; see
   [optimization-workflow.md](optimization-workflow.md)). The source root is the snapshot, so a
   callee's return annotation in another file of the repository can serve as type evidence.
   When they find no safe patch **and** the audit was created with `llm_consent: true` **and** a
   model is configured, the LLM proposer (section 6) is asked. Every refusal is kept in
   `proposer_attempts`. Otherwise the outcome is `no_safe_patch` with the reason.
4. The strict patch validator runs against the snapshot: only the finding's file may change; tests,
   fixtures, CI, env/secret files, build files and benchmark harness files are protected, **plus**
   every file the audit's discovered commands measure with (script commands' `.py` files and
   pytest path arguments, resolved against the command's working directory). Python checks:
   compiles, public signatures unchanged, no benchmark gaming (caches, globals, timer/`os`/`sys`
   imports, `exec`/`eval`/`open`, …).
5. The patch is materialised in `<data_dir>/workspaces/repo-optimizations/<candidate_id>/src`, a
   copy-on-write mirror of the snapshot: files are hard links where the filesystem allows (else
   copies), the patched file is written as a new inode (temporary file + `os.replace`), and the
   copy is left read-only like the snapshot. The copy must differ from the snapshot in exactly the
   target file, and the snapshot's tree hash must be unchanged afterwards. Rejecting a candidate
   deletes its workspace. The worker rebuilds a missing copy and requires the recorded tree hash.

## 3. API

All bodies are JSON; errors use the standard envelope `{"error": {"code", "message", "details"}}`.
Long-running endpoints return HTTP 202 with `JobAccepted`
(`{"job_id", "status", "subject_type", "subject_id"}`); poll `GET /api/jobs/{job_id}` and then the
candidate.

| Method & path | Body | Response |
|---|---|---|
| `POST /api/audits/{audit_id}/optimizations` | `{"finding_id": str}` | `201 RepoCandidate` (also for `no_safe_patch` and validator rejections; see `outcome`) |
| `GET /api/audits/{audit_id}/optimizations` | | `200 [RepoCandidate]`, oldest first |
| `GET /api/repo-optimizations/{id}` | | `200 RepoCandidate` |
| `GET /api/repo-optimizations/{id}/diff` | | `200 {"candidate_id", "generated_by", "unified_diff", "files": [{"path", "original", "patched", "original_sha256", "patched_sha256"}]}`; `404` when there is no diff |
| `POST /api/repo-optimizations/{id}/approve` | `{"approved_by": str = "local-user"}` | `200 RepoCandidate`; `409 invalid_state_transition` unless `AWAITING_APPROVAL` with a valid diff and still pending |
| `POST /api/repo-optimizations/{id}/reject` | `{"reason": str, "rejected_by": str = "local-user"}` | `200 RepoCandidate` (`REJECTED`, final) |
| `POST /api/repo-optimizations/{id}/validate` | optional `{"approval_id": str}` | `202 JobAccepted` (`subject_type: "repo_validation_run"`) |
| `GET /api/repo-optimizations/{id}/validation` | | `200 RepoValidation`; `404` if never validated |
| `POST /api/repo-optimizations/{id}/benchmark` | optional `{"warmups": 0..10 = 1, "runs": 2..50 = 10, "threshold_percent": (0,100) = 5.0, "seed": int = 0, "approval_id": str}` | `202 JobAccepted` (`subject_type: "repo_benchmark_run"`) |
| `GET /api/repo-optimizations/{id}/benchmark` | | `200 RepoBenchmark`; `404` if never benchmarked |

`actor` strings match `^[A-Za-z0-9_.@ -]{1,128}$`; ids `^[A-Za-z0-9_-]{1,64}$`; unknown fields are
rejected (422).

**Refusals of `validate` / `benchmark`** (nothing is queued, nothing runs):

* `409 invalid_state_transition`: not approved / not `AWAITING_APPROVAL` (validate), not
  `VALIDATED` (benchmark, e.g. after a failed validation).
* `409 conflict` "no approved test command for this audit …" / "no approved workload command …":
  the user has not approved a matching discovered command (section 4). PerfPilot never runs
  anything else.
* `409 original_tree_changed`: the snapshot changed (the candidate becomes `FAILED`).
* `503 unavailable`: the worker last saw no sandbox.

**`RepoCandidate`**: `id, audit_id, snapshot_id, finding_id, rule_id, finding_title,
evidence ("MEASURED"|"STATIC"), measured_evidence (the finding's MeasuredEvidence or null), outcome
("proposed"|"no_safe_patch"|"rejected_by_validator"|"rejected"), state, state_history [{from_state,
to_state, at, reason}], generated_by ("deterministic" | "llm:<model>" | proposer name), provenance,
transform, target_file, target_function, line_start, line_end, original_file_sha256,
original_tree_sha256, patched_file_sha256, patched_tree_sha256, summary, notes [str],
proposer_attempts [str], llm_usage (null or {model, prompt_version, calls, input_tokens,
output_tokens, redactions, rejected_answers}), patch_validation {valid, violations[{code, message,
path}], files, changed_lines, checks}, approval {status, approved_at, approved_by, rejected_at,
rejected_by}, rejection_reasons [{by, reason, code?, path?, at?}], workspace_path,
validation_status ("NOT_RUN"|"PASSED"|"FAILED"|"INCONCLUSIVE"), validation (RepoValidation|null),
benchmark (RepoBenchmark|null), error, created_at, updated_at`.

**`RepoValidation`**: `id, candidate_id, audit_id, approval_id, job_id, status, provenance
("MEASURED"), argv, workdir_rel, image, limits, checks [{name, status, required, detail}], summary,
variants {original|patched: {failure, exit_code, command_exit_code, timed_out, oom_killed,
duration_s, counts {passed, failed, error, skipped, total}, junit_error, junit_artifact_id}},
test_comparison {original_passed, patched_passed, regression_count, regressions [{test,
patched}], newly_passing}, patched_function_executed, environment {backend, image_digest,
python_version, platform, network, limits, dependencies, request_sha256, identical_requests, …},
original_tree_sha256, patched_tree_sha256, logs_artifact_id, duration_seconds, error, created_at,
started_at, finished_at`.

**`RepoBenchmark`**: `id, candidate_id, audit_id, approval_id, job_id, status
("QUEUED"|"RUNNING"|"SUCCEEDED"|"FAILED"), provenance ("MEASURED"), argv, workdir_rel, image,
limits, warmups, runs, threshold_percent, seed, timed (what is timed, in words), inputs {baseline,
candidate: {argv, workdir, image, limits, deps_tree_sha256, workspace_tree_sha256},
differing_files, patched_file}, summary {pairs, baseline|candidate: {wall_ms: {n, median, mean,
min, max, stdev, p95, cv_percent, p95_warning}, cpu_ms, peak_rss_bytes {median, max},
container_duration_s_median…}, ci {low, high, confidence, resamples, seed, method}, classifier
{threshold_percent, improvement_speedup_threshold, regression_speedup_threshold},
request_sha256}, speedup, speedup_ci [low, high], latency_reduction_percent, classification
("IMPROVED"|"REGRESSED"|"NO_CLEAR_DIFFERENCE"|"INCONCLUSIVE"|"INVALID"), classification_reason,
environment, warnings, error, samples [{variant ("baseline"|"candidate"), warmup, pair_index,
sequence, wall_ns, cpu_ns, peak_rss_bytes, command_exit_code, container_exit_code,
container_duration_s, ok, failure}], created_at, started_at, finished_at`.

## 4. Approval scope (consumed by design)

Candidate approval says "this diff may be tested". It does not authorise running anything new.
Validation and benchmarking reuse a command the user already approved for **this audit and
snapshot** on the Runtime Audit page (`POST /api/audits/{id}/runtime/approvals`):

* validation: a discovered `pytest` command;
* benchmark: a discovered `script`, `module` or `pytest_benchmark` command (e.g.
  `python benchmarks/run_workload.py`).

Runtime approvals are single use for profiling and are normally already `CONSUMED` by the profile
run; that is by design: the approval pinned the exact argv, working directory, image and limits,
and those are reused verbatim. `REVOKED` approvals never qualify; an unconsumed `APPROVED` one
qualifies until it expires. The latest qualifying approval is used unless `approval_id` names
one. The run records `approval_id`, `argv`, `image` and `limits`.

## 5. Validation in the sandbox (`repo_optimization.validate`)

The worker re-checks the sandbox, verifies the snapshot and the patched copy, then runs the
approved pytest command twice through the trusted harness (`command` mode, cProfile on, no
tracemalloc), each in a fresh container with identical image, limits, environment
(`PYTHONHASHSEED=0`), `network none`, harness request and prepared `/deps` (if any):

1. on the original snapshot (`/workspace` read-only), 2. on the patched copy.

The only argument PerfPilot adds is `--junitxml=/out/junit.xml` (a report; it does not change which
tests run). The JUnit file is untrusted: size-capped (16 MiB), parsed with a bare expat parser that
refuses any DTD/entity declaration and external entities, test cases capped (50,000); test id =
`classname::name`.

| Status | When |
|---|---|
| `FAILED` | a test that passed on the original fails, errors, is skipped or is missing on the patched copy; the patched suite timed out, was OOM-killed or produced no report while the original completed; the patched command failed while the original passed; the copy differs in more than the target file |
| `INCONCLUSIVE` | the original suite did not complete (timeout, OOM, no report) or does not pass; no test passed on the original; the patched function was never executed by the tests (from the patched run's `profile_stats.json`: the function name at its `co_firstlineno` in the target file had no calls) |
| `PASSED` | otherwise |

Checks recorded: `isolation`, `compile`, `original_suite`, `patched_suite`,
`patched_code_exercised`. Artifacts: both JUnit reports (marked untrusted) and a redacted log with
the stdout/stderr tails of both runs. A sandbox that became unavailable makes the run
`INCONCLUSIVE` and fails the job.

## 6. Paired benchmark in the sandbox (`repo_optimization.benchmark`)

Only for `VALIDATED` candidates. Before anything runs, the inputs are fingerprinted: the same argv,
working directory, image, limits and dependency tree hash for both variants, and the two workspace
trees must differ in exactly the patched file (else `FAILED`, classification `INVALID`, "refusing
to compare"). Then `warmups` pairs (unmeasured, stored as `warmup: true`) and `runs` measured pairs
execute, **each run in a fresh container**; the order inside every pair is a coin flip from
`random.Random(seed)`. The harness request is identical for every run (its SHA-256 is recorded and
checked).

**What is timed.** The harness runs the approved command once **without any profiler**
(`command.profile = false`) and reads `time.perf_counter_ns()` (wall) and `time.process_time_ns()`
(CPU) immediately before and after the command, inside the container. Container creation and
teardown, interpreter start-up and the harness's own imports and target resolution are **excluded**
(the container's total duration is stored separately as `container_duration_s` for reference). Peak
memory is `getrusage(RUSAGE_SELF).ru_maxrss` of the process (interpreter included).

Statistics (`backend/benchmarking/statistics.py`): per variant `describe()` (median, mean, min,
max, stdev, p95 with a small-sample warning, CV), `speedup = median_baseline / median_candidate`,
`latency_reduction = (median_baseline − median_candidate) / median_baseline × 100`, and a 95%
paired percentile bootstrap CI of the ratio of medians (pairs by `pair_index`, 10,000 resamples,
`seed`). Classification (`backend/benchmarking/regression_detector.py`) with the requested practical
threshold (default 5%) and at least 5 pairs. Every sample is stored (`repo_benchmark_samples`).
Any failed run stops the benchmark and makes it `INVALID` (see section 1 for the candidate's
state). Defaults are modest (1 warm-up pair, 10 measured pairs) and configurable per request.

## 7. LLM patch proposer

`backend/optimization/llm_proposer.py`, `LLMPatchProposer` (implements `PatchProposer`), prompt
`backend/ai/prompts/patch_proposal.md` (versioned, `patch_proposal@1`).

* **Consent.** Repository candidates: the audit's `llm_consent` (set when the audit is created).
  Workload candidates: `POST /api/optimizations {"recommendation_id", "use_llm": true}`
  (`501 not_configured` without a model). In both cases the LLM is only asked after the
  deterministic proposer found no safe patch (`ChainedPatchProposer`).
* **What is sent.** Only the target function's source (dedented, with line numbers), the finding
  (rule id, title, description, suggestion, risks) and its measured evidence. Nothing else from the
  file or the repository. Messages are redacted by the client before sending; a function containing
  secret-like text is never sent. The call must fit the token budget (`llm_max_tokens_per_call`;
  at most `llm_max_tokens_per_audit`) or nothing is sent.
* **What is accepted.** A JSON object validated by Pydantic (`FunctionRewrite`: `replacement`,
  `summary`, `correctness_notes`). The replacement must parse as exactly one function with the same
  name, kind, parameters, defaults, annotations and return annotation, and no decorators. It is
  re-indented and spliced over the function's exact line span in the real file, compiled, turned
  into a unified diff, and passed through the same patch validator (which also blocks new imports
  of `time`/`os`/…, globals, caches, `exec`/`eval`/`open`). Any problem is fed back once
  (`MAX_ATTEMPTS = 2`); after that, or when the model declines (empty replacement), or on endpoint
  failures, the result is `no_safe_patch` with the reason.
* **Recording.** `generated_by = "llm:<model>"`, provenance `AI_HYPOTHESIS` until validated and
  benchmarked, `llm_usage` with tokens, calls, redactions and rejected answers. Tests use
  `httpx.MockTransport` only.

## 8. Report

The `repo-optimization` provider fills:

* **Section 10**: one entry per candidate with a diff (listed before advisor diffs): title with the
  candidate id, finding, state, proposer and validation status; provenance as in section 1; the
  real unified diff (withheld for sensitive paths, truncated if long). Candidates without a patch
  become limitations with the reason.
* **Section 11**: every finished validation (status, exact command, each check, environment).
* **Section 12**: every successful benchmark (medians, speedup, CI, classification, samples per
  variant, peak RSS, environment, what is timed); raw measured samples also go into the
  `benchmark_samples` CSV. Failed benchmarks become limitations without numbers.

Audits without candidates are unaffected (static audits keep "Not measured").

## 9. Results on ShopFast (real Docker sandbox)

`tests/runtime/test_repo_optimization_docker_e2e.py` (`@pytest.mark.docker`; run with
`make test-docker`) imports `tests/fixtures/repos/shopfast` as a local directory, runs a RUNTIME
audit, approves and profiles `python -m pytest -q` and `python benchmarks/run_workload.py`, creates
the candidate for the measured `PP-LOOP-002` finding in `reconcile` (deterministic
`nested_loop_join_to_index`), approves it, validates and benchmarks it in the sandbox, and renders
the report. A recorded run on an Apple-silicon Mac (Docker Desktop, 2 CPUs, 2 GiB, `network none`,
1 warm-up pair + 10 measured pairs, seed 2026):

| | |
|---|---|
| Validation | `PASSED` — 24/24 tests pass on both trees; `reconcile` executed by the tests |
| Median wall time (command only) | baseline 1364.1 ms, candidate 602.5 ms |
| Speedup | 2.26× (95% CI 2.24×–2.31×), latency −55.8% |
| Classification | `IMPROVED` |

These numbers are machine-specific; the test asserts only that the classification is valid and
that the samples are real. The same test module injects a deliberately wrong candidate (a
test-only proposer double that reverses `reconcile`'s result order): validation is `FAILED`
(`tests.test_inventory_sync::test_reconcile_variants_agree` regresses) and benchmarking is refused
with `409`.

## 10. Limitations

* One finding, one function, one file per candidate. Findings outside a function (module level)
  cannot be patched by the deterministic engine or the LLM proposer.
* Validation is only as good as the repository's tests: `PASSED` means "no test that passed
  before fails now and the tests executed the patched function", not a proof of equivalence.
* The benchmark measures the approved workload command end to end (minus start-up). Effects on
  other code paths are not measured. Docker Desktop's VM adds noise; the bootstrap CI and the
  classifier account for it, but results are specific to the machine.
* Code that runs in the sandbox can tamper with the harness in its own process; results are
  validated as data but not attested (see [runtime-audit.md](runtime-audit.md)).
* Hard links make the copy cheap; on filesystems without hard links files are copied.
