"""LLM patch proposer against a fake OpenAI-compatible endpoint (httpx.MockTransport only)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from llm_support import FakeLLM, client_factory, llm_settings

from backend.ai import llm_client
from backend.core.config import Settings
from backend.infrastructure.artifact_store import sha256_bytes
from backend.optimization.diffs import apply_file_patch, parse_unified_diff
from backend.optimization.llm_proposer import MAX_ATTEMPTS, TRANSFORM, LLMPatchProposer
from backend.optimization.patch_generator import (
    ChainedPatchProposer,
    DeterministicPatchProposer,
    PatchRequest,
    PatchTarget,
    SourceChangedError,
    select_patch_proposer,
)

SOURCE = '''"""Inventory helpers."""

from __future__ import annotations

API_NOTE = "unrelated module text that must never be sent"


def restock_order(events: list[str]) -> list[str]:
    queue = list(events)
    processed: list[str] = []
    while queue:
        sku = queue.pop(0)
        if sku in processed:
            continue
        processed.append(sku)
    return processed


class Store:
    def tally(self, items: list[int]) -> int:
        total = 0
        for item in items:
            total += items.count(item)
        return total
'''

GOOD_RESTOCK = """def restock_order(events: list[str]) -> list[str]:
    processed: list[str] = []
    seen: set[str] = set()
    for sku in events:
        if sku in seen:
            continue
        seen.add(sku)
        processed.append(sku)
    return processed
"""

ORIGINAL_RESTOCK = SOURCE.split("\n\n\n")[1]

GOOD_TALLY = """def tally(self, items: list[int]) -> int:
    counts: dict[int, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return sum(counts[item] for item in items)
"""

EVIDENCE = {"measured (MEASURED, profiler)": {"attributed_time_ms": 812.5, "call_count": 1}}


def _answer(replacement: str, summary: str = "use a set for membership") -> dict[str, Any]:
    return {"replacement": replacement, "summary": summary, "correctness_notes": ["order kept"]}


def _request(
    root: Path, function: str = "restock_order", lines: tuple[int, int] = (11, 13)
) -> PatchRequest:
    return PatchRequest(
        target=PatchTarget(
            source_root=root,
            target_file="pkg/inventory.py",
            target_function=function,
            line_start=lines[0],
            line_end=lines[1],
            rule_id="PP-COLL-002",
            expected_sha256=sha256_bytes(SOURCE.encode()),
        ),
        title="list.pop(0) in a loop",
        proposed_optimization="use collections.deque or iterate",
        correctness_risks=["order of processing"],
        evidence=EVIDENCE,
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "inventory.py").write_text(SOURCE)
    return tmp_path


def _proposer(settings: Settings, fake: FakeLLM, **overrides: Any) -> LLMPatchProposer:
    return LLMPatchProposer(
        llm_settings(settings, **overrides), client_factory=client_factory(fake)
    )


def _patched(root: Path, diff: str) -> str:
    [patch] = parse_unified_diff(diff)
    return apply_file_patch((root / "pkg/inventory.py").read_text(), patch)


def test_valid_rewrite_becomes_a_validated_unified_diff(root: Path, settings: Settings) -> None:
    fake = FakeLLM(lambda _step, _user: _answer(GOOD_RESTOCK))
    proposal = _proposer(settings, fake).propose(_request(root))

    assert proposal.status == "proposed", proposal.reason
    assert (proposal.generated_by, proposal.transform) == ("llm:fake-model", TRANSFORM)
    assert proposal.target_function == "restock_order"
    assert _patched(root, proposal.diff) == proposal.patched_source
    assert "Store" in proposal.patched_source  # the rest of the file is untouched
    namespace: dict[str, Any] = {}
    exec(compile(proposal.patched_source, "inventory.py", "exec"), namespace)
    assert namespace["restock_order"](["a", "b", "a", "c", "b"]) == ["a", "b", "c"]
    assert proposal.llm_usage is not None
    assert proposal.llm_usage["calls"] == 1
    assert proposal.llm_usage["prompt_version"] == "patch_proposal@1"
    assert any("AI_HYPOTHESIS" in note for note in proposal.notes)
    assert (root / "pkg/inventory.py").read_text() == SOURCE  # the proposer never writes

    # Only the target function, the finding and its measured evidence were sent.
    sent = fake.sent_text()
    assert "queue.pop(0)" in sent
    assert "unrelated module text" not in sent
    assert "def tally" not in sent
    assert "812.5" in sent
    assert "PP-COLL-002" in sent
    body = fake.bodies[0]
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system"


def test_methods_are_rewritten_with_their_indentation(root: Path, settings: Settings) -> None:
    fake = FakeLLM(lambda _step, _user: _answer(GOOD_TALLY))
    proposal = _proposer(settings, fake).propose(_request(root, "Store.tally", (22, 23)))
    assert proposal.status == "proposed", proposal.reason
    assert (
        "    def tally(self, items: list[int]) -> int:\n        counts" in proposal.patched_source
    )
    namespace: dict[str, Any] = {}
    exec(compile(proposal.patched_source, "inventory.py", "exec"), namespace)
    assert namespace["Store"]().tally([1, 2, 1]) == 5


def test_malformed_answer_is_retried_with_the_reason(root: Path, settings: Settings) -> None:
    answers = iter(["Sure! Here is the code: def restock_order(...)", _answer(GOOD_RESTOCK)])
    fake = FakeLLM(lambda _step, _user: next(answers))
    proposal = _proposer(settings, fake).propose(_request(root))
    assert proposal.status == "proposed", proposal.reason
    assert fake.calls == 2
    assert proposal.llm_usage is not None
    assert proposal.llm_usage["calls"] == 2
    assert len(proposal.llm_usage["rejected_answers"]) == 1
    retry = fake.bodies[1]["messages"]
    assert retry[-2]["role"] == "assistant"
    assert "was rejected" in retry[-1]["content"]


@pytest.mark.parametrize(
    ("replacement", "reason"),
    [
        (GOOD_RESTOCK.replace("def restock_order(", "def restock(", 1), "same kind"),
        (
            GOOD_RESTOCK.replace("events: list[str])", "events: list[str], limit: int = 0)"),
            "parameters",
        ),
        (
            GOOD_RESTOCK.replace("    processed: list", "    import time\n    processed: list", 1),
            "import of `time`",
        ),
        (
            GOOD_RESTOCK.replace("    processed: list", "    global CACHE\n    processed: list", 1),
            "global/nonlocal",
        ),
        (GOOD_RESTOCK + "\n\ndef helper() -> None:\n    pass\n", "exactly one function"),
        ("@functools.cache\n" + GOOD_RESTOCK, "decorators"),
        ("def restock_order(events: list[str]) -> list[str]:\n    return [", "does not parse"),
        (ORIGINAL_RESTOCK, "identical"),
    ],
)
def test_hallucinated_or_unsafe_rewrites_end_in_no_safe_patch(
    root: Path, settings: Settings, replacement: str, reason: str
) -> None:
    fake = FakeLLM(lambda _step, _user: _answer(replacement))
    proposal = _proposer(settings, fake).propose(_request(root))
    assert proposal.status == "no_safe_patch"
    assert proposal.diff == ""
    assert proposal.reason is not None
    assert proposal.reason.startswith("no safe LLM patch")
    assert reason in proposal.reason
    assert fake.calls == MAX_ATTEMPTS


def test_declined_rewrite_is_not_retried(root: Path, settings: Settings) -> None:
    fake = FakeLLM(lambda _step, _user: _answer("", "needs a deque import"))
    proposal = _proposer(settings, fake).propose(_request(root))
    assert proposal.status == "no_safe_patch"
    assert proposal.reason == "no safe LLM patch: the model declined (needs a deque import)"
    assert fake.calls == 1


def test_secret_like_code_is_never_sent(root: Path, settings: Settings) -> None:
    secret = SOURCE.replace(
        "    queue = list(events)", '    token = "ghp_' + "a" * 36 + '"\n    queue = list(events)'
    )
    (root / "pkg/inventory.py").write_text(secret)
    request = _request(root, lines=(12, 14))
    request.target.expected_sha256 = sha256_bytes(secret.encode())
    fake = FakeLLM()
    proposal = _proposer(settings, fake).propose(request)
    assert proposal.status == "no_safe_patch"
    assert "secret-like" in (proposal.reason or "")
    assert fake.calls == 0


def test_budget_is_checked_before_sending(root: Path, settings: Settings) -> None:
    fake = FakeLLM()
    proposal = _proposer(settings, fake, llm_max_tokens_per_call=300).propose(_request(root))
    assert proposal.status == "no_safe_patch"
    assert "token budget" in (proposal.reason or "")
    assert fake.calls == 0


def test_endpoint_failure_is_a_clear_refusal(root: Path, settings: Settings) -> None:
    fake = FakeLLM(lambda _step, _user: httpx.Response(503, json={"error": "overloaded"}))
    proposal = _proposer(settings, fake).propose(_request(root))
    assert proposal.status == "no_safe_patch"
    assert "model endpoint failed" in (proposal.reason or "")


def test_changed_source_is_refused_before_any_call(root: Path, settings: Settings) -> None:
    (root / "pkg/inventory.py").write_text(SOURCE + "\n# changed\n")
    fake = FakeLLM()
    with pytest.raises(SourceChangedError):
        _proposer(settings, fake).propose(_request(root))
    assert fake.calls == 0


def test_chain_asks_the_model_only_when_no_deterministic_patch_exists(
    root: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLLM(lambda _step, _user: _answer(GOOD_RESTOCK))
    monkeypatch.setattr(llm_client, "create_llm_client", client_factory(fake))
    configured = llm_settings(settings)

    assert isinstance(select_patch_proposer(configured), DeterministicPatchProposer)
    assert isinstance(select_patch_proposer(settings, allow_llm=True), DeterministicPatchProposer)
    chain = select_patch_proposer(configured, allow_llm=True)
    assert isinstance(chain, ChainedPatchProposer)
    assert chain.name == "deterministic -> llm:fake-model"

    proposal = chain.propose(_request(root))  # PP-COLL-002 has no deterministic template
    assert proposal.status == "proposed"
    assert proposal.generated_by == "llm:fake-model"
    assert proposal.attempts[0].startswith("deterministic: no safe deterministic patch")
    assert fake.calls == 1

    membership = _request(root, lines=(13, 13))
    membership.target.rule_id = "PP-LOOP-001"  # `sku in processed`: processed is mutated
    refused = chain.propose(membership)
    assert fake.calls == 2  # the deterministic template refused, so the model was asked
    assert refused.status == "proposed"
    assert json.loads(fake.raw[-1])["messages"][1]["content"].count("PP-LOOP-001") == 1
