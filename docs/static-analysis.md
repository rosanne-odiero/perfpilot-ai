# Static analysis (Static Audit)

The Static Audit indexes a repository snapshot and runs deterministic rules over it. It **only parses
code with `ast`**: repository code is never imported or executed, and `setup.py` /
`pyproject.toml` are read as data. Every finding has provenance `STATIC`: it is a hypothesis with
`file:line` evidence, never a measurement.

## Pipeline

`backend/audit/pipeline.py` runs an ordered tuple of stages over a shared `AuditState`:

| Stage | Function | Work |
|---|---|---|
| `INDEXING` | `index_stage` | select files, parse, infer import roots, name modules, extract symbols and entry points |
| `GRAPHING` | `graph_stage` | resolve imports, build the import graph (networkx) and the call graph |
| `STATIC_RULES` | `rules_stage` | run all rules in one scope-aware traversal per module |
| `PRIORITIZING` | `prioritize_stage` | score and sort findings, set per-module `finding_count` |

`run_static_audit(root, inventory, settings, progress=None)` (`backend/audit/static_audit.py`) runs
`STATIC_STAGES` and returns a `StaticAuditResult` (code index, sorted findings, stats, warnings,
limits). It has no database access. Later stages (AI analysis, runtime discovery and profiling,
correlation) are added by extending the stage tuple per mode in `backend/jobs/handlers/audit.py`
(`AUDIT_STAGES`). Nothing else has to change.

### Job and API

`POST /api/audits {snapshot_id, mode, use_llm, llm_consent}` creates an `AuditRun` and enqueues
`audit.run` (HTTP 202 with `JobAccepted`, `subject_type="audit"`). These requests are rejected:
`mode=RUNTIME` returns 501 `not_configured` until the sandbox exists, `use_llm` without
`llm_consent` returns 422, an unknown snapshot returns 404, and a snapshot that is not `READY`
returns 409. This build has no AI stage, so `use_llm=true` with consent adds a warning and no code
leaves the machine.

The job loads the snapshot and checks that its `workspace_path` resolves inside
`settings.workspaces_dir`. It persists `AuditRun.stage` at each stage boundary and then stores:

* the `CodeIndex` JSON in `AuditRun.code_index`;
* the stats in `summary`;
* any warnings;
* one `FindingRecord` per finding, with promoted filter columns and the full `Finding` JSON in
  `data`.

The row ID is `uuid5(audit_id, finding.id)`, so re-running a job replaces its rows. The final
status is `SUCCEEDED`, `PARTIAL` (file limits hit, parse errors, or rules skipped), or `FAILED`.

Read endpoints:

| Endpoint | Returns |
|---|---|
| `GET /api/audits?snapshot_id=` | Recent audits |
| `GET /api/audits/{id}` | Status, stage, summary, warnings, job progress |
| `GET /api/audits/{id}/findings` | Findings filtered by `severity` (multi), `category` (multi), `rule_id`, `file_path` prefix, `provenance`, `min_priority`, with `limit`/`offset`; sorted by priority |
| `GET /api/audits/{id}/findings/{finding_id}` | One finding |
| `GET /api/audits/{id}/graph` | The `DependencyGraph` |
| `GET /api/audits/{id}/modules?path=` | Modules without symbols/imports, plus counts and graph metrics |
| `GET /api/audits/{id}/files/findings-count` | Per-file totals and severity breakdowns |

## Indexing (`backend/indexing/`)

**File selection** (`code_index.select_files`): the selection starts from the inventory's Python
files. These paths are rejected:

* absolute paths, paths containing `..`, backslashes, or NUL bytes;
* non-`.py` files;
* sensitive names, matched by `backend.core.redaction.SENSITIVE_FILE_NAMES`;
* symlinks;
* anything that resolves outside the root;
* files over `max_file_bytes`.

If more than `max_python_files` remain, source files are kept before test files and the limit is
reported.

**Parsing** (`source.py`): files are decoded per PEP 263 (BOM-aware), with lossy UTF-8 as a fallback
and a warning. Newlines are normalized so that AST line numbers index the stored lines exactly.
Syntax errors, NUL bytes, and excessive nesting are recorded per file and do not stop the audit.

**Import roots** (`module_resolution.infer_import_roots`): roots are chosen in this order:

1. The repository root is always a root.
2. The parent of every top-level regular package is a root. This covers `src/` layouts and nested
   projects such as `backend/app/...` with `from app.x import y`.
3. Other directories are added greedily while they make more absolute imports resolvable. Candidates
   are script directories and the ancestors of Python files, excluding package directories.

The chosen roots are stored in `CodeIndex.import_roots`. Each file gets its canonical name from the
deepest root that gives a unique, importable name. When several files share a name, they fall back
to shallower names.

**Import resolution**: absolute imports resolve against every name from every root. When several
files match, the one closest to the importing file wins. Relative imports resolve purely by path.
`from pkg import sub` links to the submodule when it exists. Anything unresolved is external, and
`external_imports` counts it by top-level package. Imports inside `if TYPE_CHECKING:` are skipped
because they do not run.

**Graph** (`import_graph.py`): the graph records internal edges (first import line), fan-in,
fan-out, and `in_cycle`. `in_cycle` is exact and comes from strongly connected components. The
listed `cycles` are capped at 50 simple cycles of length 8 or less.

**Symbols** (`ast_index.py`): functions, async functions, methods, and classes, each with:

* a line span that includes decorators;
* decorators and a docstring;
* `max_loop_depth` (comprehensions count once per generator);
* `is_recursive` (direct self-calls);
* best-effort dotted callee names.

**Call graph** (`call_graph.py`): nodes are `module:qualname`. Calls are linked by name only:

* local definitions, including enclosing functions;
* `self`/`cls`/`ClassName` methods;
* names imported from internal modules;
* `module.func` through an imported internal module.

Dynamic dispatch is not followed.

**Entry points** (`entrypoints.py`): each module or symbol is marked with the reasons that apply:

* an `if __name__ == "__main__"` guard (functions it calls are entry symbols);
* `main.py`, `__main__.py`, `manage.py`, `wsgi.py`, or `asgi.py`;
* FastAPI, Starlette, or Flask route decorators (`@app.get`, `@router.post`, `@bp.route`, …);
* `add_api_route` / `add_url_rule` registrations;
* app objects and app factories (`FastAPI()`, `Flask()`, `Celery()`, …);
* Celery `@app.task` and `@shared_task`;
* click and typer commands;
* Django `urls.py` with `urlpatterns`, and Django views;
* `argparse` CLIs;
* `[project.scripts]` and `[tool.poetry.scripts]` console scripts.

## Rules (`backend/audit/rules/`)

All rules run in **one traversal per module** (`base.ScopeTracker`). The traversal tracks:

* the enclosing function and whether it is async;
* the class;
* the loop stack: `for`, `while`, and comprehensions, reset at every function or lambda boundary;
* the names assigned or possibly mutated inside each loop;
* flow-insensitive "kinds" of names (`kinds.py`: list, set, dict, str, DB connection or cursor,
  HTTP client, DataFrame, …), taken from literals, constructors, and annotations.

When a name has conflicting bindings, its kind is unknown and kind-based rules stay silent.
Evaluation order is respected: a `for` iterable and a comprehension's first iterable are visited
outside the loop.

Loop-based rules drop code that runs at most once per loop execution. That covers code inside a
`raise` and statements followed by `return`, `raise`, or `break` in the same block.

| ID | Title | Default severity / confidence | Notes (key exclusions) |
|---|---|---|---|
| PP-LOOP-001 | List membership inside a loop | MEDIUM / HIGH (MEDIUM when only a parameter annotation proves list-ness); LOW for constant list literals of 6 or more items | Skips sets, dicts, strings, ranges, tuples, and short fixed lists |
| PP-LOOP-002 | Nested loops matching by `==` (join) | HIGH / HIGH | Also matches `next(x for x in B if x.k == a.k)` in a loop. Skips inner iterables derived from the outer item, `range` loops, short literals, and `if a == b: continue` |
| PP-LOOP-003 | Loop-invariant computation | MEDIUM / MEDIUM (HIGH severity for file reads) | Copies, sorts, and reductions of one collection; comprehensions that are assigned or consumed once; parsers (`json.loads`); file listings and reads. Requires every input to be unchanged in the loop. Skips fresh per-iteration copies that the loop mutates, and file reads in `while` (polling) loops |
| PP-LOOP-004 | Re-sorting a growing collection | MEDIUM / HIGH | Only when the same list grows inside the loop and is not reset each iteration |
| PP-STR-001 | String built with `+=` or `s = s + …` in a loop | MEDIUM / HIGH (MEDIUM if only the appended piece is known to be a str) | Notes CPython's in-place resize. Skips per-iteration resets and loop targets |
| PP-REGEX-001 | Constant regex inside a loop | LOW / HIGH for `re.compile`, LOW / LOW for `re.search` and the like (patterns are cached) | Patterns built from variables are ignored |
| PP-COLL-001 | `list.index`/`count`/`remove` in a loop | MEDIUM / HIGH | The receiver must be a known list |
| PP-COLL-002 | `pop(0)` / `insert(0, x)` on a list in a loop | MEDIUM / HIGH | The receiver must be a known list; a deque never matches |
| PP-COLL-003 | `len(list(...))`, `list(d.keys())` in a loop | LOW / HIGH | Skips the defensive-copy idiom `for k in list(d.keys())` |
| PP-DB-001 | Query inside a loop (N+1) | HIGH / MEDIUM (HIGH for DB-API cursors and Django eager calls) | Matches SQLAlchemy `session`/`db` `.query/.execute/.scalars/.get(Model, …)`, Django `.objects.*`, DB-API `.execute`, and awaited queries. Skips `while` (pagination) loops, loops over literal lists, and HTTP sessions |
| PP-ASYNC-001 | Blocking call in `async def` | HIGH / HIGH (file I/O LOW / MEDIUM) | Matches `time.sleep`, `requests`, `urlopen`, module-level `httpx`, `subprocess`, sync DB drivers, sync clients, and `future.result()`. A sync ORM session (`db.query`, never awaited) gets one finding per coroutine. Calls inside nested sync defs or lambdas, and calls passed to `to_thread`/`run_in_executor`, are not flagged |
| PP-NET-001 | Sequential HTTP request in a loop | MEDIUM / HIGH | `requests`/`httpx`/`urllib` functions and known client objects. Skips retry loops (`for attempt in …`, `range(<=10)`) |
| PP-NET-002 | HTTP client created per call or iteration | MEDIUM / HIGH in loops; LOW / MEDIUM per call | Skips module level, factory or lifecycle functions, and clients that are returned or stored |
| PP-ALLOC-001 | `copy.deepcopy` of the same outer object in a loop | MEDIUM / MEDIUM | Per-item copies of the loop variable are inherent work and are skipped |
| PP-ALLOC-002 | Large constant literal rebuilt in a loop | LOW / MEDIUM | Needs 8 or more constant elements. Skips compiler-folded `in`/`for` literals and fresh containers that are mutated |
| PP-ALLOC-003 | DataFrame grown by `concat`/`append` in a loop | HIGH / HIGH | Only for modules that import pandas |
| PP-ALLOC-004 | `df.iterrows()` | MEDIUM / MEDIUM | Only for modules that import pandas |
| PP-CACHE-001 | Overlapping recursion without memoization | MEDIUM / MEDIUM | Needs 2 or more self-calls with arithmetic arguments (`n-1`, `n-2`), a pure body, and no cache decorator |
| PP-CACHE-002 | Expensive call repeated with identical constant arguments | LOW / MEDIUM | `json.loads`, `pandas.read_*`, file listings, `re.compile`, constant-URL requests, … |
| PP-ARCH-001 | Heavy work at import time | HIGH for network, MEDIUM for subprocesses and data or model loads, LOW for plain file reads and big `range` loops | Skips `__main__` and `TYPE_CHECKING` blocks, tests, and `setup.py`/`conftest.py` |
| PP-ARCH-002 | Unbounded module-level cache | MEDIUM / MEDIUM | A dict named `*cache*` or `*memo*` that functions write to but never evict from, size-check, or reset |

Descriptions explain the mechanism without claiming that it causes slowness. Complexity notes are
always labeled `estimate:`. Suggestions come with semantic trade-offs: hashability, lost ordering
or duplicates, memory, staleness, and thread safety.

**Finding identity and evidence.** The finding ID is
`sha256(rule_id | path | line | normalized code)[:24]`, so it is stable across runs. `location`
gives the exact span of the offending node. `snippet` holds the exact source lines (up to 2 lines of
leading context, at most 12 lines) and always contains `line_start`. Duplicate IDs are dropped.

**Suppression.** Add `# perfpilot: ignore` or `# perfpilot: ignore[PP-LOOP-001,PP-STR-001]` to the
first or last line of a finding to suppress it. Suppressed findings are counted in the stats.

## Prioritization (`backend/audit/prioritization.py`)

```
priority_score   = 100 * severity.weight * confidence.weight * path_criticality
path_criticality = reachability * loop_factor * fan_in_factor
```

* Weights: severity CRITICAL 1.0, HIGH 0.8, MEDIUM 0.55, LOW 0.3, INFO 0.1; confidence HIGH 1.0,
  MEDIUM 0.7, LOW 0.4.
* `reachability`:
  * **1.0**: the enclosing function (or an enclosing outer function) is an entry point.
  * **0.9**: the call graph reaches it from an entry-point function.
  * **0.75**: its module is reachable from an entry-point module through imports.
  * **0.5**: otherwise.
  * **0.3**: test code, always.
* `loop_factor = min(1.0, 0.6 + 0.2 * loop_depth)`. `loop_depth` is the depth at the finding,
  counting comprehensions.
* `fan_in_factor = 0.7 + 0.3 * fan_in / max_fan_in`. Fan-in is the number of modules that import
  the finding's module.

Every factor is stored in `Finding.priority_factors`. Findings are sorted by score, then path, line,
and rule.

## Limitations

* Kinds are inferred within one module and are flow-insensitive. Values returned by other
  functions are unknown, so rules that need a kind stay silent for them (this lowers recall).
* Loop invariance is best-effort. Aliasing through other functions, threads, or I/O can make an
  invariant-looking value change. Those findings are therefore MEDIUM confidence.
* The call graph does not follow dynamic dispatch, decorators that replace functions, or calls
  through instance attributes.
* Static findings cannot know data sizes. A rule may flag a pattern whose input is always tiny, and
  severity reflects the pattern rather than measured impact. Runtime profiling (Phase 8) is what
  upgrades findings to `MEASURED`.
