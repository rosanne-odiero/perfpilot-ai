"""Patch generation against a hashed source snapshot.

``generate_patch`` is generic over *(source root, target file, target function/line span, rule
id)*, so the same code serves bundled workloads and repository hotspots. It verifies the target
file's hash before reading it, runs the deterministic AST transformation registered for the rule,
re-parses and compiles the result, and returns a reviewable unified diff - or an explicit
``no_safe_patch`` result explaining why nothing was proposed. Nothing is ever guessed.

Proposers implement :class:`PatchProposer`. :class:`DeterministicPatchProposer` ships here; the
LLM-backed :class:`~backend.optimization.llm_proposer.LLMPatchProposer` is chained after it only
when the caller has consent and a model is configured (:func:`select_patch_proposer`). Another
proposer can be registered with :func:`register_patch_proposer`. Whatever proposes the diff, the
patch validator is the gate before approval.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from backend.core.config import Settings
from backend.core.exceptions import ConflictError, SecurityViolationError
from backend.infrastructure.artifact_store import sha256_bytes
from backend.optimization.diffs import make_unified_diff
from backend.optimization.patch_validator import (
    MAX_FILE_BYTES,
    path_problem,
    public_signatures,
)
from backend.optimization.source_edits import EditConflictError
from backend.optimization.transforms import TRANSFORMS, NoSafePatch, build_context

DETERMINISTIC = "deterministic"


class SourceChangedError(ConflictError):
    code = "source_changed"


class PatchTarget(BaseModel):
    source_root: Path
    target_file: str = Field(description="Workspace-relative POSIX path")
    target_function: str | None = Field(default=None, description="Qualified name, e.g. f or C.m")
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    rule_id: str
    expected_sha256: str | None = Field(
        default=None, description="Hash recorded by the analysis snapshot; verified before use"
    )


class PatchRequest(BaseModel):
    """What a proposer receives: the target plus the recommendation it should implement."""

    target: PatchTarget
    title: str = ""
    proposed_optimization: str = ""
    correctness_risks: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Finding details and MEASURED profiler evidence (sent to an LLM proposer)",
    )


class PatchProposal(BaseModel):
    status: Literal["proposed", "no_safe_patch"]
    rule_id: str
    target_file: str
    target_function: str | None
    generated_by: str = DETERMINISTIC
    transform: str | None = None
    original_sha256: str
    patched_sha256: str | None = None
    diff: str = ""
    original_source: str = ""
    patched_source: str = ""
    summary: str = ""
    notes: list[str] = Field(default_factory=list)
    reason: str | None = None
    attempts: list[str] = Field(
        default_factory=list, description="Proposers consulted before this result, and why not"
    )
    llm_usage: dict[str, Any] | None = None


class PatchProposer(Protocol):
    @property
    def name(self) -> str:
        """``"deterministic"`` or the model identifier recorded on the candidate."""
        ...

    def propose(self, request: PatchRequest) -> PatchProposal: ...


def qualified_functions(
    tree: ast.Module,
) -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    found: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                qualname = f"{prefix}{child.name}"
                found.append((qualname, child))
                visit(child, f"{qualname}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")

    visit(tree, "")
    return found


def find_function(
    tree: ast.Module, qualname: str | None, line_start: int, line_end: int
) -> tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    candidates = [
        (name, node)
        for name, node in qualified_functions(tree)
        if node.lineno <= line_start and (node.end_lineno or node.lineno) >= line_end
    ]
    if qualname:
        candidates = [
            (name, node)
            for name, node in candidates
            if name == qualname or name.rsplit(".", 1)[-1] == qualname
        ]
    if not candidates:
        raise NoSafePatch(f"no function {qualname or ''} encloses lines {line_start}-{line_end}")
    return max(candidates, key=lambda item: item[1].lineno)


def read_target(target: PatchTarget) -> tuple[Path, bytes]:
    if problem := path_problem(target.target_file):
        raise SecurityViolationError(f"invalid target file: {problem}")
    root = target.source_root.resolve()
    path = root / target.target_file
    current = root
    for part in target.target_file.split("/"):
        current = current / part
        if current.is_symlink():
            raise SecurityViolationError("target file path contains a symlink")
    if not path.resolve().is_relative_to(root) or not path.is_file():
        raise SecurityViolationError("target file is not a regular file inside the source root")
    data = path.read_bytes()
    if target.expected_sha256 and sha256_bytes(data) != target.expected_sha256:
        raise SourceChangedError(
            f"{target.target_file} changed since it was analyzed (hash mismatch); re-run the "
            "analysis before generating a patch",
            details={"expected_sha256": target.expected_sha256, "actual": sha256_bytes(data)},
        )
    return path, data


def generate_patch(target: PatchTarget) -> PatchProposal:
    _path, data = read_target(target)
    original_sha = sha256_bytes(data)
    base = PatchProposal(
        status="no_safe_patch",
        rule_id=target.rule_id,
        target_file=target.target_file,
        target_function=target.target_function,
        original_sha256=original_sha,
    )

    def refuse(reason: str) -> PatchProposal:
        return base.model_copy(update={"reason": f"no safe deterministic patch: {reason}"})

    transform = TRANSFORMS.get(target.rule_id)
    if transform is None:
        return refuse(f"no deterministic transformation exists for rule {target.rule_id}")
    if len(data) > MAX_FILE_BYTES:
        return refuse(f"the file is larger than {MAX_FILE_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return refuse("the file is not UTF-8 text")
    if "\r" in text:
        return refuse("files with CR/CRLF line endings are not rewritten")
    try:
        tree = ast.parse(text, filename=target.target_file)
        qualname, function = find_function(
            tree, target.target_function, target.line_start, target.line_end
        )
        context = build_context(
            target.target_file, text, tree, function, qualname, target.line_start, target.line_end
        )
        result = transform(context)
        patched = result.edits.apply(text)
    except NoSafePatch as exc:
        return refuse(str(exc))
    except (SyntaxError, ValueError, EditConflictError) as exc:
        return refuse(f"the source could not be analyzed: {exc}")
    try:
        compile(patched, target.target_file, "exec", dont_inherit=True)
        patched_tree = ast.parse(patched)
    except (SyntaxError, ValueError) as exc:
        return refuse(f"the rewritten code does not compile ({exc}); refusing to propose it")
    if public_signatures(patched_tree) != public_signatures(tree):
        return refuse("the rewrite would change a public signature")
    return base.model_copy(
        update={
            "status": "proposed",
            "target_function": qualname,
            "transform": result.name,
            "patched_sha256": sha256_bytes(patched.encode("utf-8")),
            "diff": make_unified_diff(target.target_file, text, patched),
            "original_source": text,
            "patched_source": patched,
            "summary": result.summary,
            "notes": result.notes,
        }
    )


class DeterministicPatchProposer:
    """AST-guided templates for the bundled rule families; never calls a model."""

    @property
    def name(self) -> str:
        return DETERMINISTIC

    def propose(self, request: PatchRequest) -> PatchProposal:
        return generate_patch(request.target)


class ChainedPatchProposer:
    """Ask each proposer in turn and return the first proposed patch. If none proposes one, the
    last refusal is returned; every refusal is listed in ``attempts``."""

    def __init__(self, proposers: Sequence[PatchProposer]) -> None:
        if not proposers:
            raise ValueError("at least one proposer is required")
        self._proposers = list(proposers)

    @property
    def name(self) -> str:
        return " -> ".join(p.name for p in self._proposers)

    def propose(self, request: PatchRequest) -> PatchProposal:
        attempts: list[str] = []
        usage: dict[str, Any] | None = None
        proposal: PatchProposal | None = None
        for proposer in self._proposers:
            proposal = proposer.propose(request)
            usage = proposal.llm_usage or usage
            if proposal.status == "proposed":
                break
            attempts.append(f"{proposer.name}: {proposal.reason or 'no patch'}")
        assert proposal is not None
        return proposal.model_copy(
            update={"attempts": [*attempts, *proposal.attempts], "llm_usage": usage}
        )


ProposerFactory = Callable[[Settings], PatchProposer | None]
_factory: ProposerFactory | None = None


def register_patch_proposer(factory: ProposerFactory | None) -> None:
    """Install a factory (e.g. an LLM proposer when a model is configured). Returning ``None``
    from the factory falls back to the deterministic proposer."""
    global _factory
    _factory = factory


def select_patch_proposer(settings: Settings, *, allow_llm: bool = False) -> PatchProposer:
    """The registered proposer (default: deterministic). With ``allow_llm`` (the caller has the
    user's consent) and a configured model, the LLM proposer is asked when the first one finds no
    safe patch."""
    base: PatchProposer = DeterministicPatchProposer()
    if _factory is not None and (proposer := _factory(settings)) is not None:
        base = proposer
    if not allow_llm or not settings.llm_configured:
        return base
    from backend.optimization.llm_proposer import LLMPatchProposer

    return ChainedPatchProposer([base, LLMPatchProposer(settings)])
