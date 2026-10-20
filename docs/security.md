# Security

PerfPilot imports **untrusted repositories**, can execute their code (runtime audits) and can send
excerpts to an external LLM. This document is the threat model, the controls that exist (with the
tests that pin them), and the risks that remain. Component details live in
[ingestion.md](ingestion.md), [sandbox.md](sandbox.md), [ai-design.md](ai-design.md) and
[optimization-workflow.md](optimization-workflow.md).

## Assets and trust boundaries

| Asset | Where |
|---|---|
| Host filesystem, other containers, the Docker daemon | host / Docker Desktop VM |
| LLM API key and other settings | process environment, `.env` (git-ignored) |
| Secrets inside imported repositories (`.env`, keys in code) | workspaces under the data dir |
| Audit results, reports, artifacts | SQLite + data dir |

```
browser ──► Streamlit UI ──► API (validates, persists, enqueues; never runs repo code, no Docker)
                               │
                               ▼  jobs table
                             worker ──► ingestion / indexing / rules (parse only, never import)
                               │   ──► LLM endpoint (opt-in, consented, redacted, no tools)
                               └─(docker.sock)──► sandbox container (untrusted code, no network)
```

**Attackers considered:** a malicious or compromised repository (code, tests, configuration,
archive structure, file names, dependency metadata); a malicious URL or path supplied to the API;
a misbehaving or prompt-injected LLM; a web page in the user's browser trying to reach the local
API (CSRF/CORS). **Out of scope:** a malicious local user with shell access, a compromised host,
multi-tenant isolation (PerfPilot has **no authentication** and must listen on localhost only).

## Controls

### Ingestion (`backend/ingestion/`)

| Threat | Control | Tests |
|---|---|---|
| Zip-slip, absolute paths, `..`, drive letters | entry names normalised and contained; rejected otherwise | `tests/security/test_ingestion_security.py` |
| Symlinks / hardlinks / devices in archives or local dirs | rejected or not followed; workspace reads refuse symlinks | same; `test_api_hardening.py::test_file_api_refuses_a_symlink_planted_in_the_workspace` |
| ZIP bombs | entry-count, per-file, total-size and compression-ratio limits enforced on **actual** bytes | `test_zip_bomb_*` |
| Huge uploads | streamed to disk with a byte cap; request-body middleware caps everything else | `tests/api/test_repositories_api.py::test_upload_size_limit`, `test_api_hardening.py` |
| SSRF via GitHub URLs | `https` only, exact host allowlist (`github.com`), no userinfo/port/query/fragment, strict owner/repo/ref grammar; git runs with `protocol.allow=never` except https, `http.followRedirects=false`, no hooks, no credential helper, no submodules | `tests/unit/test_ingestion_github.py`, `test_api_hardening.py::test_github_import_rejects_ssrf_shaped_urls` (IP literals, cloud metadata, IDN look-alikes) |
| Reading arbitrary host directories | local imports only from `PERFPILOT_LOCAL_ALLOWLIST`; resolved paths must stay inside; the data dir itself is refused | `test_allowlist_escapes_are_rejected`, `test_local_path_traversal_is_rejected` |
| Secrets in repositories | sensitive files (`.env`, keys, credentials) are never copied into workspaces nor served; file content is redacted before it is returned | `test_sensitive_*`, `test_file_content_is_redacted` |

### API (`backend/main.py`, `backend/api/`)

- **Request size:** `BodySizeLimitMiddleware` rejects bodies above `PERFPILOT_MAX_REQUEST_BYTES`
  (1 MiB; ZIP uploads use `PERFPILOT_MAX_UPLOAD_BYTES`) with `413`, using `Content-Length` and the
  actual streamed byte count (chunked or lying clients).
- **Bounded inputs:** every list endpoint takes `limit` (max 500) / `offset`; string fields in
  request models have maximum lengths; runtime approvals cap the argv item count and must match a
  discovered command exactly.
  Snapshot file lists and graphs are bounded by the ingestion limits.
- **Path traversal in the file-content API:** the path must be a member of the snapshot inventory,
  is normalised, refused if sensitive, resolved inside the workspace without following symlinks,
  and its SHA-256 must match the ingested file.
- **CORS:** only `PERFPILOT_CORS_ALLOWED_ORIGINS` (the local UI origins by default), no
  credentials. The UI calls the API server-side, so browsers never need CORS for normal use.
- **Headers:** `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer` everywhere; report downloads are `attachment`s and carry
  `Content-Security-Policy: sandbox; default-src 'none'; …`. Report previews are `text/plain`.
- **Errors:** a single JSON envelope; unhandled job exceptions and tracebacks are **redacted**
  before they are stored and returned by `/api/jobs` (they can quote repository content).
- **No shell, no Docker:** the API never executes repository code or shells out with user input.

### Static analysis

Repository code is parsed with `ast`, never imported or executed (ADR-003). Parse errors are
recorded per file.

### Runtime sandbox (`backend/sandbox/`)

Untrusted code runs only in `perfpilot-sandbox` containers started by the worker: `--network
none`, read-only root and workspace, non-root uid 65534, `--cap-drop ALL`,
`no-new-privileges`, CPU/memory/PID/time limits, a small `noexec` tmpfs, output size caps, no host
environment, fixed argv built from discovered commands that the user approved **exactly**
(single-use, expiring approvals). Bind-mount sources are validated to live under the data dir.
See [sandbox.md](sandbox.md#threat-model) and `tests/sandbox/` (real-Docker tests are
`@pytest.mark.docker` and run in CI).

### Patches (`backend/optimization/patch_validator.py`)

Generated diffs may only touch allowed source files; tests, benchmarks, harness and config files
are protected; renames, binary patches, new/deleted files, symlinked targets and paths outside the
workspace are rejected; patched Python must compile and keep public signatures; benchmark
tampering is detected. Validation runs on a copy, never on the original workspace.
`tests/security/test_patch_validator.py`.

### LLM outbound path (`backend/ai/`)

Off unless configured **and** the user consents per audit. Context is curated and redacted
immediately before sending; the client has no tools, follows no redirects, and only parses
schema-validated JSON whose file/line citations are checked against the index (citation guard).
Token budgets cap spend. Logs record model, host, tokens and cost, never prompts, completions or
the key. `tests/security/test_llm_outbound.py`, `tests/unit/test_observability.py`.

### Reports and UI rendering

The HTML report uses Jinja2 autoescaping; `Markup` is only built from escaped pieces; a final pass
redacts secrets in every format (HTML, Markdown, JSON, CSV, PDF). UI helpers that render HTML
(`unsafe_allow_html`) escape repository- and LLM-controlled text.
`tests/reporting/test_report_renderers.py`, `tests/frontend/test_code_and_diff.py`,
`tests/security/test_ui_html_escaping.py`.

### Secrets in logs

JSON logs scrub common credential formats from messages, fields and tracebacks; request logs
record the path only (no query string). Settings hold the LLM key as `SecretStr`.
`tests/security/test_api_hardening.py::test_request_logs_do_not_include_query_strings`.

### Supply chain and CI

Dependencies are hash-locked (`uv.lock`); the app image installs them with `--require-hashes`.
`security.yml` runs pip-audit on the lock export, gitleaks over the full history (fake test
credentials are allowlisted under `tests/` only, via `.gitleaks.toml`) and CodeQL, on every push,
PR and weekly. Dependabot updates Python packages, Actions and base images. Workflows need no
secrets.

## Security review (Phase 11)

Reviewed: ingestion, the file-content API, report rendering, the sandbox, the patch validator,
the LLM outbound path and every route, for path traversal, symlink tricks, ZIP bombs, SSRF,
command injection, XSS, secrets in logs/reports/exports, unbounded inputs and CORS.

| Finding | Severity | Fix |
|---|---|---|
| No request-body limit outside the ZIP upload route (memory exhaustion via huge JSON) | Medium | `BodySizeLimitMiddleware` |
| Unhandled job exceptions/tracebacks stored and served unredacted (e.g. a `SyntaxError` quoting a secret line) | Medium | redacted in `backend/jobs/worker.py` |
| `RepositoryRef` string fields unbounded | Low | max lengths in `backend/models/audit_schemas.py` |
| CORS allowed every method and header | Low | explicit methods/headers, origins configurable |
| No `nosniff`/frame/referrer headers; downloads without CSP | Low | `SecurityHeadersMiddleware` |
| Sandbox mounted the profiling harness from the installed package path, which is not a host path when the worker is containerised | Functional (deployment) | harness staged under the data dir (`backend/runtime/execution.py`) |

No path-traversal, symlink, ZIP-bomb, SSRF, command-injection or report-XSS issue was found; the
new tests above pin the behaviour.

### Accepted dependency advisories

`pip-audit` runs on every push and weekly. Advisories that cannot be fixed yet are listed in
[`.github/pip-audit-ignores.txt`](../.github/pip-audit-ignores.txt), each with the reason it is not
exploitable in PerfPilot and a review-by date. Currently: the Pillow 12.3.0 advisories fixed in
13.0.0. Streamlit still requires `pillow<13`, and PerfPilot never decodes untrusted raster images.
Ingestion skips images as binaries, reports embed only generated SVG, and the PDF engines are
restricted to `data:` URIs that PerfPilot itself creates.

## Residual risks

- **No authentication.** Anyone who can reach the API can import, run approved commands and read
  results. Keep ports bound to `127.0.0.1` (the compose default) or put an authenticating reverse
  proxy in front.
- **Docker socket = host root.** The worker's socket access is root-equivalent on the host. See
  [deployment.md](deployment.md#the-docker-socket-trade-off) for rootless Docker, a socket proxy, or
  running the worker on the host.
- **Container escape.** Sandboxes share a kernel with the host (or the Docker Desktop VM). gVisor is
  not integrated. Dependency installs (explicitly approved) have network access.
- **Pattern-based redaction** catches common token formats and high-entropy assignments, not every
  secret. Do not import repositories you are not allowed to share with the configured LLM.
- **LLM output** is advisory (`AI_HYPOTHESIS`); prompt injection in repository text can bias it,
  but it cannot call tools or bypass the patch validator, and it never becomes `MEASURED`.
- **Disk usage** inside a sandbox run is bounded by the timeout, not a quota.
- **Plotly hover labels** in the dependency graph render module and file names with Plotly's own
  limited markup (no script execution); exotic file names may display oddly.
- **Local single-user design:** SQLite, no rate limiting, no audit log of API callers.
