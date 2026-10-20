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

## Jobs and API

The `report.generate` job (`backend/jobs/handlers/reports.py`) takes the payload
`{"audit_id": "...", "format": "html|md|json|csv|pdf"}`. It builds the report, renders it, and
stores the file(s) with `ArtifactStore` (`kind="report"`, `owner_type="report"`). It then sets the
`ReportRecord` to `SUCCEEDED` with its `artifact_id`. If anything fails, the record is set to
`FAILED` with the error.

| Method | Path | Result |
|---|---|---|
| POST | `/api/audits/{audit_id}/reports` | Body `{"formats": ["html", "md", "json", "csv", "pdf"]}`. Returns 202 with one `{report_id, job_id, format, status}` per format. 404 if the audit is unknown, 409 if the audit is not `SUCCEEDED`/`PARTIAL`, 422 for unknown formats. |
| GET | `/api/audits/{audit_id}/reports` | All reports for the audit, newest first |
| GET | `/api/reports/{report_id}` | Status, error, files (role, name, size, sha256), meta (engine, redactions, schema version) |
| GET | `/api/reports/{report_id}/download` | The file with its media type and a `Content-Disposition` filename. `?part=benchmark_samples` returns the samples CSV. 409 until the report succeeds. |
| GET | `/api/reports/{report_id}/preview` | HTML or Markdown source as `text/plain` (never served as active HTML). 422 for other formats. |

Requests are idempotent per audit, format and audit version: the job idempotency key is
`report:<audit_id>:<format>:<audit updated_at>`. Repeating a request returns the same report and
job. If the audit changes, a new report is created. If the previous attempt failed, was cancelled
or timed out, a new attempt is queued under a suffixed key.

## Adding a report provider

The measured sections (7, 11, 12) are filled by providers. A provider is a module in
`backend/reporting/providers/` that is discovered automatically. PerfPilot ships no providers; the
runtime-audit phase will add one that reads its profiling, validation and benchmark tables.

```python
# backend/reporting/providers/runtime_audit.py
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
