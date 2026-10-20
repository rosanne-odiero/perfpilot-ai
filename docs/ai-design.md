# AI-assisted analysis (advisor)

The `AI_ANALYSIS` audit stage runs after `PRIORITIZING`. It turns prioritized static findings into
recommendations and architecture insights. The **deterministic advisor always runs** and needs no
API key or network. An optional **LLM pipeline** adds `AI_HYPOTHESIS` recommendations and
insights, but only when the operator configured an endpoint *and* the user asked for it *and*
consented to sending redacted snippets.

```
findings + CodeIndex ──► DeterministicAdvisor ──► Recommendation[] (STATIC) + ArchitectureInsight[] (STATIC)
          │                                                │
          └─(configured AND use_llm AND llm_consent)──► HierarchicalPipeline ──► CitationGuard ──► AI_HYPOTHESIS items
                                                           │                                         │
                                                           └──────────────── merged by priority ◄────┘
```

Code: `backend/ai/` (`analysis.py` orchestrates), stage wiring in `backend/audit/ai_stage.py`,
persistence in `backend/services/recommendations.py`.

## Deterministic advisor (`deterministic_advisor.py`)

* **Grouping.** One recommendation per change. The same rule in the same function forms one group.
  For N+1 queries, blocking calls in coroutines, and HTTP clients (`PP-DB-001`, `PP-ASYNC-001`,
  `PP-NET-001`, `PP-NET-002`), the grouping is per module instead, so one router with three N+1
  handlers gets one recommendation that cites all three. Synchronous database sessions inside
  `async def` are a separate variant (`kind = async-db-boundary`).
* **Ranking.** `group score = max(priority) + 0.1 × sum(other priorities)`. `priority_rank` is
  1..N by score. Priorities come from the static audit (see
  [static-analysis.md](static-analysis.md), including the `path_role` factor).
* **Text.** Each recommendation carries a strategy (rule-family framing plus the findings'
  concrete suggestions), a rationale (static evidence, complexity estimate, code role, execution
  path), an uncertainty statement, a qualitative expected effect (never a number), and correctness
  risks (rule risks plus the findings' trade-offs). Code in seed, test, example, or CLI paths gets
  an explicit "benefit probably limited to that workflow" qualifier.
* **Effort.** The rule's base effort is raised one level for 3 or more functions and one more
  level for 3 or more files (SMALL → MEDIUM → LARGE). Effort is a size, not a time estimate.
* **Citations.** Citations are always the findings' own `CodeLocation`s, so they are real by
  construction.
* **Insights** (`STATIC`):
  * "Introduce a shared HTTP client": per-call clients in at least 2 modules.
  * "Synchronous database access inside async code": at least 2 coroutines.
  * "Blocking calls on the event loop": at least 3 coroutines.
  * "N+1 query pattern recurs across modules": at least 2 modules.
  * High fan-in hot modules: fan-in at or above max(3, p90) with at least 2 findings; top 3 only.
  * "Findings outside request paths were deprioritized".

  The cross-module insights ignore seed, test, example, and CLI code.
* **Provenance.** Everything here is `STATIC` with `generated_by = "deterministic"`. It is
  derived from rules, not guessed.

### Suggested diffs (`rewrites.py`, `diffs.py`)

A diff is attached only when a rewrite is mechanical and provably local:

| Rule | Rewrite | Preconditions (otherwise no diff) |
|---|---|---|
| `PP-REGEX-001` | hoist `re.compile(<const>)` to a module constant | top-level `import re`, constant pattern/flags, single-line call, no keywords |
| `PP-LOOP-001` | `x in [consts]` → `x in {consts}` | inline literal of hashable constants, or a local literal list used *only* for membership tests (not mutated, passed, returned, or captured) |
| `PP-STR-001` | `s = ""` … `s += piece` → `s_parts = []` … `s_parts.append(piece)` … `s = "".join(s_parts)` | constant-string init directly before the loop in the same block, no other use of `s` in the loop, no `else` on the loop |
| `PP-CACHE-001` | add `@functools.lru_cache(maxsize=None)` | undecorated, top-level, synchronous function; no `*args`/`**kwargs`; no list/dict/set-annotated parameters |

Edits are computed against the original AST and real lines. The diff is generated with `difflib`
from the exact indexed text. It is published only if all of these hold:

1. it applies cleanly with the strict applier (`apply_unified_diff`);
2. the result still parses;
3. the file on disk is byte-identical to the indexed text (no CRLF).

Tests apply the diffs with `patch -p1` as well and execute the original and patched functions to
compare their results.

## LLM pipeline (`summarizer.py`)

Five structured-output steps. Each validates the reply with a Pydantic model in `schemas.py`.

| Step | Input (selective) | Output schema | Used for |
|---|---|---|---|
| 1. Unit summaries | functions with findings or on their execution paths (max 16; 4 per call) | `UnitSummaries` | context for later steps; cached |
| 2. Dependency context | import-graph neighborhoods of those modules (max 10) | `DependencyContext` | context |
| 3. Likely hot paths | static execution paths of top findings (max 8) | `HotPaths` | "Likely hot path" insights + context |
| 4. Cross-module correlation | one line per finding group (max 30) | `Correlations` | AI insights (max 5) |
| 5. Recommendations | top 8 finding groups with retrieved source (4 per call) | `RecommendationDrafts` | AI recommendations |

* **Retrieval** (`retrieval.py`). Only the enclosing function of each finding is sent, with line
  numbers, plus a short neighborhood summary (fan-in/out, entry points, imports, importers).
  Functions over 80 lines are cut to windows around the flagged lines. Blocks are added in
  priority order until the per-call context allowance is used up. The repository is never sent
  wholesale.
* **Caching.** Unit summaries are cached on disk at `<data_dir>/ai_cache/summaries/`. The key is
  sha256(model, prompt versions, file sha256, unit id), so they are reused until the file or a
  prompt changes.
* **Malformed output.** The step is retried once with the validation error (never the model's
  text). After that, the step uses its deterministic substitute and counts a fallback (step
  status `fallback`).
* **Merging.** AI recommendations are interleaved with deterministic ones. The score is the best
  priority among the findings they address or cite; ties go to deterministic.
  `generated_by = <model id>`, `provenance = AI_HYPOTHESIS`, `kind = ai-hypothesis`. They never
  carry a diff.

### Budgets (`budget.py`)

`PERFPILOT_LLM_MAX_TOKENS_PER_AUDIT` (default 60,000) and `PERFPILOT_LLM_MAX_TOKENS_PER_CALL`
(input + output, default 6,000). Before a call, input tokens are estimated at about 4 characters
per token. After a call, the provider's reported `usage` is recorded (or estimated and flagged).

* If a call would not fit the remaining audit budget, it is **not sent**. `budget_exhausted` is
  set, the remaining steps are `skipped`, and a warning is added to the audit.
* If a single request exceeds the per-call cap, only that call is skipped
  (`skipped_call_too_large`).

### Citation guard (`citation_guard.py`)

* A cited file must be an indexed module. Sensitive files are never indexed. The path must be
  relative with no `..`.
* `1 ≤ line_start ≤ line_end ≤ line_count`.
* A cited symbol must exist in that file and overlap the line range.
* Unit ids, module names, and finding ids must exist.

Invalid citations are dropped and counted (`rejected_citations`). Sentences claiming runtime
numbers (ms, seconds, MB, %, "3x", "10 times faster", req/s, …) are stripped and counted
(`rejected_claims`), because the model never ran the code. An item whose required text or every
citation disappears is rejected (`rejected_items`).

### Prompts (`prompts/*.md`)

The prompts are versioned with a `<!-- perfpilot-prompt name=… version=N -->` header and loaded by
`prompt_loader.py`. The combined version is recorded in `LLMUsage.prompt_version`. The system
prompt tells the model to:

* act as a senior Python performance engineer;
* treat static findings and profiler data as evidence and its own ideas as hypotheses;
* cite only the locations provided;
* never state runtime numbers;
* prefer localized, reviewable changes;
* state its uncertainty and the correctness risks.

## Consent, redaction, and what is sent

* **Gate.** `settings.llm_configured AND audit.use_llm AND audit.llm_consent`. The API already
  rejects `use_llm` without `llm_consent` (422). Without the gate, no client is created and no
  HTTP request is made. `stage_status.llm` records `not_requested`, `no_consent`, or
  `not_configured`. When the LLM was requested but is not configured, the audit gets a warning.
* **Redaction.** `backend.core.redaction.redact_text` runs on every retrieved block, and the
  client runs it again on every message right before sending (`outbound.py`). Redaction counts
  are recorded in `LLMUsage.redactions`.
* **What is sent.** Function snippets around findings (redacted), finding descriptions, and
  module import summaries. Content of sensitive files (`.env`, keys, credential files) is never
  sent.
* **Logging.** Prompts, responses, and keys are never logged. Logs contain only model, token
  counts, attempt numbers, and failure classes.

## LLM patch proposer (`backend/optimization/llm_proposer.py`)

Separate from the audit pipeline, the same client can propose a code patch when the deterministic
templates find no safe one. Consent is per use: the audit's `llm_consent` for repository
candidates, `use_llm: true` on `POST /api/optimizations` for workload candidates. It sends only the
target function (dedented, redacted; refused outright if it contains secret-like text), the finding
and its measured evidence, under the token budget, with the versioned prompt
`prompts/patch_proposal.md` (`TASK_PROMPTS` in `prompt_loader.py`, outside the summary-cache
key). The answer must be a JSON `FunctionRewrite` replacing exactly that function; it is spliced
into the real file, compiled and passed through the patch validator, with one bounded retry.
Results are `AI_HYPOTHESIS` until validated and benchmarked. See
[repo-optimization.md](repo-optimization.md#7-llm-patch-proposer).

## Configuration

| Variable | Meaning |
|---|---|
| `PERFPILOT_LLM_BASE_URL` | OpenAI-compatible base URL (the client POSTs to `{base}/chat/completions`) |
| `PERFPILOT_LLM_MODEL` | Model or deployment name |
| `PERFPILOT_LLM_API_KEY` | Optional key (sent as `Authorization: Bearer`, or `api-key` for Azure hosts) |
| `PERFPILOT_LLM_TIMEOUT_SECONDS`, `PERFPILOT_LLM_MAX_RETRIES` | Per-request timeout; retries on 408/425/429/5xx/timeouts with exponential backoff (honors `Retry-After`, capped at 30 s) |
| `PERFPILOT_LLM_MAX_TOKENS_PER_AUDIT`, `PERFPILOT_LLM_MAX_TOKENS_PER_CALL` | Budgets |
| `PERFPILOT_LLM_INPUT_COST_PER_1K`, `PERFPILOT_LLM_OUTPUT_COST_PER_1K` | Used for `estimated_cost_usd` (0 = unknown or free) |

Examples:

```bash
# OpenAI
PERFPILOT_LLM_BASE_URL=https://api.openai.com/v1  PERFPILOT_LLM_MODEL=gpt-4o-mini  PERFPILOT_LLM_API_KEY=sk-...
# Azure OpenAI (v1-compatible endpoint; model = deployment name)
PERFPILOT_LLM_BASE_URL=https://<resource>.openai.azure.com/openai/v1  PERFPILOT_LLM_MODEL=<deployment>  PERFPILOT_LLM_API_KEY=<key>
# Ollama (local; no key)
PERFPILOT_LLM_BASE_URL=http://localhost:11434/v1  PERFPILOT_LLM_MODEL=qwen2.5-coder:7b
```

The client asks for JSON mode (`response_format: json_object`). When a server rejects
`response_format`, `max_tokens` (newer OpenAI models want `max_completion_tokens`), or
`temperature`, the client adapts once per parameter and retries. The JSON instruction and the
schema are always in the prompt.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/audits/{id}/recommendations?provenance=` | Recommendations sorted by `priority_rank`, optionally only `STATIC` or `AI_HYPOTHESIS` |
| `GET /api/audits/{id}/insights` | `ArchitectureInsight[]` |
| `GET /api/audits/{id}/llm-usage` | `LLMUsage`: calls, tokens, cost, budget, fallbacks, rejected citations/claims/items, cache hits, redactions, prompt version, per-step status |
| `GET /api/ai/status` | Whether an LLM is configured, the model, the endpoint **host only**, whether a key is set, budgets, prompt version, and what would be sent |

`AuditRun.summary.advisor` holds the mode (`deterministic` or `deterministic+llm`), counts by
provenance, insight count, and suggested-diff count.

## Evaluation

`evals/recommendation_cases.json` holds 30 small scenarios (24 positive, 6 negative).
`evals/evaluation_runner.py` audits each one and reports detection and recommendation accuracy,
negative cases, diff validity, and citation validity in `evals/evaluation_report.md`. With
`--llm` it also runs the configured endpoint and reports calls, tokens, kept AI recommendations,
rejections, and fallbacks.

The committed report is a real deterministic run (30/30). The cases were written alongside the
rules, so they guard against regressions rather than serving as an independent benchmark. CI runs
the runner with a mocked LLM.

## Limitations

* Static evidence only: the advisor cannot see data sizes or call frequency. Expected effects are
  qualitative and must be confirmed by profiling.
* Execution paths come from a name-based call graph (see static-analysis.md). The model's
  "likely hot paths" are hypotheses.
* The citation guard proves that a location exists and overlaps the cited symbol. It cannot prove
  that the model's reasoning about that code is correct, so AI recommendations stay
  `AI_HYPOTHESIS`.
* Token counts before a call are heuristic (no tokenizer dependency). Providers that omit `usage`
  get estimated counts.
* Only four rewrites produce diffs in the advisor. Anything else is described but not patched.
  Applying, validating, and benchmarking patches is the patch-validation workflow
  ([optimization-workflow.md](optimization-workflow.md),
  [repo-optimization.md](repo-optimization.md)).
