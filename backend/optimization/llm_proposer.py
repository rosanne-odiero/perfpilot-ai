"""LLM-backed :class:`~backend.optimization.patch_generator.PatchProposer`.

Only the target function's source, the finding and its measured evidence are sent (redacted by
:mod:`backend.ai.outbound` inside the client, and only within the token budget). The model must
answer with a :class:`FunctionRewrite`: the complete replacement of the target function, nothing
else. The rewrite is spliced into the real file at the function's exact line span, turned into a
unified diff, and must then pass the same compile check and patch validator as every other
proposal. Malformed or unsafe answers are retried a bounded number of times, with the reasons fed
back; after that the proposer returns ``no_safe_patch``. A proposal is an ``AI_HYPOTHESIS`` until
it has been validated and benchmarked.
"""

from __future__ import annotations

import ast
import json
import textwrap
from collections.abc import Callable
from typing import Any

from pydantic import Field, ValidationError

from backend.ai import llm_client
from backend.ai.budget import BudgetExhaustedError, TokenBudget, estimate_messages
from backend.ai.outbound import ChatMessage, may_send_file
from backend.ai.prompt_loader import load_prompt
from backend.ai.schemas import LLMOutput
from backend.core.config import Settings
from backend.core.redaction import REDACTED, redact_text
from backend.infrastructure.artifact_store import sha256_bytes
from backend.optimization.diffs import make_unified_diff
from backend.optimization.patch_generator import (
    PatchProposal,
    PatchRequest,
    find_function,
    read_target,
)
from backend.optimization.patch_validator import (
    MAX_FILE_BYTES,
    public_signatures,
    validate_patch,
)
from backend.optimization.transforms import NoSafePatch

PROMPT_NAME = "patch_proposal"
TRANSFORM = "llm-function-rewrite"
MAX_ATTEMPTS = 2
MAX_FUNCTION_LINES = 300
DESIRED_OUTPUT_TOKENS = 2_000

ClientFactory = Callable[[Settings], llm_client.LLMClient]


class FunctionRewrite(LLMOutput):
    replacement: str = Field(default="", max_length=60_000)
    summary: str = Field(default="", max_length=2_000)
    correctness_notes: list[str] = Field(default_factory=list, max_length=10)


class _Rejected(Exception):
    """The model's answer cannot be used; the message is fed back on retry."""


def _numbered(lines: list[str], first: int) -> str:
    return "\n".join(f"{first + i:>5}| {line}" for i, line in enumerate(lines))


def _parse(content: str) -> FunctionRewrite:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        return FunctionRewrite.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise _Rejected(f"the answer is not a JSON object matching the schema ({exc})") from exc


def _splice(lines: list[str], start: int, end: int, indent: str, replacement: str) -> str:
    body = textwrap.dedent(replacement).strip("\n").split("\n")
    if any("\t" in line[: len(line) - len(line.lstrip())] for line in body):
        raise _Rejected("the replacement must be indented with spaces, not tabs")
    new_lines = [f"{indent}{line}" if line.strip() else "" for line in body]
    return "\n".join([*lines[: start - 1], *new_lines, *lines[end:]])


def _check_function(
    replacement: str, original: ast.FunctionDef | ast.AsyncFunctionDef
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    try:
        tree = ast.parse(textwrap.dedent(replacement))
    except (SyntaxError, ValueError) as exc:
        raise _Rejected(f"the replacement does not parse: {exc}") from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef | ast.AsyncFunctionDef):
        raise _Rejected("the replacement must be exactly one function definition and nothing else")
    function = tree.body[0]
    if function.name != original.name or type(function) is not type(original):
        raise _Rejected(f"the replacement must define `{original.name}` with the same kind")
    if function.decorator_list:
        raise _Rejected("the replacement must not contain decorators")
    if ast.dump(function.args) != ast.dump(original.args) or (
        ast.dump(function.returns) if function.returns else None
    ) != (ast.dump(original.returns) if original.returns else None):
        raise _Rejected("the replacement changes the function's parameters or return annotation")
    return function


class LLMPatchProposer:
    """Asks the configured model for a rewrite of the target function only."""

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: ClientFactory | None = None,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        self._settings = settings
        self._client_factory = client_factory
        self._max_attempts = max(1, max_attempts)

    @property
    def name(self) -> str:
        return f"llm:{self._settings.llm_model or 'unconfigured'}"

    def _client(self) -> llm_client.LLMClient:
        # Looked up at call time so tests can substitute the transport (no network in CI).
        factory = self._client_factory or llm_client.create_llm_client
        return factory(self._settings)

    def propose(self, request: PatchRequest) -> PatchProposal:
        target = request.target
        _path, data = read_target(target)
        base = PatchProposal(
            status="no_safe_patch",
            rule_id=target.rule_id,
            target_file=target.target_file,
            target_function=target.target_function,
            generated_by=self.name,
            original_sha256=sha256_bytes(data),
        )

        def refuse(reason: str, usage: dict[str, Any] | None = None) -> PatchProposal:
            return base.model_copy(
                update={"reason": f"no safe LLM patch: {reason}", "llm_usage": usage}
            )

        if not may_send_file(target.target_file):
            return refuse("the target file is sensitive and is never sent to a model")
        if len(data) > MAX_FILE_BYTES:
            return refuse(f"the file is larger than {MAX_FILE_BYTES} bytes")
        try:
            text = data.decode("utf-8")
            if "\r" in text:
                return refuse("files with CR/CRLF line endings are not rewritten")
            tree = ast.parse(text, filename=target.target_file)
            qualname, function = find_function(
                tree, target.target_function, target.line_start, target.line_end
            )
        except (UnicodeDecodeError, SyntaxError, ValueError, NoSafePatch) as exc:
            return refuse(f"the target function could not be located: {exc}")
        lines = text.split("\n")
        start, end = function.lineno, function.end_lineno or function.lineno
        if end - start + 1 > MAX_FUNCTION_LINES:
            return refuse(f"the function is longer than {MAX_FUNCTION_LINES} lines")
        source_lines = lines[start - 1 : end]
        indent = source_lines[0][: len(source_lines[0]) - len(source_lines[0].lstrip())]
        if "\t" in indent:
            return refuse("tab indentation is not rewritten")
        function_source = textwrap.dedent("\n".join(source_lines))
        if redact_text(function_source).count:
            return refuse("the function contains secret-like content; it is not sent to a model")

        prompt = load_prompt(PROMPT_NAME)
        user = self._user_message(request, qualname, function_source, start)
        messages = [ChatMessage("system", prompt.text), ChatMessage("user", user)]
        budget = TokenBudget(
            total=min(
                self._settings.llm_max_tokens_per_audit,
                self._settings.llm_max_tokens_per_call * self._max_attempts,
            ),
            per_call=self._settings.llm_max_tokens_per_call,
        )
        usage: dict[str, Any] = {
            "model": self._settings.llm_model,
            "prompt_version": f"{prompt.name}@{prompt.version}",
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "redactions": 0,
            "rejected_answers": [],
        }
        try:
            client = self._client()
        except Exception as exc:  # misconfiguration must not break candidate creation
            return refuse(f"the model client could not be created ({type(exc).__name__})", usage)
        try:
            for _attempt in range(self._max_attempts):
                try:
                    max_tokens = budget.plan_call(
                        estimate_messages(m.content for m in messages), DESIRED_OUTPUT_TOKENS
                    )
                except BudgetExhaustedError as exc:
                    return refuse(f"token budget: {exc}", usage)
                try:
                    completion = client.complete(messages, max_tokens=max_tokens)
                except llm_client.LLMError as exc:
                    return refuse(f"the model endpoint failed: {exc.message}", usage)
                budget.record(completion.input_tokens, completion.output_tokens)
                usage["calls"] += 1
                usage["input_tokens"] += completion.input_tokens
                usage["output_tokens"] += completion.output_tokens
                usage["model"] = completion.model
                try:
                    proposal = self._build(
                        request, base, text, lines, function, qualname, indent, completion.content
                    )
                except _Rejected as rejected:
                    usage["rejected_answers"].append(str(rejected)[:500])
                    messages = [
                        *messages,
                        ChatMessage("assistant", completion.content[:8_000]),
                        ChatMessage(
                            "user",
                            f"Your answer was rejected: {rejected}. Answer again with one JSON "
                            "object following every rule, or with an empty replacement.",
                        ),
                    ]
                    continue
                return proposal.model_copy(update={"llm_usage": usage})
        finally:
            usage["redactions"] = client.redactions
            client.close()
        reasons = "; ".join(usage["rejected_answers"][-2:])
        return refuse(f"no usable answer after {self._max_attempts} attempts ({reasons})", usage)

    @staticmethod
    def _user_message(
        request: PatchRequest, qualname: str, function_source: str, first_line: int
    ) -> str:
        target = request.target
        context = {
            "file": target.target_file,
            "function": qualname,
            "flagged_lines": [target.line_start, target.line_end],
            "rule_id": target.rule_id,
            "finding": request.title,
            "suggested_strategy": request.proposed_optimization,
            "known_correctness_risks": request.correctness_risks,
            "evidence": request.evidence,
        }
        return (
            "Finding and evidence (JSON):\n"
            f"{json.dumps(context, indent=2, sort_keys=True, default=str)}\n\n"
            f"Function `{qualname}` (lines {first_line}-"
            f"{first_line + function_source.count(chr(10))} of {target.target_file}, "
            "dedented; line numbers are for reference only):\n"
            f"{_numbered(function_source.split(chr(10)), first_line)}"
        )

    def _build(
        self,
        request: PatchRequest,
        base: PatchProposal,
        text: str,
        lines: list[str],
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        qualname: str,
        indent: str,
        content: str,
    ) -> PatchProposal:
        rewrite = _parse(content)
        if not rewrite.replacement.strip():
            return base.model_copy(
                update={
                    "reason": "no safe LLM patch: the model declined"
                    + (f" ({rewrite.summary})" if rewrite.summary else "")
                }
            )
        if REDACTED in rewrite.replacement:
            raise _Rejected("the replacement contains a redaction placeholder")
        _check_function(rewrite.replacement, function)
        start, end = function.lineno, function.end_lineno or function.lineno
        patched = _splice(lines, start, end, indent, rewrite.replacement)
        if patched == text:
            raise _Rejected("the replacement is identical to the original function")
        try:
            compile(patched, request.target.target_file, "exec", dont_inherit=True)
            patched_tree = ast.parse(patched)
        except (SyntaxError, ValueError) as exc:
            raise _Rejected(f"the patched file does not compile: {exc}") from exc
        if public_signatures(patched_tree) != public_signatures(ast.parse(text)):
            raise _Rejected("the rewrite changes a public signature")
        diff = make_unified_diff(request.target.target_file, text, patched)
        validated = validate_patch(
            diff,
            workspace_root=request.target.source_root,
            allowed_files=[request.target.target_file],
        )
        if not validated.report.valid:
            problems = "; ".join(v.message for v in validated.report.violations[:5])
            raise _Rejected(f"the patch validator rejected the rewrite: {problems}")
        if validated.patched.get(request.target.target_file) != patched:
            raise _Rejected("the generated diff does not reproduce the rewritten file")
        return base.model_copy(
            update={
                "status": "proposed",
                "target_function": qualname,
                "transform": TRANSFORM,
                "patched_sha256": sha256_bytes(patched.encode("utf-8")),
                "diff": diff,
                "original_source": text,
                "patched_source": patched,
                "summary": rewrite.summary or f"LLM rewrite of `{qualname}`",
                "notes": [
                    "Generated by a language model (AI_HYPOTHESIS) until it is validated and "
                    "benchmarked.",
                    *rewrite.correctness_notes,
                ],
            }
        )
