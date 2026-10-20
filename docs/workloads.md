# Workloads

A **workload** is a function plus a deterministic input generator that PerfPilot can profile and
benchmark. The bundled workloads in `examples/` are trusted code and are registered automatically
as the **"Bundled examples"** project when the API starts (`seed_bundled_examples`, which is
idempotent).

## Layout of a workload

```
examples/<slug>/
  workload.py            # entrypoint: deliberately inefficient but correct
  fixtures.py            # make_input(**size_params, seed=...) -> deterministic input
  workload.json          # spec (below)
  README.md              # suspected bottleneck and semantic trade-offs of the obvious fix
  reference/optimized.py # REFERENCE IMPLEMENTATION, hand-written for tests (not platform output)
  tests/test_workload.py # functional + edge cases for the original and the reference
```

The example tests run with the main suite (`testpaths = ["tests", "examples"]`). Each test file
loads modules by path relative to its own workspace, so the same tests can later run against a
patched copy of the workspace.

## `workload.json`

| Field | Meaning |
|---|---|
| `entrypoint` | `module:function`, resolved **inside** the workspace (identifiers only) |
| `fixture` | `module:function` that builds the input |
| `fixture_description` | What the generated input looks like |
| `invocation_mode` | `single` → `fn(data)`, `positional` → `fn(*data)`, `keyword` → `fn(**data)`, `none` → `fn()` |
| `expected_output` | `{type, comparison: exact, unordered or float_tolerance, rel_tol, abs_tol, description}`. `unordered` compares a top-level list/tuple as a multiset |
| `timeout_seconds`, `memory_limit_mb` | Per-process limits |
| `permitted_env_vars` | Variables forwarded to the child (everything else is scrubbed) |
| `sizes`, `default_size`, `seed` | Fixture keyword arguments per size and the fixed seed |
| `reference_candidate` | Workspace-relative path of the bundled reference implementation |
| `validation` | Contract used to validate patch candidates (below). Optional |

### `validation`

| Field | Meaning |
|---|---|
| `tests` | Workspace-relative test paths that pytest runs against the original and the patched copy (default `["tests"]`) |
| `min_tests` | Fewer passing tests makes the check `INCONCLUSIVE` |
| `sizes`, `seeds` | Fixture inputs compared between original and candidate (default: first + default size, `seed..seed+2`) |
| `edge_cases` | `[{name, input}]`: literal inputs shaped for the invocation mode (JSON, so tuples arrive as lists) |
| `error_cases` | `[{name, input, expected_exception}]`: inputs on which the original must raise; the candidate must raise the same type |
| `allow_input_mutation` | If `false` (default), a candidate that starts mutating its input fails validation |

See [optimization-workflow.md](optimization-workflow.md) for how these are used.

The registered form is `backend.models.workloads.WorkloadSpec`. It adds `id`, `project_id`,
`working_dir_id` (source snapshot id), and `input_dataset_id` (null for fixture-generated inputs).

## Bundled workloads

| Slug | Suspected bottleneck | Reference change | Main trade-off |
|---|---|---|---|
| `list_lookup` | `x in list` inside a loop, O(n·m) | `set` built once | Needs hashable ids, extra memory |
| `duplicate_counter` | `list.count` per element, O(n²) | `collections.Counter` | Needs hashable items |
| `string_builder` | `s += line` in a loop | `"".join([...])` | CPython often resizes `s` in place, so the gain may be negligible or negative. The benchmark decides |
| `regex_matching` | `re.findall(pattern_str, line)` per line | Pattern compiled once at module level, still called via `re.findall` | `re` already caches patterns by string, so no compilation is saved; a compiled `Pattern` takes the cache-miss path each call |
| `nested_loop` | Join by nested loop, O(c·o) | Dict index of totals | Float sums compared with tolerance |

`string_builder` and `regex_matching` exist to test plausible optimizations whose benefit is
doubtful (small, absent, or negative, and possibly paid for with memory). The verdict always comes
from measurement, never from the label.

## Running workloads

API (all long-running calls return `202` with a job id; poll `GET /api/jobs/{id}`):

```
GET  /api/projects
GET  /api/projects/{project_id}
GET  /api/projects/{project_id}/workloads
GET  /api/workloads/{workload_id}            # spec, entrypoint + fixture source, README
POST /api/profiles   {"workload_id", "size", "enable_sampling"}
GET  /api/profiles/{id}
GET  /api/profiles/{id}/hotspots?sort=exclusive|cumulative|calls&scope=all|user&limit=
POST /api/benchmarks {"workload_id", "candidate": "reference" | {"candidate_id": "…"},
                      "size", "warmups", "runs", "memory_runs", "threshold_percent", "seed",
                      "order": "random"|"alternate", "bootstrap_resamples"}
GET  /api/benchmarks/{id}
GET  /api/benchmarks/{id}/results?kind=preflight|timing|memory
```

`{"candidate_id": …}` benchmarks a **VALIDATED** patch candidate against the original
(see [optimization-workflow.md](optimization-workflow.md)). Unknown candidates return `404`.
Candidates that are not validated, or that belong to another workload, return `409`. If no
resolver is registered (`backend.benchmarking.candidates.register_candidate_resolver`), the
request returns `501 not_configured`.

The deep-dive endpoints (`/api/analyses`, `/api/optimizations`) and
`scripts/optimize_workload.py` are documented in
[optimization-workflow.md](optimization-workflow.md).

CLI (same services, same JSON shapes, persisted to the same database):

```
.venv/bin/python scripts/run_workload.py list
.venv/bin/python scripts/run_workload.py profile duplicate_counter --size medium --top 10
.venv/bin/python scripts/run_workload.py benchmark string_builder --warmups 5 --runs 20
.venv/bin/python scripts/run_workload.py benchmark-all --summary
```

See [methodology.md](methodology.md) for what each number means and its limitations.
