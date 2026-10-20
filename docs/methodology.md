# Profiling and benchmarking methodology

> Applies to the workload deep dive (Phases 5 and 7). The code lives in `backend/profiling/` and
> `backend/benchmarking/`. Every number described here is labeled `MEASURED`.

## 1. Execution model

Each profile or benchmark run starts a **new child process**:

```
python -P -s -B backend/profiling/harness_entry.py <request.json> <result.json>
```

* `harness_entry.py` is self-contained and stdlib-only (`psutil` is used if installed). It
  imports nothing from `backend`, so the same file can be copied into a Docker sandbox and run
  against untrusted code later.
* The interpreter is always `sys.executable`. The argv is a fixed list, and no shell is used.
  `-P` and `-s` keep the script directory and user site-packages off `sys.path`, and `-B` stops
  `.pyc` files from being written into the workspace.
* The child gets a scrubbed environment: `PATH=os.defpath`, `HOME` and `TMPDIR` inside the run
  directory, `PYTHONHASHSEED=0` (so set/dict iteration order is reproducible), UTF-8 I/O, and only
  the workload's **permitted** variables. Variables that change interpreter or loader behavior
  (`PYTHON*`, `LD_*`, `DYLD_*`, `PERFPILOT_*`, `PATH`, `HOME`, …) cannot be permitted.
* Limits: `RLIMIT_CPU`, `RLIMIT_FSIZE` and `RLIMIT_NOFILE` are set in the child before `exec`.
  `RLIMIT_AS` is set on Linux. macOS does not enforce it, so this is recorded as unsupported. On
  every OS, the parent also enforces a wall-clock timeout and a sampled-RSS ceiling by killing the
  child's process group.
* stdout/stderr go to files, and at most 64 KiB of each is read back. Error text is passed through
  `backend.core.redaction` before it is stored.

**Containment.** An entrypoint is `module:function` (identifiers only, no dunders). The module is
resolved to a file *inside* the registered workspace, with no `sys.path` search, and symlinks are
resolved before the `is_relative_to` check. The parent (`resolve_target_file`) and the child
(`module_path`, `load_callable`) use the same code, so a target is rejected before any process
starts, and the child checks it again. Command mode (`module`, `script`, `pytest` targets) applies
the same rules.

**Inputs.** The fixture (`fixtures:make_input`) is called with the size's keyword arguments plus
the spec's `seed`. The child hashes the generated input in a canonical form (dict and set order
normalized, list/tuple order and type kept) and reports `input_sha256`.

## 2. Profiling (`mode = "profile"`)

1. Generate the input, then run **one call under cProfile**. Only the workload call is profiled,
   not input generation. The raw `.prof` file is stored in the artifact store.
2. Generate a fresh input, then run **one call under tracemalloc**. Tracing starts after the input
   exists, so only allocations made by the call are counted.
3. The parent samples the child's RSS (process tree) every 10 ms.

### Hotspot metrics (from `pstats`)

| Field | Definition |
|---|---|
| `internal_time_ms` | pstats `tottime`: time spent in the function body itself (**exclusive**) |
| `cumulative_time_ms` | pstats `cumtime`: time in the function and everything it called |
| `primitive_calls` / `total_calls` | pstats `cc` / `nc`. They differ for recursion. `total_calls` includes recursive calls |
| `percentage_of_profiled_time` | `tottime / Σ tottime(all functions) × 100` |
| `cumulative_percentage` | `cumtime / Σ tottime(all functions) × 100` |

* Exclusive percentages add up to 100% across all functions.
* Cumulative percentages **overlap**: a caller's cumulative time includes its callees. They are
  shown in a separate column and are **never summed**.
* `file_path` is relative to the workspace for workload code. Otherwise it is marked
  `<built-in>`, `<stdlib>/…`, `<site-packages>/…`, `<harness>/…`, or `<external>/<file name>`.
  Host directory layout is not exposed.
* "Top user hotspots" keep only workspace frames. Builtins, stdlib, site-packages, and harness
  frames (including cProfile's own `disable`) stay in the raw data (DB rows and the normalized
  JSON artifact).
* Rankings: `exclusive` (`tottime`), `cumulative` (`cumtime`), `calls` (`nc`).

### Memory metrics: three different things

| Metric | What it is | What it is **not** |
|---|---|---|
| `tracemalloc_peak_bytes` | Peak of Python-traced allocations during one call | Process memory. It misses C allocations that bypass Python's allocator, interpreter overhead, and the input |
| `rss_peak_bytes` | Peak resident set size of the child process tree, sampled by the parent | Exact. Short spikes between samples can be missed. It includes the interpreter, the input, and both profiling calls |
| `ru_maxrss_bytes` | Kernel-reported maximum RSS of the child (`getrusage`) | Specific to the call, because it covers the whole process lifetime |

If a metric cannot be measured on the platform, it is `null` and listed under `unavailable`.

### Optional sampling (py-spy)

With `enable_sampling`, PerfPilot looks for a `py-spy` binary and runs one extra timed call under
`py-spy record --format speedscope`. If the binary is missing, or the process is not root on macOS
(py-spy needs root there), the profile still succeeds and a warning explains why sampling was
skipped.

### Profiler overhead

cProfile instruments every call. Absolute times under the profiler are inflated, and functions with
many cheap calls (e.g. builtins inside a loop) are over-represented. Use profiles to decide *where*
to look. Use the benchmark to decide *whether a change helped*.

### Command mode options

`mode = "command"` runs a module, script or pytest target once under cProfile. Two opt-in flags on
the `command` object are used by Runtime Audits ([runtime-audit.md](runtime-audit.md)):

* `trace_memory`: tracemalloc runs **concurrently** with cProfile (one execution, so the command
  is not run twice); the result gains `memory.tracemalloc_peak_bytes`. Timings then include
  tracemalloc's overhead as well as cProfile's.
* `stats_json`: also write `profile_stats.json`, the pstats table as plain JSON
  (`[file, line, function, cc, nc, tt, ct, callers]`). Consumers of untrusted runs read this
  instead of unmarshalling `profile.prof`, because `marshal` is not safe for untrusted input.

## 3. Benchmarking (paired A/B)

The baseline is the registered workspace. The candidate is a copy of the workspace with the
entrypoint module replaced by the bundled reference (labeled
`reference implementation (bundled, for testing)`), or a copy of a **validated** patch
candidate's workspace (see [optimization-workflow.md](optimization-workflow.md)). Both trees are hashed into `SourceSnapshot` rows.

1. **Preflight.** One call per variant. The run is `INVALID` if the input hashes differ ("refusing
   to compare") or the outputs are not equivalent under the output contract (`exact` hash equality,
   or `float_tolerance` with `math.isclose(rel_tol, abs_tol)` on the canonical values). If a
   preflight run crashes, the benchmark is `FAILED`/`INVALID`.
2. **Timing pairs** (default 20 pairs). Each pair runs baseline and candidate in a seeded random
   order (or alternating order if requested). Each variant runs in a **fresh process**:
   `warmups` untimed calls (default 5), then one timed call. Wall time uses
   `time.perf_counter_ns`, and CPU time uses `time.process_time_ns`; both are recorded. GC stays
   enabled. Every process re-reports `input_sha256`, and any mismatch aborts the comparison.
   Output changes between runs on identical input, and input mutation, are reported as warnings.
3. **Memory runs** (default 3 per variant, alternating) are separate processes, because
   tracemalloc distorts timing. RSS is sampled every 5 ms.
4. Every raw sample is stored (`BenchmarkSample`) and also exported as a JSON artifact.

### Statistics

For each variant (wall and CPU, in ms): `n`, median, mean, min, max, sample stdev, p95 (linear
interpolation), and coefficient of variation. When `n < 100`, p95 carries a warning: with 20 runs
it is determined by the single slowest run.

Formulas (base spec §12.3), using **medians of wall time**:

```
Speedup                 = median_baseline / median_optimized
LatencyReductionPercent = (median_baseline − median_optimized) / median_baseline × 100
ThroughputSpeedup       = optimized_throughput / baseline_throughput
```

Example: 200 ms → 125 ms is a **1.6×** speedup and a **37.5%** latency reduction. The latency
reduction is not `100 − speedup`, and it is not `(speedup − 1) × 100`. Throughput is measured in
calls per second on the same input, so `ThroughputSpeedup` equals `Speedup` here.

**Confidence interval.** A paired percentile bootstrap: draw pair indices with replacement (pairs
stay together, which cancels drift between pairs), compute `median(baseline) / median(candidate)`
for each resample (10,000 by default), and take the 2.5th and 97.5th percentiles. NumPy's
generator is seeded with the request seed, so the CI is reproducible.

### Classification

With threshold `t` (default 5% of baseline latency):

* improvement band: speedup ≥ `1 / (1 − t/100)` (latency reduced by at least t%)
* regression band: speedup ≤ `1 / (1 + t/100)` (latency increased by at least t%)

Rules are applied in order (`[lo, hi]` is the CI):

1. **INVALID**: correctness failed, inputs differ, or runs failed.
2. **INCONCLUSIVE**: fewer than 5 pairs, or the CI spans both bands (too noisy).
3. **IMPROVED**: `lo > 1` and the point speedup is in the improvement band.
4. **REGRESSED**: `hi < 1` and the point speedup is in the regression band.
5. **INCONCLUSIVE**: the point estimate is beyond a threshold, but the CI includes 1.
6. **NO_CLEAR_DIFFERENCE**: everything else. Either the difference is not statistically
   distinguishable, or it is real but below `t` (a 0.1% change is never `IMPROVED`).

### Environment fingerprint

Each run stores: OS, release and version, architecture, CPU model (`sysctl` on macOS,
`/proc/cpuinfo` on Linux), logical and physical CPUs, CPU frequency (only if plausible), affinity,
load average, total memory, Python version, implementation and executable, key package versions,
the `uv.lock` SHA-256, a timestamp, the workload, size and fixture arguments, the baseline,
candidate and source hashes, and all benchmark settings. Factors that were not controlled are
listed explicitly in `notes`: no CPU pinning (and affinity unavailable on macOS), frequency
scaling/turbo, and thermal state.

## 4. Limitations

* **Noise.** Runs share the machine with everything else. Paired, order-randomized runs and the
  bootstrap CI reduce the effect of noise but do not remove it. Re-run on a quiet machine before
  drawing conclusions from a small difference.
* **One timed call per process** keeps processes independent. Very short workloads (< 1 ms) are
  dominated by timer and scheduling noise, so pick a larger size for them.
* **Warm-up** only warms the interpreter state inside the process (caches, allocator, `re` cache).
  Each process starts cold.
* **cProfile overhead** distorts absolute times (see above). Profiles and benchmarks are separate
  runs on purpose.
* **tracemalloc** traces only Python allocator activity. RSS sampling can miss spikes shorter than
  the sampling interval. RSS includes interpreter and input memory.
* **Bundled workloads** are trusted and run in a constrained subprocess on the host. Imported
  repositories must use the Docker sandbox (Phase 8). `RLIMIT_AS` is not enforced on macOS, so the
  sampled-RSS ceiling is the memory guard there.
