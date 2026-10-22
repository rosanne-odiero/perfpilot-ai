# Runtime sandbox

Runtime Audits run code from imported repositories. That code is untrusted. Per
[ADR-004](decisions.md) it runs **only** inside a Docker container started by the **worker**
(never the API, [ADR-005](decisions.md)). If Docker is not available, runtime mode is disabled with
a reason. There is no host fallback.

| Module | Responsibility |
|---|---|
| `backend/sandbox/backend.py` | `ExecutionBackend` protocol, request/result/limit contracts, `get_execution_backend()` |
| `backend/sandbox/docker_backend.py` | `DockerSandboxBackend`: argv construction, path validation, run/kill/inspect/cleanup |
| `backend/sandbox/command_discovery.py` | Static discovery of candidate commands and the argv allowlist |
| `backend/sandbox/approvals.py` | Recording commands; approving, revoking and consuming approvals |
| `backend/sandbox/environment.py` | Separately approved dependency installation into a per-snapshot `deps` dir |
| `backend/models/orm/runtime.py` | `runtime_commands` and `runtime_approvals` tables |
| `docker/sandbox.Dockerfile` | The sandbox image (`make sandbox-image`) |

## Flow

1. **Discover** (`discover_commands`). Reads configuration with `tomllib`/`configparser`/`json` and
   Python files with `ast`. Nothing is executed. Every proposal is `STATIC` provenance with
   `file:line` evidence.
2. **Record** (`record_discovered_commands`). Stores the proposals for an audit. This step is
   idempotent per stable command id.
3. **Approve** (`approve_command`). The client must submit the *exact* discovered argv. Any
   difference, including reordering, is a `SecurityViolationError`. The stored command is
   re-validated against the snapshot workspace. The approval pins the argv, the working
   directory, the configured image, and the limits. Limit overrides may only tighten the
   configured defaults. The approval also records `approved_by` and an expiry
   (`PERFPILOT_SANDBOX_APPROVAL_TTL_SECONDS`, 24 h by default).
4. **Optionally install dependencies** (`consume_dependency_install` → `prepare_environment`).
   This needs `allow_dependency_install=True` on the approval and can be used only once. Source
   builds need the separate `allow_sdist_builds` flag.
5. **Execute** (`consume_approval` → `backend.run`). Consumption is atomic and single use. The
   worker runs exactly the returned argv, image and limits.

The Runtime Audit (API routes, the `runtime.profile` job, correlation and reporting) is built on
top of these services; see [runtime-audit.md](runtime-audit.md).

### Mount sources must be host paths

The worker passes bind-mount sources to the Docker daemon, which resolves them on the **host**.
Every source therefore lives under the data dir (workspaces, `artifacts/`), including the
profiling harness, which is copied to `artifacts/runtime-runs/<run>/in/` per run rather than
mounted from the installed package. When the worker itself runs in a container, the data dir is
mounted at the same absolute path as on the host ([deployment.md](deployment.md)).

## Container hardening

Every container (execution and dependency install) gets these flags from `hardening_flags()`:

| Flag | Purpose |
|---|---|
| `--network none` | No network for execution. The dependency install alone uses `bridge`. |
| `--read-only` | Immutable root filesystem |
| `--tmpfs /tmp:rw,noexec,nosuid,nodev,size=<n>m` | Bounded scratch space. Nothing in it can be executed. |
| `--cpus`, `--memory`, `--memory-swap` (= memory), `--pids-limit` | Resource limits. Swap is disabled, so the OOM killer triggers at the limit. |
| `--cap-drop ALL`, `--security-opt no-new-privileges` | No Linux capabilities and no privilege escalation. Setuid bits are also stripped in the image. |
| `--user 65534:65534` | Non-root (`nobody`) |
| `--ipc private`, `--hostname sandbox` | No IPC namespace or hostname shared with the host |
| `--ulimit core=0:0`, `--ulimit nofile=1024:1024` | No core dumps, bounded file descriptors |
| `--init` | A real PID 1 that reaps zombies and forwards signals |
| `--pull never` | Never pulls images at run time |
| `--label perfpilot.sandbox=1` | Identifies leftovers |

Mounts use `--mount type=bind,...`. Host paths are validated before mounting:

| Container path | Host source | Mode |
|---|---|---|
| `/workspace` | snapshot workspace (must be inside `workspaces_dir`) | read-only |
| `/out` | empty output dir (inside `workspaces_dir` or `artifacts_dir`, not overlapping the workspace) | read-write |
| `/deps` | prepared dependency dir (inside `workspaces_dir`) | read-only, `PYTHONPATH=/deps` |
| `/perfpilot/<name>` | extra files such as the trusted profiling harness `backend/profiling/harness_entry.py` or a request JSON (inside `workspaces_dir`, `artifacts_dir` or the PerfPilot `backend` package) | read-only |

Each host path must be absolute and must not be a symlink. Its resolved target must be strictly
inside an allowed root, so `..` and symlinks that escape the root are rejected. Paths containing
`,`, `"`, or control characters are rejected so they cannot break the mount syntax.

The environment is fixed as `HOME=/tmp`, `TMPDIR=/tmp`, `PYTHONDONTWRITEBYTECODE=1`,
`PYTHONUNBUFFERED=1` (plus `PYTHONPATH=/deps`). Callers may add only allowlisted names, such as
`PYTHONHASHSEED`, `TZ`, `LANG`, `*_NUM_THREADS`, and `PERFPILOT_HARNESS_*`. A value that the
redaction module flags as a secret is rejected. The docker CLI itself gets only `HOME` and the
`DOCKER_*`/`XDG_RUNTIME_DIR` variables, so keys from the worker's environment, such as the LLM key,
never reach it.

### Lifecycle, timeouts and OOM

* The docker CLI is resolved once to an absolute path, from `PERFPILOT_SANDBOX_DOCKER_PATH` or from
  `PATH` plus `/usr/local/bin`, `/opt/homebrew/bin`, `/usr/bin` and the Docker Desktop bundle. It
  is always invoked with an argv list (never a shell) and with timeouts.
* Containers are started **without `--rm`** under a unique name. After the client exits, the
  worker runs `docker container inspect` to read `State.ExitCode`, `State.OOMKilled` and the image
  ID. It then always runs `docker rm --force` in a `finally` block. With `--rm`, the container
  would be removed before the OOM flag could be read.
* Wall-clock timeout: `docker kill <name>`. If the client still has not exited after a grace
  period, the client process is killed. The result has `timed_out=true`.
* stdout and stderr are drained by bounded reader threads. Each stream is truncated to
  `output_max_bytes` and then passed through `backend.core.redaction`.
* Files in `/out` are collected afterwards without following symlinks. Only regular files within
  the size and count caps are listed. Symlinks, FIFOs, devices and oversized files are deleted.

### Availability

`availability()` runs `docker info` and `docker image inspect <sandbox image>`, each with a
timeout. It returns `BackendStatus(available, backend, reason, details)`. `get_execution_backend()`
raises `UnavailableError("runtime mode is disabled: …")` when the backend is unavailable.

## Command discovery

| Source | Result |
|---|---|
| `pytest.ini`, `pyproject [tool.pytest.ini_options]`, `setup.cfg [tool:pytest]`, `tox.ini [pytest]` | `pytest`, run from the config directory (`testpaths`/`python_files` honoured) |
| `test_*.py` / `*_test.py` without a config (incl. unittest `TestCase` suites), `conftest.py` | `pytest` from the nearest project root (`pyproject`/`setup.*`/`requirements*.txt`) |
| test functions taking a `benchmark` argument (unless the project defines its own `benchmark` fixture) | `pytest_benchmark` |
| `.py` files with `if __name__ == "__main__":` under `benchmarks/ bench/ perf/ performance/ profiling/ scripts/` | `script` |
| `__main__.py` packages in those dirs, `[project.scripts]` / `[tool.poetry.scripts]` modules that can run with `-m` | `module` |
| `asv.conf.json` | `asv` (not runnable: asv builds its own environments) |
| tox envs, nox sessions, Makefile targets named like test/bench/perf/profile | `info_only` (PerfPilot never runs tox, nox or make) |

Each candidate also reports `requires_dependencies`, which is true when its files import
third-party modules that are neither stdlib, local, nor preinstalled in the image. It also carries
a heuristic `estimated_risk`: `high` for imports of network, cloud or DB clients, `medium` for
`subprocess`/`shutil`/`socket`, for anything in `scripts/`, or when dependencies are needed.
Vendored and tool directories (`node_modules`, `.venv`, `build`, `.tox`, …) and symlinks are
skipped.

**Argv allowlist** (`validate_command_argv`), applied at discovery and again at approval:

* `python -m pytest [paths or node ids inside the workspace] [-k EXPR] [-x] [-q]`
* `python <relative .py file inside the workspace>` (no extra arguments)
* `python -m <module that exists inside the workspace>`

Everything else is rejected. That includes `-c`, `-I`, `-m pip`, and pytest options such as `-p`,
`--rootdir`, `-c`, `-o`, `--pyargs`, `--confcutdir` and `-W`. Paths that escape the workspace or
working directory, including through symlinks, are rejected too.

## Dependency preparation

Dependency installation is the only step with network access, and it is approved separately:

1. The requirements file (following `-r` includes inside the workspace) or PEP 621
   `[project.dependencies]` plus the selected extras is parsed **statically** with `packaging`.
   pip options (`--index-url`, `--extra-index-url`, `-e`, `-f`, `-c`, `--trusted-host`), direct
   URL/VCS references and local paths are dropped and reported (redacted). Poetry tables and
   dynamic dependencies are not supported.
2. The sanitized list is written to `<workspaces_dir>/runtime-envs/<snapshot_id>/requirements.txt`
   and mounted read-only. **The repository itself is not mounted.**
3. `python -m pip install --target /deps --only-binary=:all: -r /perfpilot/requirements.txt` runs
   in a hardened container with `--network bridge`. Using wheels only means no repository-controlled
   `setup.py` or build backend runs. With `allow_sdist_builds`, `--prefer-binary` is used instead.
   An index can be configured with `PERFPILOT_SANDBOX_PIP_INDEX_URL` (no credentials).
4. The install goes into a staging dir. On success it is renamed to `…/deps`. On failure it is
   removed. Metadata is written to `environment.json`. Later runs mount `deps` read-only with
   `--network none`.
5. If pip fails because the **package index** failed (an HTTP 404/408/429/5xx while getting a file,
   a timeout or a dropped connection), the same approved install (same manifest, image and limits,
   fresh staging dir) is retried up to `PERFPILOT_SANDBOX_INSTALL_ATTEMPTS` times (default 3), with
   a growing delay (`PERFPILOT_SANDBOX_INSTALL_RETRY_DELAY_SECONDS`, default 10 s × attempt).
   Mirrors that proxy PyPI on demand (for example Azure Artifacts upstream feeds) can return 404 for
   a file, including PEP 658 `.whl.metadata`, until the package is cached; a retry then succeeds.
   Real install errors (no matching distribution, build failures, timeouts, OOM) are not retried.
   The run records `attempts` and `retry_reasons`, and a failed run's reason quotes pip's error.

## Threat model

**Attacker:** a malicious or compromised repository, including its code, tests, `conftest.py`,
pytest config (`addopts = -p …`) and declared dependencies, plus anyone who can influence the
command to be run (an API client, or an LLM suggestion).

**Protected**

* No network during execution. Code cannot exfiltrate data or reach internal services.
* The host filesystem is unreachable except for the read-only workspace, `/deps` and harness, and
  the per-run `/out`. The repository cannot modify its own snapshot.
* No root, no capabilities, no privilege escalation, and no setuid binaries. The PID, memory, CPU,
  tmpfs and time limits contain fork bombs, memory exhaustion and hangs.
* Users cannot inject arguments. Only discovered, allowlisted argv can be approved, approval
  requires an exact match, and approvals are single-use and expire.
* Secrets: the worker's environment never reaches the container or the docker CLI. Output is
  redacted before it is stored.
* The API process never touches Docker. Only the worker does.

**Not protected / limitations**

* **Kernel boundary.** Containers share a kernel. On Linux that is the host kernel. On macOS,
  Docker Desktop runs containers in a Linux VM, so the VM adds an outer boundary, but all
  containers share that VM's kernel. A kernel or runtime exploit can escape. Stronger options:
  run the worker against **rootless Docker** or `userns-remap` so container root is not host root,
  or use **gVisor** (`runsc` as the container runtime), which is a planned option rather than
  implemented. Docker's default seccomp and AppArmor profiles stay enabled. Never set
  `--privileged`.
* **Disk usage.** `/out` and `/deps` are bind mounts without a quota. A run can write until its
  timeout. Collected outputs are capped afterwards, not during the run. Put the data dir on a
  quota-limited volume if this matters.
* **The dependency install has network access.** Wheels can still contain malicious code that
  runs later, inside the offline sandbox. Package-index typosquatting is not detected.
* **Leftover containers.** If the worker is SIGKILLed mid-run, the `finally` cleanup does not run.
  Leftovers carry the `perfpilot.sandbox=1` label:
  `docker ps -a --filter label=perfpilot.sandbox=1`.
* **Measurement fidelity.** CPU quotas, the Docker Desktop VM and virtiofs bind mounts all affect
  timings. Compare runs only when they used the same environment.
* **Linux permissions.** Output and staging dirs are made `0777` so uid 65534 can write to them.
  They live under PerfPilot's data dir, which should not be readable by other users. The workspace
  must be readable by uid 65534.
* **Redaction is pattern-based.** It catches common token formats. It is not a guarantee.

## Running the integration tests

```bash
make sandbox-image          # docker build -f docker/sandbox.Dockerfile -t perfpilot-sandbox:latest docker
make test-docker            # pytest -m docker
PERFPILOT_RUN_NETWORK_TESTS=1 .venv/bin/pytest -m "docker and network" tests/sandbox
```

`@pytest.mark.docker` tests in `tests/sandbox/` are skipped with the reason when `docker info`
fails or the image is missing. They check against a real container that the network is blocked,
that only `/out` and `/tmp` are writable, that `/tmp` is noexec, that the process runs as uid
65534 with no capabilities, that the memory limit triggers OOM, that the PID limit caps threads,
that the timeout kills the run, and that no container is left behind. The unit tests use a
clearly labelled docker CLI *test double* only to exercise PerfPilot's own process handling.
