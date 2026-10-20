"""Token budgets for the LLM pipeline: per-audit and per-call caps.

Token counts before a call are estimates (about four characters per token plus a small per-message
overhead); after a call the provider's reported ``usage`` replaces the estimate. When a call cannot
fit, :class:`BudgetExhaustedError` is raised *before* anything is sent, and the pipeline stops
gracefully and reports it instead of silently truncating.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

CHARS_PER_TOKEN = 4
MESSAGE_OVERHEAD_TOKENS = 4
MIN_OUTPUT_TOKENS = 256


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_messages(contents: Iterable[str]) -> int:
    return sum(estimate_tokens(c) + MESSAGE_OVERHEAD_TOKENS for c in contents)


class BudgetExhaustedError(Exception):
    """A call does not fit. ``scope='audit'``: the audit budget is spent (no more calls);
    ``scope='call'``: this request alone exceeds the per-call cap."""

    def __init__(self, message: str, scope: Literal["audit", "call"]) -> None:
        super().__init__(message)
        self.scope = scope


@dataclass
class TokenBudget:
    total: int
    per_call: int
    used: int = 0
    exhausted: bool = False

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.used)

    def context_allowance(self, reserved_output: int, overhead: int) -> int:
        """Tokens left for retrieved context in one call after prompts and output."""
        return max(0, min(self.per_call, self.remaining) - reserved_output - overhead)

    def plan_call(self, input_tokens: int, desired_output: int) -> int:
        """Max output tokens for a call with ``input_tokens`` of input, or raise."""
        if self.exhausted:
            raise BudgetExhaustedError("audit token budget already exhausted", "audit")
        if input_tokens + MIN_OUTPUT_TOKENS > self.per_call:
            raise BudgetExhaustedError(
                f"request needs ~{input_tokens} input tokens; per-call cap is {self.per_call}",
                "call",
            )
        output = min(desired_output, self.per_call - input_tokens, self.remaining - input_tokens)
        if output < MIN_OUTPUT_TOKENS:
            self.exhausted = True
            raise BudgetExhaustedError("audit token budget exhausted", "audit")
        return output

    def record(self, input_tokens: int, output_tokens: int) -> None:
        self.used += max(0, input_tokens) + max(0, output_tokens)
