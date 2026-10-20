# Reports

PerfPilot builds one `AuditReport` (a Pydantic model in `backend/reporting/model.py`) from the
database. That model is then rendered to HTML, Markdown, JSON, CSV or PDF. Every renderer reads the
same model, so the formats cannot disagree.

```
DB rows ──► build_audit_report() ──► AuditReport ──► redact ──► render_report(fmt) ──► redact ──► artifact
              (builder.py)              │                         (renderers/)
              report providers ─────────┘ (optional measured sections)
```

## Sections

| # | Section | Source | Provenance |
|---|---|---|---|
| 1 | Executive summary | Deterministic text built from counts. No numbers are invented. | — |
| 2 | Repository metadata & analysis environment | `Repository`, `RepositorySnapshot`, `AuditRun`, `LLMUsage` | — |
| 3 | Files & modules analyzed | `RepositoryInventory`, `CodeIndex` (skipped by reason, limits hit, parse errors) | — |
| 4 | Architecture overview & dependency graph | `CodeIndex.graph`, inventory technologies, architecture insights | STATIC / per insight |
| 5 | Prioritized findings | `FindingRecord.data`, ordered by priority score, then severity, path, line | per finding |
| 6 | Source references & evidence | Snippet with line numbers. GitHub permalink pinned to the commit SHA. | per finding |
| 7 | CPU & memory measurements | **Optional.** Report providers only. | MEASURED |
| 8 | Algorithmic complexity analysis | `Finding.complexity_note`, labeled *estimate* | per finding |
| 9 | Proposed optimization strategies | `RecommendationRecord.data` | per recommendation |
| 10 | Suggested code changes & diffs | `Recommendation.suggested_diff`, or "No code changes were generated" | per recommendation |
| 11 | Correctness validation outcomes | **Optional.** Report providers only. | MEASURED |
| 12 | Measured benchmark results | **Optional.** Report providers only (medians, speedup, CI, classification, samples, environment) | MEASURED |
| 13 | Estimated engineering effort & uncertainty | Recommendation effort sizes (qualitative, not time estimates) | per recommendation |
| 14 | Optimization roadmap | Deterministic grouping, see below | per recommendation |
| 15 | Analysis limitations & reproducibility | Static caveats, limits, LLM fallbacks, audit warnings, commit, config, environment, commands | — |

When an optional section has no data, the report keeps the section and says
**"Not measured — static audit only."** It contains no numbers. Tests check that a static-only
report has no MEASURED badge and no time, memory, percentage or speedup figure in any format.

Rules the builder follows:

- Only audits with status `SUCCEEDED` or `PARTIAL` can be reported. Any other status raises
  `ConflictError` (HTTP 409).
- A snippet is numbered from `location.line_start`, so the snippet's first line is that line.
  Snippets are capped at 40 lines and diffs at 400 lines.
- A file that matches `backend.core.redaction.is_sensitive_path` (`.env`, keys,
  `credentials.json`, …) never has its content shown. Its snippet or diff is replaced by a
  "Content withheld" note.
- Permalinks are only built for `GITHUB_PUBLIC` and `GITHUB_PRIVATE` sources with a well-formed
  `https://github.com/<owner>/<repo>` URL and a hex commit SHA. They have the form
  `…/blob/<sha>/<path>#L<start>-L<end>`. Paths containing `..` or starting with `/` get no link.
- Fan-in, fan-out and per-module finding counts are recomputed from the graph edges and this
  audit's findings.

### Roadmap rules (section 14)

1. **Strategic** — every recommendation with `LARGE` effort.
2. **Quick wins** — `SMALL` effort with at least one linked finding of `MEDIUM` severity or higher.
3. **Next** — everything else.

Within a phase, items are sorted by advisor priority rank (unranked last), then by highest linked
severity, then by recommendation id. The rules are also printed in the report.

## Provenance

Every finding, recommendation, insight and metric shows a badge: `MEASURED` (code ran in an
isolated PerfPilot environment), `STATIC` (deterministic AST/rule output, nothing executed) or
`AI_HYPOTHESIS` (LLM/heuristic suggestion, unverified). Each report starts with a legend that
explains the three labels. The measured sub-models (`RuntimeProfile`, `ValidationOutcome`,
`BenchmarkResult` and their sections) fix `provenance` to `MEASURED`. Static or AI output therefore
fails validation if it is placed in those sections.

## Formats

| Format | Renderer | Notes |
|---|---|---|
| `html` | `renderers/html.py` + `templates/report.html.j2` | Jinja2 with autoescape. Has a cover header, table of contents, legend, severity/provenance badges, numbered code snippets, an inline SVG graph and colored diffs. Responsive, with an `@media print` stylesheet. No scripts and no external resources. |
| `md` | `renderers/markdown.py` | GitHub-flavored, for pasting into a PR or issue. The graph is a Mermaid diagram of the top modules and entrypoints. Code is in fenced blocks. |
| `json` | `renderers/json_export.py` | `AuditReport.model_dump(mode="json")`, with `schema_version` at the top level. Parses back into `AuditReport`. |
| `csv` | `renderers/csv_export.py` | Findings table. If benchmark samples exist, a second file (`role=benchmark_samples`) holds the raw samples. Cells that a spreadsheet would read as a formula (`= + - @`) are prefixed with `'`. |
| `pdf` | `renderers/pdf.py` | The same template rendered with the print stylesheet, then converted. See below. |

File names follow `perfpilot-<repo>-<sha7>-audit.<ext>`. The CSV files are named
`perfpilot-<repo>-<sha7>-findings.csv` and `…-benchmark-samples.csv`.

### PDF engines

`available_pdf_engine()` uses **WeasyPrint** if it imports and its native libraries (Pango/Cairo)
load. Otherwise it falls back to **xhtml2pdf**. The engine that was used is printed in the report
footer, stored in the artifact metadata (`pdf_engine`), and returned by
`GET /api/reports/{id}` under `meta`.

| | WeasyPrint | xhtml2pdf |
|---|---|---|
| Install | Python package + Pango (bundled in the Docker image; `brew install pango` on macOS) | Pure Python (already a dependency) |
| CSS | Modern CSS and paged media. Page numbers come from `@bottom-right`. | CSS 2.1 subset only: no flexbox, grid, variables, `border-radius`, gradients or HTML5 sectioning backgrounds. Page numbers come from an `@frame` footer. |
| SVG | Native | Through svglib (basic shapes and text). The graph SVG uses only lines, circles, polygons and text for this reason. |
| Fonts | System fonts | Base-14 fonts. Glyphs outside Latin-1 (for example CJK in snippets) appear as boxes. |

So that both engines produce the same layout, the PDF variant uses `report_print.css` (CSS 2.1
only) plus a small engine-specific page stylesheet (`page_weasyprint.css` / `page_xhtml2pdf.css`).
It also embeds SVGs as `data:` images and uses `<div>` instead of `<header>`/`<nav>`. Both engines
may only load `data:` URIs, so PDF rendering never touches the network or the filesystem.

## Secret redaction

1. Before rendering, every string in the `AuditReport` is passed through
   `backend.core.redaction.redact_text`. Doing this before escaping means escaping cannot hide a
   secret from the matcher.
2. Every rendered output is redacted again, using a pass that respects the format's structure:
   per text node for HTML (and for the HTML that becomes the PDF), over the whole text for
   Markdown, and per value for JSON and CSV. This keeps the output well-formed. Running the
   pattern matcher over raw markup could let a false positive swallow tags.

The total number of redactions is stored in the artifact metadata (`redaction_count`,
`redaction_kinds`). The number from the first pass is also printed in the report footer. The
contents of sensitive files are never included, redacted or not.

`file:line` references such as `app/auth.py:1012-1040` are replaced by long placeholders before
matching. Without this, the generic `name: value` secret rule would treat them as an assignment and
redact the line range. The placeholders look like secrets to the matcher, so a location-shaped
string that is the value of a secret assignment (`password=v1.prod:9876`) is still redacted.
References followed by `@` are never masked, so URL credentials are always redacted.

## Jobs and API

The `report.generate` job (`backend/jobs/handlers/reports.py`) takes the payload
`{"audit_id": "...", "format": "html|md|json|csv|pdf"}`. It builds the report, renders it, and
stores the file(s) with `ArtifactStore` (`kind="report"`, `owner_type="report"`). It then sets the
`ReportRecord` to `SUCCEEDED` with its `artifact_id`. If anything fails, the record is set to
`FAILED` with the error. The worker can kill a job (timeout, cancellation, crash) before the
handler records anything; in that case, reading or listing the report reconciles it with the
terminal job status and marks it `FAILED`.

| Method | Path | Result |
|---|---|---|
| POST | `/api/audits/{audit_id}/reports` | Body `{"formats": ["html", "md", "json", "csv", "pdf"]}`. Returns 202 with one `{report_id, job_id, format, status}` per format. 404 if the audit is unknown, 409 if the audit is not `SUCCEEDED`/`PARTIAL`, 422 for unknown formats. |
| GET | `/api/audits/{audit_id}/reports` | All reports for the audit, newest first |
| GET | `/api/reports/{report_id}` | Status, error, files (role, name, size, sha256), meta (engine, redactions, schema version) |
| GET | `/api/reports/{report_id}/download` | The file with its media type and a `Content-Disposition` filename. `?part=benchmark_samples` returns the samples CSV (audit reports); `?part=validation_checks` returns the validation-checks CSV of an experiment report. 409 until the report succeeds. Works for every report subject (audit, optimization, benchmark). |
| GET | `/api/reports/{report_id}/preview` | HTML or Markdown source as `text/plain` (never served as active HTML). 422 for other formats. |

Requests are idempotent per audit, format and audit version: the job idempotency key is
`report:<audit_id>:<format>:<audit updated_at>`. Repeating a request returns the same report and
job. If the audit changes, a new report is created. If the previous attempt failed, was cancelled
or timed out, a new attempt is queued under a suffixed key.

## Adding a report provider

The measured sections (7, 11, 12) are filled by providers. A provider is a module in
`backend/reporting/providers/` that is discovered automatically. PerfPilot ships one,
`runtime-audit` (`backend/reporting/providers/runtime.py`): it fills section 7 from successful
sandbox profile runs (hotspot table, wall/CPU time, peak RSS and tracemalloc peak labelled
separately, environment, exact command and limits) and lists failed runs as limitations without
numbers. See [runtime-audit.md](runtime-audit.md). The shape of a provider:

```python
# backend/reporting/providers/<name>.py
from sqlalchemy.orm import Session

from backend.reporting.model import RuntimeProfile
from backend.reporting.provider_registry import (
    ProviderContext,
    ProviderContribution,
    report_provider,
)


@report_provider("runtime-audit")
def runtime_audit(session: Session, ctx: ProviderContext) -> ProviderContribution | None:
    profiles = ...  # query this audit's measured runs using ctx.audit_id
    if not profiles:
        return None  # nothing measured: the section keeps saying "Not measured"
    return ProviderContribution(runtime_profiles=[RuntimeProfile(...) for p in profiles])
```

- `ProviderContext` contains `audit_id`, `snapshot_id`, `repository_id`, `commit_sha` and the
  audit's prioritized findings, so a provider can link hotspots to finding ids.
- A contribution can contain `runtime_profiles`, `validation_outcomes`, `benchmark_results` and
  extra `limitations`. Providers run in name order and their lists are concatenated. The names of
  providers that contributed are listed in `metadata.providers_applied`.
- Only return data from real runs. The measured models fix `provenance` to `MEASURED`.
- Measured evidence on individual findings (`Finding.measured`) is read from the finding rows
  themselves and needs no provider.

## Dependency graph image

`graph_svg.render_dependency_graph` writes a standalone SVG without network access or external
renderers:

- Layout: a networkx spring layout with a fixed seed over a sorted node order, with coordinates
  rounded. The same graph always produces the same SVG.
- Visual encoding: node size shows fan-in, fill color shows finding count (0, 1–2, 3–5, 6+),
  entrypoints have a blue outline, import cycles have red outlines and edges, and reciprocal
  imports are drawn offset.
- Large graphs are capped at 40 modules. Entrypoints and cycle members are kept first (using up to
  half the slots), and the rest are ranked by fan-in, fan-out and findings. A note such as
  "Showing 40 of 312 modules" appears on the image and in section 15.
- Modules with no edges among the shown nodes are placed in a row at the bottom, so they do not
  distort the layout.

## Experiment reports (workload deep dive)

An *experiment report* is the engineering write-up of one workload optimization experiment, meant
to be attached to a GitHub issue or pull request. Its subject is either one
`OptimizationCandidate` (profile → recommendation → patch → approval → validation → benchmark) or
one benchmark of a workload's bundled reference implementation (labeled as such: hand-written for
tests, not platform output).

```
DB rows ──► experiment_builder ──► ExperimentReport ──► redact ──► renderers/experiment*.py ──► redact ──► artifact
```

Code: `backend/reporting/experiment_model.py` (contract, `schema_version` 1.0),
`experiment_builder.py` (reads stored rows only; nothing is re-run), `experiment_charts.py`
(inline SVG box/strip plot), `renderers/experiment*.py`, `templates/experiment.html.j2`,
`experiment_service.py` (requests + the `report.experiment` job),
`backend/api/routes/experiments.py`, `backend/services/experiments.py` (history).

### Content

| Section | Provenance | Content |
|---|---|---|
| 1. Outcome: measured vs hypothesis | mixed, each row labeled | Headline and reasons, then the summary box: measured rows (profiled share, validation result, medians, speedup + CI, latency reduction, CPU speedup or why unavailable, classification, tracemalloc and RSS medians) next to the hypotheses (root cause, expected effect, estimated impact, memory trade-off) and a verdict (confirmed / contradicted / not confirmed / untested) |
| 2. Workload & input | | Workload, spec, fixture description, size parameters, input SHA-256 (benchmark preflight, else profile), output contract, limits, registered tree hash |
| 3. Environment fingerprint | `MEASURED` | From the benchmark run (else the profile). The interpreter path is omitted (host layout) |
| 4. Profiling evidence | `MEASURED` | Top 10 hotspots by exclusive time with exclusive vs cumulative definitions, profiler-overhead note, and the profile's tracemalloc / RSS / ru_maxrss with their definitions |
| 5. Recommendation | per field | Observed bottleneck (`MEASURED` + rule), profiler evidence (`MEASURED`), root cause, proposed optimization, expected effect and impact (qualitative), correctness risks, memory trade-offs, validation plan |
| 6. Proposed change | | Unified diff, original/patched file and tree SHA-256, `generated_by`, transform, patch-validator checks and violations |
| 7. Approval & lifecycle | | Approval/rejection record and the full state history |
| 8. Correctness validation | `MEASURED` | Status (`PASSED`/`FAILED`/`INCONCLUSIVE`/`NOT_RUN`), required checks, per-check detail and failures |
| 9. Benchmark results | `MEASURED` | Settings (warmups, runs, memory runs, seed, order, resamples, threshold), per-variant n/median/mean/min/max/stdev/p95/CV with the small-n warning, speedup, bootstrap CI, latency reduction, throughput speedup, CPU speedup, memory comparison (tracemalloc, RSS and ru_maxrss rows, never compared with each other), classification + thresholds, warnings, box/strip plot and raw timing samples |
| 10. Reproducibility | | `scripts/optimize_workload.py …` (or `scripts/run_workload.py benchmark …`) and the exact API calls with this experiment's IDs and settings |
| 11. Limitations | | Host execution, validation scope, memory definitions, profiler overhead, small sample sizes |

Expected effect and estimated impact are predictions, so they carry the label the analysis
recorded for them (`AI_HYPOTHESIS`, for rule templates and model output alike) and the same label
the recommendations API returns; a hypothesis is never labeled `MEASURED`.
Sections that did not run are `null` with a reason. A candidate that **failed validation** shows
the failing checks and `benchmark: null` with "No benchmark ran: correctness validation FAILED…",
so the report contains no performance numbers for it. A candidate **rejected** by the patch
validator or a reviewer shows the rejection reasons and neither validation nor benchmark.

### Formats

| Format | Files |
|---|---|
| `md` | GitHub-flavored Markdown (PR-ready; diff in a `diff` fence, raw samples in `<details>`) |
| `json` | The model dump with `schema_version` |
| `html` | Same template language and stylesheets as the audit report; the box/strip plot is inline SVG (no network). Log scale when max/min > 20 |
| `pdf` | The print variant of the HTML through the same engines (WeasyPrint, else xhtml2pdf) |
| `csv` | `primary`: every raw benchmark sample (preflight, timing, memory; header only if no benchmark ran). `validation_checks`: one row per validation check |

The same two-pass redaction and CSV formula-injection guard as audit reports apply.

### Jobs and API

| Method | Path | Result |
|---|---|---|
| POST | `/api/optimizations/{candidate_id}/reports` | Body `{"formats": [...]}`. 202 with `{subject_type, subject_id, reports: [{report_id, job_id, format, status, report_status}]}`. 404 unknown candidate, 409 while the candidate is `VALIDATING`/`BENCHMARKING`, 422 bad formats |
| GET | `/api/optimizations/{candidate_id}/reports` | The candidate's reports, newest first (`ReportOut` with `subject_type`/`subject_id`) |
| POST | `/api/benchmarks/{benchmark_id}/reports` | Same, for a finished (`SUCCEEDED`/`FAILED`) reference benchmark. 409 if it is still running, or if it benchmarks a candidate (the error names the candidate to report on instead) |
| GET | `/api/benchmarks/{benchmark_id}/reports` | The benchmark's reports |
| GET | `/api/experiments?limit=&workload_id=&kind=optimization\|reference_benchmark` | History for the Reports & History page: recent candidates and reference benchmarks with state, validation status, classification, speedup + CI, medians, report count, `created_at` |

Metadata, downloads and previews use the generic `/api/reports/{report_id}`, `/download` and
`/preview` endpoints. Records reuse `ReportRecord` with `subject_type` `optimization` or
`benchmark` (`audit_id` is `null`). The `report.experiment` job
(`backend/jobs/handlers/experiment_reports.py`) takes
`{"subject_type", "subject_id", "format"}`; its idempotency key is
`experiment-report:<subject_type>:<id>:<format>:<subject updated_at>`, so repeating a request
returns the same report until the candidate changes state, and failed attempts are retried under a
suffixed key, exactly like audit reports.

`tests/e2e/test_workload_deep_dive.py` drives the whole deep dive for `duplicate_counter` and
`nested_loop` through the API and the isolated worker, exports every format, then disposes the
engine and starts a fresh app on the same data directory to check that the candidate, benchmark
samples and report files (byte-identical, matching the stored SHA-256) survive a restart.
