"""Compare baseline and candidate outputs according to a workload's output contract.

Outputs arrive in the harness' canonical form (see ``harness_entry.canonical``): dict and set
ordering are already normalized, and tuples/lists stay distinct, so ``EXACT`` comparison is a
hash comparison and ``FLOAT_TOLERANCE`` walks both structures with ``math.isclose``.
"""

from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel

from backend.models.workloads import OutputComparison, OutputContract


class CorrectnessResult(BaseModel):
    equivalent: bool
    comparison: OutputComparison
    detail: str


def _close(a: Any, b: Any, contract: OutputContract, path: str) -> str | None:
    if isinstance(a, bool) or isinstance(b, bool):
        return None if a is b else f"{path}: {a!r} != {b!r}"
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, int) and isinstance(b, int):
            return None if a == b else f"{path}: {a} != {b}"
        if math.isclose(a, b, rel_tol=contract.rel_tol, abs_tol=contract.abs_tol):
            return None
        return (
            f"{path}: {a!r} vs {b!r} exceeds rel_tol={contract.rel_tol} abs_tol={contract.abs_tol}"
        )
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for index, (x, y) in enumerate(zip(a, b, strict=True)):
            problem = _close(x, y, contract, f"{path}[{index}]")
            if problem:
                return problem
        return None
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return f"{path}: different structure {sorted(a)} != {sorted(b)}"
        for key in a:
            problem = _close(a[key], b[key], contract, f"{path}.{key}")
            if problem:
                return problem
        return None
    return None if a == b else f"{path}: {a!r} != {b!r}"


def compare_outputs(
    baseline: dict[str, Any], candidate: dict[str, Any], contract: OutputContract
) -> CorrectnessResult:
    """``baseline``/``candidate`` are harness results (``output_sha256`` and, for tolerance
    comparisons, ``output_value``)."""
    if baseline.get("output_sha256") == candidate.get("output_sha256"):
        return CorrectnessResult(
            equivalent=True, comparison=contract.comparison, detail="outputs are identical"
        )
    if contract.comparison == OutputComparison.EXACT:
        return CorrectnessResult(
            equivalent=False,
            comparison=contract.comparison,
            detail=(
                f"output hashes differ: baseline {baseline.get('output_preview')!r} "
                f"vs candidate {candidate.get('output_preview')!r}"
            ),
        )
    if "output_value" not in baseline or "output_value" not in candidate:
        return CorrectnessResult(
            equivalent=False,
            comparison=contract.comparison,
            detail="outputs differ and are too large to compare with tolerance",
        )
    problem = _close(baseline["output_value"], candidate["output_value"], contract, "output")
    return CorrectnessResult(
        equivalent=problem is None,
        comparison=contract.comparison,
        detail=problem or "outputs equal within tolerance",
    )
