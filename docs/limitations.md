# Limitations

What PerfPilot does **not** do, or does only approximately. Methodology details are in
[methodology.md](methodology.md); security residual risks in [security.md](security.md).

## Scope

- **Python only.** Static rules, indexing, profiling and patching target Python 3 source. Other
  languages in a repository are inventoried (counted, sized, classified) but not analysed. Mixed
  repositories (e.g. a Next.js app with a Python backend) are audited on their Python part.
- **Static findings are hypotheses.** Rules match syntactic patterns (`STATIC` provenance). They do
  not know input sizes, call frequency or data shapes, so a flagged loop may be irrelevant in
  practice and real bottlenecks (I/O, the database, C extensions) may produce no finding. Only a
  runtime audit or workload profile yields `MEASURED` data.
- **No cross-language or service-level view.** Network calls, databases, queues and other
  processes are visible only as time spent inside Python frames.

## Measurement

- **Profiling overhead.** cProfile instruments every call: absolute times under the profiler are
  inflated and call-heavy code is over-represented. Profiles say *where* to look; only the paired
  A/B benchmark says *whether a change helped*.
- **tracemalloc vs RSS.** `tracemalloc` counts Python-allocator allocations only (it misses C
  allocations, interpreter overhead and the input). RSS is sampled, so short spikes can be missed,
  and it includes the interpreter and everything else in the process. They answer different
  questions and are reported separately; neither is "the memory usage".
- **Hardware and noise.** Results depend on CPU model, frequency scaling, thermal state, other load
  and the container's CPU quota. Paired, order-randomised runs with bootstrap confidence intervals
  reduce but do not remove noise. Results are comparable only within one environment fingerprint;
  shared CI runners are never treated as authoritative.
- **Docker Desktop on macOS/Windows.** Containers run inside a Linux VM. Timings include the VM's
  scheduling, and bind-mounted files go through a file-sharing layer (virtiofs) that is much
  slower than a native filesystem for I/O-heavy code. A runtime audit on a Mac measures the code
  *in that VM*, not on bare metal or on your production Linux hosts.
- **Very short workloads** (< 1 ms) are dominated by timer and scheduling noise.
- **py-spy sampling** is optional, available for bundled workloads only (not inside the runtime
  sandbox), and needs root on macOS; without it only deterministic profiling is available.

## Validation and benchmarking

- **Correctness validation is only as good as the tests and reference cases.** PerfPilot runs the
  workload's correctness checks and the repository's approved test command; behaviour that no test
  covers can change unnoticed. Passing validation is evidence, not proof, of equivalence.
- **Patches are narrow.** The default deterministic proposer handles a fixed set of rewrite
  patterns. Other proposers can be plugged in, but every patch must pass the patch validator
  (allowed files only, public signatures kept, no test or benchmark changes), so many valid
  optimisations are out of reach.
- **Benchmarks measure the workload you approved**, at the sizes you chose. They do not predict
  production behaviour under different data, concurrency or hardware.

## AI

- **LLM output is a hypothesis** (`AI_HYPOTHESIS`). Models can be wrong, overconfident or
  influenced by text in the repository (prompt injection). Citations are checked against the
  index, but the reasoning is not. Without an LLM the deterministic advisor is used.
- **Token budgets** truncate context on large repositories; summaries are hierarchical and lossy.
- **Cost accounting** uses the per-1K prices you configure and the usage the endpoint reports
  (estimated and flagged when it reports none).

## Sandbox and deployment

- **Containers are not VMs.** Sandboxes share the kernel with the host (or the Docker Desktop VM);
  a kernel or runtime exploit can escape. gVisor/Kata are not integrated.
- **Approved dependency installs have network access** and run package build steps; malicious
  packages are not detected.
- **No disk quota** inside a run — only the timeout bounds writes.
- **Repository code that needs services** (databases, network, GPUs, secrets) cannot run in the
  offline sandbox; such commands fail and are reported as failed, not measured.
- **The Docker socket** given to the worker is root-equivalent on the host (see
  [deployment.md](deployment.md#the-docker-socket-trade-off)).
- **Single-user, local.** No authentication, one SQLite database, one worker by default. Several
  workers can share the queue, but SQLite limits write concurrency.
