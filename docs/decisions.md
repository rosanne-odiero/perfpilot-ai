# Architecture Decision Log

Short records of decisions made during planning. Each one can be revisited. If it is, add a new
entry rather than editing the old one.

### ADR-001 — Repository Audit is the primary entry point
The product starts from a whole repository and produces a prioritized static assessment. The
single-workload optimize → validate → benchmark loop is the deep-dive workflow reached from a hotspot.
*Why:* this matches how engineers actually begin, and it is a stronger demonstration than single-function
optimization.

### ADR-002 — Three provenance labels on every result
`MEASURED`, `STATIC`, and `AI_HYPOTHESIS` are stored on findings, recommendations, and metrics, and rendered
in every report format. *Why:* the core promise is separating optimizations that sound good from ones that are verified.

### ADR-003 — Static analysis never executes repository code
Analysis uses `ast` only. `setup.py` is read with `ast`, never run. Dependencies are discovered from manifests.
*Why:* the default Static Audit must be safe for any public repository.

### ADR-004 — Untrusted code runs only in a Docker sandbox, with no host fallback
Imported repositories are executed only by `DockerSandboxBackend` (no network, resource limits, non-root,
read-only source). If Docker is unavailable, runtime mode is disabled. Bundled `examples/` are trusted
and may use a constrained subprocess. *Why:* the feature spec forbids running untrusted code in the API or host process.

### ADR-005 — The worker, not the API, owns Docker access
The API container never mounts the Docker socket. *Why:* it limits the blast radius of an API compromise.

### ADR-006 — Persistent DB-backed job queue
Jobs live in SQLite with leases and heartbeats, and a separate worker process consumes them. There is no Celery or Redis
for the MVP. *Why:* jobs survive restarts and the dependencies stay simple, as the base spec requires.

### ADR-007 — Hierarchical, budgeted LLM analysis with a citation guard
The LLM only sees retrieved snippets for relevant units. Outputs are Pydantic-validated. Every cited
path, line, and symbol is checked against the index, and numeric runtime claims are rejected. A deterministic
advisor is the default. *Why:* this avoids whole-repo prompts and invented evidence, and the product works without an API key.

### ADR-008 — Private GitHub via GitHub App / OAuth, never PATs in URLs
The `PrivateGitHubProvider` interface is defined in Phase 2. Tokens are short-lived and passed
through `GIT_ASKPASS` or a header, and they are redacted from logs. *Why:* this is the secure authorization the spec requires.

### ADR-009 — PDF via WeasyPrint from the same HTML template
One Jinja2 template drives both HTML and PDF. WeasyPrint needs Pango, which is installed in the Docker image
and documented for local use. *Why:* HTML and PDF stay identical. Alternatives were ReportLab (a second
layout) and headless Chromium (a heavy dependency).

### ADR-010 — Streamlit frontend with custom CSS
The base spec mandates Streamlit. The shared mockups guide layout and styling only. The dependency graph
uses Plotly with a networkx layout. *Why:* it meets the spec and keeps everything in one language.

### ADR-011 — Python 3.12 and uv
3.12 is the project's target (≥3.11 per spec). Dependencies are locked with `uv.lock`. *Why:* it matches the installed
interpreter and gives reproducible environments.
