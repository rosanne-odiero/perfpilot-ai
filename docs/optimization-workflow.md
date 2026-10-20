# Optimization workflow (deep dive)

> Phase 6. Code: `backend/optimization/`, `backend/services/{analyses,optimizations}.py`,
> routes `analyses.py` and `optimizations.py`, CLI `scripts/optimize_workload.py`.

```
profile -> analyze -> recommend -> patch -> (explicit) approve -> validate -> benchmark
```

Nothing that sounds like an optimization counts until it has been validated and benchmarked. The
workflow never edits the registered workload: patches are applied to copies, and the original
tree's hash is re-checked after every operation.

## 1. Lifecycle

`backend/optimization/state_machine.py` holds the transition table and its guards. The same
machine drives two records:

| Record | Path |
|---|---|
| `AnalysisRun` | `CREATED -> PROFILING -> PROFILED -> ANALYZING -> RECOMMENDED` (`CREATED -> PROFILED` when an existing profile is reused) |
| `OptimizationCandidate` | `RECOMMENDED -> PATCH_PROPOSED -> AWAITING_APPROVAL -> VALIDATING -> VALIDATED -> BENCHMARKING -> COMPLETED` |

`COMPLETED`, `REJECTED` and `FAILED` are terminal. Every non-terminal state may go to `FAILED`.
Two retry edges exist: `VALIDATING -> AWAITING_APPROVAL` (validation was `INCONCLUSIVE`, for
example a timeout) and `BENCHMARKING -> VALIDATED` (the benchmark process crashed).

| Guard | Rule |
|---|---|
| `PROFILING/CREATED -> PROFILED` | the profile is `SUCCEEDED` and has hotspots (a failed profile cannot produce hotspots) |
| `RECOMMENDED -> PATCH_PROPOSED` | a diff exists |
| `PATCH_PROPOSED -> AWAITING_APPROVAL` | the diff passed the patch validator |
| approve (in `AWAITING_APPROVAL`) | a valid diff exists and the candidate is still `PENDING` |
| `AWAITING_APPROVAL -> VALIDATING` | approved, valid diff, original tree unchanged |
| `VALIDATING -> VALIDATED` | validation `PASSED` with every required check passed |
| `VALIDATED -> BENCHMARKING` | the same, plus approved and original tree unchanged |
| `BENCHMARKING -> COMPLETED` | approved, validated, the benchmark `SUCCEEDED`, and its input equals the validated baseline input |
| `* -> REJECTED` | only through an explicit rejection (by a user or by the patch validator) |

Every transition is appended to `state_history` with a timestamp and a reason. Approval itself is
recorded as an event in the history (same state) plus `approved_by` / `approved_at`.

## 2. Analysis and recommendations

`POST /api/analyses {workload_id, profile_id?, size?}` starts the `optimization.analyze` job.

* With `profile_id`, the profile must belong to the workload and be `SUCCEEDED` (otherwise
  `422`/`409`). Without it, the job profiles the workload itself (`PROFILING` state).
* The static rules (`backend/audit/rules`) run over the workload's own source files. Tests,
  fixtures, references and harness files are excluded, using the same classifier as the patch
  validator. Static analysis never imports or executes code.
* Each finding becomes an `OptimizationRecommendation` (base-spec schema). The ranking uses the
  measured cumulative share of the flagged function.

| Field | Provenance | Content |
|---|---|---|
| `profiler_evidence` | `MEASURED` | profile id, size, input hash, and the function's exclusive/cumulative ms, percentages and calls, copied from the referenced `ProfileRun`. Also the largest non-workspace functions in the same profile, explicitly not attributed to a caller |
| `observed_bottleneck` | `MEASURED` + `STATIC` | the measured share of the function plus the rule that flags it |
| `suspected_root_cause` | `STATIC` | rule template |
| `expected_effect`, `estimated_impact` | `AI_HYPOTHESIS` | qualitative only (`high`/`moderate`/`low`/`uncertain`/`unknown` plus a rationale). Never a number |
| `correctness_risks`, `memory_tradeoffs` | `STATIC` | rule template |
| `validation_plan` | | the checks that validation will run, derived from the workload contract |
| `patch_strategy` | | dry run of the deterministic proposer: is a patch available, and if not, why |

`GET /api/analyses/{id}` and `GET /api/analyses/{id}/recommendations` return the results.

## 3. Patch generation

`backend/optimization/patch_generator.py` is generic over *(source root, target file, target
function and line span, rule id)*, so the runtime repository audit reuses it for hotspots in
imported repositories ([repo-optimization.md](repo-optimization.md)). Steps:

1. Resolve the target inside the source root (no symlinks, no `..`) and **verify its SHA-256**
   against the hash recorded by the analysis snapshot (`409 source_changed` otherwise).
2. Run the AST-guided transformation registered for the rule (`transforms.py`). It edits text at
   AST positions, so formatting and comments outside the edited spans are preserved.
3. Re-parse and `compile()` the result, check that public signatures are unchanged, and emit a
   `git`-style unified diff.

| Rule | Transformation | Applied only when |
|---|---|---|
| `PP-LOOP-001` | `x in L` in a loop -> `L_set = set(L)` built once before the outermost loop where `L` is invariant | `L` is a parameter annotated `list`/`tuple` or a local always assigned a list/tuple, and every use of `L` in the loop is a membership test |
| `PP-COLL-001` | `L.count(x)` in a loop -> `L_counts = Counter(L)` once, `L_counts[x]` | same evidence for `L`; every use in the loop is `.count(x)`; `Counter` is importable without shadowing |
| `PP-STR-001` | `s += piece` in a loop -> `s_parts.append(piece)` and `s = "".join(s_parts)` after the loop | `s` starts as a string literal before the loop, is only ever appended to inside it, and the loop is not inside `try`/`with` (the partial string would differ after a handled exception) |
| `PP-REGEX-001` | `re.f(r"literal", ...)` in a loop -> module-level `re.compile` and `PATTERN.f(...)` | literal pattern, constant flags (moved into `compile`), `re` imported at module level |
| `PP-LOOP-002` | nested-loop equality join -> dict index of the inner list (original order kept) | inner body is a single `if a == b` without `else`, keys are plain names/attributes/constant subscripts, the inner list is invariant and the loop variable is not used elsewhere |

Anything else returns `status: "no_safe_patch"` with the reason (for example `cannot prove
parameter known is a list or tuple`), and the candidate goes to `FAILED`. Nothing is guessed.
Tab indentation and CRLF files are refused. The bundled workloads all get patches. They are
generated from the original `workload.py`, and they differ from the hand-written
`reference/optimized.py` files.

**Proposers.** `PatchProposer` is a protocol (`name`, `propose(PatchRequest) -> PatchProposal`).
`DeterministicPatchProposer` is the default; another proposer can be registered with
`register_patch_proposer(factory)`. The LLM proposer (`backend/optimization/llm_proposer.py`) is
chained after it only with consent: `POST /api/optimizations {"recommendation_id", "use_llm":
true}` (`501 not_configured` when no model is configured). It is asked only when no safe
deterministic patch exists, sees only the target function, the recommendation and its
`MEASURED` profiler evidence, and its rewrite must pass the compile check and the validator below
(details in [repo-optimization.md](repo-optimization.md#7-llm-patch-proposer)). The candidate
records the model identifier in `generated_by` and earlier refusals in `notes`. Whatever produced
the diff, it passes through the same validator.

## 4. Patch validator

`backend/optimization/patch_validator.py` decides whether a diff may reach approval:

* **Strict parsing.** `---`/`+++`/`@@` only, exact hunk line counts, and no NUL or CR bytes.
  Binary patches, mode changes (`old mode`, `index ... 100755`), renames, copies, new files and
  deleted files are rejected (new and deleted files can be enabled explicitly).
* **Paths.** Relative POSIX paths with no `..`, `.`, empty segments, backslashes or `~`. The path
  must resolve inside the workspace, contain no symlinks, and point to an existing regular UTF-8
  file of at most 512 KiB.
* **Protected files.** These can never be patched, even if they are in the allow-list: secrets
  and env files, CI configs (`.github/`, ...), security configs, VCS metadata, build and
  dependency files, benchmark harness files (`workload.json`, `conftest.py`, `benchmarks/`,
  `sitecustomize.py`, ...), fixtures, reference oracles, tests, plus the workload's own fixture
  module and validation test paths.
* **Allow-list.** Only the recommendation's target file may change.
* **Exact application.** No fuzz and no offsets.
* **Python checks.** The patched file must compile, keep every public signature, and introduce no
  benchmark-gaming constructs: caching decorators, `global`/`nonlocal`, module-level mutable
  containers (results carried across calls), imports of `time`/`gc`/`sys`/`os`/... (timer or
  interpreter tampering), `exec`/`eval`/`open`/`setattr`, or assignments to module attributes.
  Only constructs *introduced* by the patch are flagged.

A diff that fails validation leaves the candidate `REJECTED` (`rejected_by: patch-validator`) with
one rejection reason per violation.

## 5. Approval and isolated workspaces

* `POST /api/optimizations {recommendation_id}` generates and validates the patch and returns the
  candidate (`201`). The candidate is in `AWAITING_APPROVAL`, or in `FAILED`/`REJECTED` with
  reasons.
* `GET /api/optimizations/{id}/diff` returns the unified diff plus the full original and patched
  text for side-by-side display.
* `POST /api/optimizations/{id}/approve {approved_by}` is explicit and records who approved and
  when. `POST /api/optimizations/{id}/reject {reason, rejected_by}` is final.
* `POST /api/optimizations/{id}/validate` returns `202` and a job, and only works for an approved
  candidate (`409` otherwise).

Validation copies the workload to
`<data_dir>/workspaces/optimizations/<candidate_id>/{baseline,patched}/<slug>`, re-validates the
diff against the copy, writes the patched file, and checks that its hash equals the proposed hash.
The original tree's hash is verified before copying, after patching, after validation, and before
benchmarking. A mismatch fails the candidate (`original_tree_changed`).

## 6. Validation engine

`backend/optimization/validation.py`. Every execution goes through
`backend/profiling/runner.run_harness` (fresh child process, fixed interpreter, scrubbed
environment, rlimits, wall-clock timeout, RSS ceiling).

| Check | Required | What it does |
|---|---|---|
| `isolation` | yes | only the target file differs between the two copies |
| `compile` | yes | the patched file compiles |
| `functional_tests` | yes | the workload's tests (`validation.tests`) run with pytest in a child process against **both** copies, with an isolated pytest config. Any failure is `FAILED`. Fewer than `min_tests` passes, or an original that fails its own tests, is `INCONCLUSIVE` |
| `output_equivalence` | yes | the original and the candidate on fixture inputs for `validation.sizes` x `validation.seeds`, compared with the output contract (`exact` hash, `unordered` multiset, or `float_tolerance` with explicit `rel_tol`/`abs_tol`). Inputs must hash identically |
| `edge_cases` | if configured | the same comparison for literal edge inputs from the contract |
| `exception_parity` | if configured | each error input must raise in the original (optionally the stated type) and the same exception type in the candidate |
| `input_mutation` | unless `allow_input_mutation` | the candidate must not mutate an input that the original leaves untouched |
| `patched_code_exercised` | yes | the tests called the patched function (from the test run's cProfile data), or the patched function is the entrypoint that every case calls. Otherwise `INCONCLUSIVE` (insufficient coverage) |

The overall status is `FAILED` if any required check failed, `INCONCLUSIVE` if any required check
could not decide (timeout, crash, too few tests, outputs too large to compare), `PASSED` only when
every required check passed, and `NOT_RUN` when nothing ran. A candidate is never labeled
validated without `PASSED`. The `ValidationRun` stores per-check results, the baseline input
hashes per fixture case, and a redacted log artifact.

The per-workload contract lives in the `validation` section of `workload.json` (see
[workloads.md](workloads.md)).

## 7. Benchmark integration

`backend/optimization/candidate_resolver.install()` registers the candidate resolver. It is
installed by the optimization routes, the job handlers and the CLI.
`POST /api/benchmarks {"workload_id", "candidate": {"candidate_id": "..."}}` then works as follows:

* **On submission.** The candidate must exist (`404`) and belong to the same workload (`409`: a
  different workload's input cannot be compared). It must also pass `VALIDATED -> BENCHMARKING`
  (`409 invalid_state_transition` for anything not validated, including `FAILED` and `REJECTED`
  candidates).
* **In the worker.** The validated patched workspace is copied after re-checking its tree hash
  and the original tree hash.
* **On completion.** The run is linked to the candidate. A `SUCCEEDED` benchmark that is not
  `INVALID`, whose input hash equals the validated baseline input for that size and seed, and
  whose baseline tree equals the candidate's original tree, moves the candidate to `COMPLETED`. A
  non-comparable run moves it to `FAILED`. A crashed run returns it to `VALIDATED` for a retry.

The benchmark methodology itself is unchanged ([methodology.md](methodology.md)).

## 8. CLI

```
.venv/bin/python scripts/optimize_workload.py duplicate_counter            # stops at approval
.venv/bin/python scripts/optimize_workload.py duplicate_counter --approve  # full deep dive
.venv/bin/python scripts/optimize_workload.py nested_loop --approve --profile-size small \
    --size medium --warmups 5 --runs 20
```

Without `--approve`, the candidate stays in `AWAITING_APPROVAL` and the output says so. Nothing
is applied, validated or benchmarked. The printed JSON includes the profile, analysis,
recommendation, diff, validation checks, the benchmark summary (medians, speedup, 95% CI,
classification), the state history, and `original_tree_unchanged`.

## 9. Experiment reports

`POST /api/optimizations/{id}/reports {"formats": ["md", "json", "html", "csv", "pdf"]}` exports
the whole experiment (profile evidence, recommendation, diff and hashes, approval, per-check
validation, benchmark distributions, memory, reproducibility commands) as an engineering report for
an issue or pull request. Failed and rejected candidates are reported too, with the reason and
without benchmark numbers. See [reports.md](reports.md#experiment-reports-workload-deep-dive).

## 10. Limitations

* The deterministic templates cover five rule families. Other findings stay recommendations with
  `no safe deterministic patch`.
* Static safety checks are conservative but not a proof. Aliasing (another reference mutating the
  list inside the loop) and exotic `__hash__`/`__eq__` implementations are documented risks. The
  validation checks cover only the inputs they exercise.
* Bundled workloads are trusted and run in a constrained subprocess on the host. Patches to
  imported repositories are validated and benchmarked in the Docker sandbox instead; see
  [repo-optimization.md](repo-optimization.md).
* Approval identity is a free-text `approved_by`. There is no authentication yet.
