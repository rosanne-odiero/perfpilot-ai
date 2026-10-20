"""Citation guard (invented locations, wrong symbols, runtime-number claims) and token budgets."""

from __future__ import annotations

import pytest

from backend.ai.budget import BudgetExhaustedError, TokenBudget, estimate_tokens
from backend.ai.citation_guard import CitationGuard, contains_runtime_claim
from backend.ai.schemas import Citation, InsightDraft, RecommendationDraft
from backend.models.audit_schemas import CodeIndex, ModuleInfo, SymbolInfo
from backend.models.enums import EffortEstimate, Provenance


@pytest.fixture
def guard() -> CitationGuard:
    module = ModuleInfo(
        module_name="app.orders",
        file_path="app/orders.py",
        line_count=40,
        symbols=[
            SymbolInfo(
                qualname="load_orders",
                kind="function",
                file_path="app/orders.py",
                line_start=5,
                line_end=15,
            ),
            SymbolInfo(
                qualname="Repo.fetch",
                kind="method",
                file_path="app/orders.py",
                line_start=20,
                line_end=30,
            ),
        ],
    )
    return CitationGuard(CodeIndex(modules=[module]), finding_ids=["f1", "f2"])


def _draft(**overrides: object) -> RecommendationDraft:
    values: dict[str, object] = {
        "title": "Batch the order queries",
        "summary": "load_orders issues one query per order.",
        "rationale": "PP-DB-001 flags a query inside the loop.",
        "proposed_strategy": "Fetch all orders with one IN query before the loop.",
        "finding_ids": ["f1", "invented"],
        "citations": [Citation(file_path="app/orders.py", line_start=8, line_end=9)],
        "effort": EffortEstimate.MEDIUM,
        "uncertainty": "Input sizes are unknown.",
        "expected_effect": "Removes one database round trip per order.",
        "correctness_risks": "Large IN lists may need chunking.",
    }
    values.update(overrides)
    return RecommendationDraft.model_validate(values)


@pytest.mark.parametrize(
    ("citation", "valid"),
    [
        (Citation(file_path="app/orders.py", line_start=5, line_end=15), True),
        (Citation(file_path="./app/orders.py", line_start=1, line_end=40), True),
        (Citation(file_path="app/orders.py", line_start=6, line_end=7, symbol="load_orders"), True),
        (Citation(file_path="app/orders.py", line_start=21, line_end=22, symbol="fetch"), True),
        (
            Citation(
                file_path="app/orders.py",
                line_start=21,
                line_end=22,
                symbol="app.orders:Repo.fetch",
            ),
            True,
        ),
        (Citation(file_path="app/payments.py", line_start=1, line_end=2), False),
        (Citation(file_path="app/orders.py", line_start=39, line_end=41), False),
        (Citation(file_path="app/orders.py", line_start=10, line_end=5), False),
        (
            Citation(file_path="app/orders.py", line_start=6, line_end=7, symbol="save_orders"),
            False,
        ),
        (
            Citation(file_path="app/orders.py", line_start=25, line_end=26, symbol="load_orders"),
            False,
        ),
        (Citation(file_path="../app/orders.py", line_start=5, line_end=6), False),
        (Citation(file_path="/app/orders.py", line_start=5, line_end=6), False),
        (Citation(file_path="app\\orders.py", line_start=5, line_end=6), False),
    ],
)
def test_citation_validation(guard: CitationGuard, citation: Citation, valid: bool) -> None:
    location = guard.location(citation)
    assert (location is not None) is valid
    assert guard.stats.rejected_citations == (0 if valid else 1)
    if location is not None:
        assert location.file_path == "app/orders.py"


@pytest.mark.parametrize(
    "text",
    [
        "This saves about 120ms per request.",
        "Expect a 3x speedup.",
        "It becomes 10 times faster.",
        "Latency drops by 40%.",
        "Uses 512 MB less memory.",
        "The loop takes 2.5 seconds.",
        "Throughput rises to 900 req/s.",
        "A 10-fold improvement.",
        "Saves 3s on startup.",
    ],
)
def test_runtime_claims_are_detected(text: str) -> None:
    assert contains_runtime_claim(text)


@pytest.mark.parametrize(
    "text",
    [
        "Removes one database round trip per item (N+1 to 1 query).",
        "Lines 12-14 call the client 3 times per order.",
        "Python 3.12 caches compiled patterns.",
        "Turns O(n^2) into O(n).",
        "Use a timeout argument and 3 sessions per worker.",
        "Effort is SMALL; see PP-DB-001 at line 42.",
    ],
)
def test_qualitative_text_is_kept(text: str) -> None:
    assert not contains_runtime_claim(text)


def test_valid_recommendation_is_accepted_and_labeled(guard: CitationGuard) -> None:
    rec = guard.recommendation(_draft(), "gpt-test")
    assert rec is not None
    assert rec.provenance is Provenance.AI_HYPOTHESIS
    assert rec.generated_by == "gpt-test"
    assert rec.finding_ids == ["f1"]
    assert rec.suggested_diff is None
    assert [(c.file_path, c.line_start) for c in rec.citations] == [("app/orders.py", 8)]


def test_hallucinated_citations_reject_the_recommendation(guard: CitationGuard) -> None:
    draft = _draft(
        citations=[
            Citation(file_path="app/payments.py", line_start=1, line_end=3),
            Citation(file_path="app/orders.py", line_start=100, line_end=101),
        ]
    )
    assert guard.recommendation(draft, "m") is None
    assert guard.stats.rejected_citations == 2
    assert guard.stats.rejected_items == 1


def test_numeric_claims_are_stripped_or_reject(guard: CitationGuard) -> None:
    rec = guard.recommendation(
        _draft(
            expected_effect="Removes one round trip per order. This makes it 5x faster.",
            rationale="Measured 300 ms in production. The loop queries per order.",
        ),
        "m",
    )
    assert rec is not None
    assert "5x" not in rec.expected_effect
    assert "round trip" in rec.expected_effect
    assert "300 ms" not in rec.rationale
    assert guard.stats.rejected_claims == 2
    only_numbers = _draft(expected_effect="Saves 250ms.")
    assert guard.recommendation(only_numbers, "m") is None
    assert guard.stats.rejected_items == 1


def test_insight_requires_real_modules_or_citations(guard: CitationGuard) -> None:
    good = InsightDraft(
        title="Orders N+1", description="Orders load per item.", modules=["app.orders"]
    )
    bad = InsightDraft(title="Payments", description="Payments are slow.", modules=["app.payments"])
    assert guard.insight(good, "m") is not None
    assert guard.insight(bad, "m") is None
    assert guard.stats.rejected_citations == 1


def test_units(guard: CitationGuard) -> None:
    assert guard.units(["app.orders:load_orders", "app.orders:nope"]) == ["app.orders:load_orders"]
    assert guard.stats.rejected_citations == 1


def test_budget_plans_and_exhausts() -> None:
    budget = TokenBudget(total=2_000, per_call=1_200)
    assert budget.plan_call(400, 600) == 600
    assert budget.plan_call(400, 2_000) == 800  # capped by the per-call limit
    budget.record(400, 800)
    assert budget.remaining == 800
    with pytest.raises(BudgetExhaustedError) as exc:
        budget.plan_call(700, 600)
    assert exc.value.scope == "audit"
    assert budget.exhausted
    with pytest.raises(BudgetExhaustedError):
        budget.plan_call(10, 300)  # stays exhausted


def test_budget_rejects_oversized_single_calls_without_exhausting() -> None:
    budget = TokenBudget(total=100_000, per_call=1_000)
    with pytest.raises(BudgetExhaustedError) as exc:
        budget.plan_call(900, 500)
    assert exc.value.scope == "call"
    assert not budget.exhausted
    assert budget.context_allowance(reserved_output=300, overhead=200) == 500
    assert estimate_tokens("x" * 9) == 3
