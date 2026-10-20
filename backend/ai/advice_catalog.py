"""Per-rule advice used by the deterministic advisor: strategy family, wording, base effort.

Texts are qualitative on purpose: static analysis cannot know input sizes or call frequency, so no
expected effect here ever contains a number.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.models.enums import EffortEstimate


@dataclass(frozen=True)
class RuleAdvice:
    kind: str
    title: str
    strategy: str
    expected_effect: str
    risks: str
    effort: EffortEstimate
    group_by_module: bool = False


_GENERIC = RuleAdvice(
    kind="review-pattern",
    title="Review a flagged performance pattern",
    strategy="Review the flagged code and apply the rule's suggestion where the data can grow.",
    expected_effect="Depends on how often this code runs and on input sizes; profile first.",
    risks="Check that the rewrite keeps the observable behavior.",
    effort=EffortEstimate.MEDIUM,
)

ADVICE: dict[str, RuleAdvice] = {
    "PP-LOOP-001": RuleAdvice(
        kind="set-membership",
        title="Use a set for repeated membership tests",
        strategy="Replace repeated linear scans of a list with hash lookups.",
        expected_effect=(
            "Each membership test no longer scans the whole list, so the loop scales with the "
            "number of iterations instead of iterations times list length; negligible for short "
            "lists."
        ),
        risks="Elements must be hashable, and a set does not preserve order or duplicates.",
        effort=EffortEstimate.SMALL,
    ),
    "PP-LOOP-002": RuleAdvice(
        kind="index-join",
        title="Replace a nested-loop join with a dict index",
        strategy="Index the inner collection by the join key once, then look items up.",
        expected_effect=(
            "Turns a quadratic pairwise comparison into a single pass plus lookups; the benefit "
            "grows with both collection sizes."
        ),
        risks=(
            "Keys must be hashable; non-unique keys need a dict of lists, and first-match order "
            "must be preserved if the original loop stopped at the first match."
        ),
        effort=EffortEstimate.MEDIUM,
    ),
    "PP-LOOP-003": RuleAdvice(
        kind="hoist-invariant",
        title="Hoist loop-invariant work out of the loop",
        strategy="Compute the value once before the loop and reuse it.",
        expected_effect="The repeated work runs once instead of once per iteration.",
        risks=(
            "Only valid while every input stays unchanged during the loop; aliasing through other "
            "functions or I/O can make an invariant-looking value change."
        ),
        effort=EffortEstimate.SMALL,
    ),
    "PP-LOOP-004": RuleAdvice(
        kind="incremental-ordering",
        title="Avoid re-sorting a growing collection",
        strategy="Sort once after the loop, or keep the collection ordered incrementally.",
        expected_effect="Avoids sorting the whole collection on every iteration.",
        risks="Sort stability and key functions must stay identical.",
        effort=EffortEstimate.SMALL,
    ),
    "PP-STR-001": RuleAdvice(
        kind="string-join",
        title="Build strings with a list and join",
        strategy="Collect the pieces in a list and join them once after the loop.",
        expected_effect=(
            "Avoids repeatedly copying the growing string; CPython sometimes resizes in place, so "
            "the benefit varies and should be measured."
        ),
        risks=(
            "If the loop can raise and the partially built string is used afterwards (for example "
            "in an exception handler), that partial value is no longer available."
        ),
        effort=EffortEstimate.SMALL,
    ),
    "PP-REGEX-001": RuleAdvice(
        kind="precompile-regex",
        title="Compile constant regular expressions once",
        strategy="Move constant `re.compile` calls to module level and reuse the compiled pattern.",
        expected_effect=(
            "Removes per-iteration compilation and cache lookups; the `re` module caches recent "
            "patterns, so the effect is usually small."
        ),
        risks="The module-level constant is created at import time.",
        effort=EffortEstimate.SMALL,
    ),
    "PP-COLL-001": RuleAdvice(
        kind="index-lookup",
        title="Replace list.index/count/remove in a loop with a lookup structure",
        strategy="Maintain a dict (value -> position/count) or a set alongside the list.",
        expected_effect="Each lookup stops scanning the list from the start.",
        risks="The auxiliary structure must be kept in sync with every list mutation.",
        effort=EffortEstimate.SMALL,
    ),
    "PP-COLL-002": RuleAdvice(
        kind="deque",
        title="Use collections.deque for queue operations",
        strategy="Use a deque so removals and insertions at the front do not shift the list.",
        expected_effect="Front operations stop moving every remaining element.",
        risks="A deque does not support slicing; indexing in the middle stays linear.",
        effort=EffortEstimate.SMALL,
    ),
    "PP-COLL-003": RuleAdvice(
        kind="avoid-materialization",
        title="Avoid materializing temporary lists in a loop",
        strategy="Use views and iterators directly instead of building throwaway lists.",
        expected_effect="Avoids allocating and copying a temporary list on every iteration.",
        risks="Iterating a dict view while mutating the dict raises; keep copies where needed.",
        effort=EffortEstimate.SMALL,
    ),
    "PP-DB-001": RuleAdvice(
        kind="batch-db-queries",
        title="Batch database queries issued inside loops (N+1)",
        strategy=(
            "Fetch everything the loop needs with one query (IN-list, eager loading, or a join) "
            "before the loop, index the rows in a dict, and batch writes."
        ),
        expected_effect=(
            "Replaces one database round trip per item with a constant number of round trips; the "
            "benefit grows with the number of items and with database latency."
        ),
        risks=(
            "Large IN-lists may need chunking; eager loading can fetch more columns than needed; "
            "transaction boundaries and per-row error handling may change."
        ),
        effort=EffortEstimate.MEDIUM,
        group_by_module=True,
    ),
    "PP-ASYNC-001": RuleAdvice(
        kind="unblock-event-loop",
        title="Stop blocking the event loop in async code",
        strategy=(
            "Use async equivalents or offload the blocking call with `asyncio.to_thread`; for "
            "synchronous database sessions, choose one async/sync boundary per endpoint."
        ),
        expected_effect=(
            "Other requests and timers keep making progress while the slow operation runs; the "
            "effect shows up as better tail latency under concurrent load."
        ),
        risks=(
            "Offloaded code must be thread-safe; async libraries differ in timeouts, exceptions "
            "and session lifecycles."
        ),
        effort=EffortEstimate.MEDIUM,
        group_by_module=True,
    ),
    "PP-NET-001": RuleAdvice(
        kind="concurrent-http",
        title="Avoid sequential HTTP requests in a loop",
        strategy=(
            "Use a batch endpoint, or issue requests concurrently with a bound and one client."
        ),
        expected_effect="Network waits overlap instead of adding up one after another.",
        risks="Respect remote rate limits; ordering and partial-failure handling change.",
        effort=EffortEstimate.MEDIUM,
        group_by_module=True,
    ),
    "PP-NET-002": RuleAdvice(
        kind="shared-http-client",
        title="Reuse one HTTP client instead of creating one per call",
        strategy="Create the client once (startup, lifespan, dependency) and reuse it.",
        expected_effect="Connections and TLS sessions get pooled instead of re-established.",
        risks=(
            "A shared client must be closed on shutdown; per-call settings (headers, timeouts, "
            "auth) must move to request arguments."
        ),
        effort=EffortEstimate.SMALL,
        group_by_module=True,
    ),
    "PP-ALLOC-001": RuleAdvice(
        kind="avoid-deepcopy",
        title="Avoid deep-copying the same object in a loop",
        strategy="Copy only what is mutated, or copy once outside the loop.",
        expected_effect="Avoids walking and duplicating the whole object graph every iteration.",
        risks="Shallow copies share nested objects; mutations may leak between iterations.",
        effort=EffortEstimate.MEDIUM,
    ),
    "PP-ALLOC-002": RuleAdvice(
        kind="hoist-constant",
        title="Define large constant literals once",
        strategy="Move the literal to a module-level constant (tuple/frozenset if read-only).",
        expected_effect="The literal is built once instead of once per iteration.",
        risks="A shared mutable constant must never be modified.",
        effort=EffortEstimate.SMALL,
    ),
    "PP-ALLOC-003": RuleAdvice(
        kind="dataframe-batch-build",
        title="Build DataFrames once instead of growing them in a loop",
        strategy="Collect rows or partial frames in a list and build or concat once.",
        expected_effect="Avoids copying the accumulated frame on every iteration.",
        risks="Column dtypes and index handling may differ when built in one step.",
        effort=EffortEstimate.SMALL,
    ),
    "PP-ALLOC-004": RuleAdvice(
        kind="vectorize-dataframe",
        title="Replace DataFrame.iterrows with vectorized operations",
        strategy="Use column operations, or itertuples when a Python loop is unavoidable.",
        expected_effect="Moves per-row Python work into vectorized code.",
        risks="Vectorized expressions can change dtype coercion and NaN handling.",
        effort=EffortEstimate.MEDIUM,
    ),
    "PP-CACHE-001": RuleAdvice(
        kind="memoize-recursion",
        title="Memoize overlapping recursion",
        strategy="Cache results of the pure recursive function, or rewrite it iteratively.",
        expected_effect=(
            "Repeated sub-problems are computed once, which can change the growth rate of the "
            "recursion."
        ),
        risks=(
            "Arguments must be hashable; an unbounded cache keeps every result alive for the "
            "process lifetime; the function must stay pure."
        ),
        effort=EffortEstimate.SMALL,
    ),
    "PP-CACHE-002": RuleAdvice(
        kind="reuse-repeated-call",
        title="Reuse the result of an expensive call repeated with the same arguments",
        strategy="Call it once and reuse the result, or cache it when it is stable.",
        expected_effect="Identical expensive work runs once.",
        risks="Cached results can go stale if the underlying source changes.",
        effort=EffortEstimate.SMALL,
    ),
    "PP-ARCH-001": RuleAdvice(
        kind="defer-import-time-work",
        title="Move heavy work out of import time",
        strategy="Defer the work to a function, a startup hook, or a lazily initialized value.",
        expected_effect="Imports (and therefore startup and test collection) stop paying for it.",
        risks="Errors surface later (first use instead of import); initialization order changes.",
        effort=EffortEstimate.MEDIUM,
    ),
    "PP-ARCH-002": RuleAdvice(
        kind="bounded-cache",
        title="Bound module-level caches",
        strategy="Use an LRU/TTL cache or explicit eviction.",
        expected_effect="Memory stops growing with every distinct key seen by the process.",
        risks="Evicted entries are recomputed; TTLs change freshness guarantees.",
        effort=EffortEstimate.SMALL,
    ),
}


def advice_for(rule_id: str) -> RuleAdvice:
    return ADVICE.get(rule_id, _GENERIC)
