"""Versioned prompt files in ``backend/ai/prompts/``.

Each file starts with ``<!-- perfpilot-prompt name=<name> version=<n> -->``. The header is
stripped from the text sent to the model; the version is recorded in ``LLMUsage.prompt_version``
and in the summary-cache key, so changing a prompt invalidates cached summaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"
PIPELINE_PROMPTS = (
    "system",
    "unit_summaries",
    "dependency_context",
    "hot_paths",
    "correlation",
    "recommendations",
)
_HEADER = re.compile(r"\A<!--\s*perfpilot-prompt\s+name=(\w+)\s+version=(\d+)\s*-->\s*\n")


@dataclass(frozen=True)
class Prompt:
    name: str
    version: int
    text: str


@cache
def load_prompt(name: str) -> Prompt:
    if name not in PIPELINE_PROMPTS:
        raise KeyError(f"unknown prompt {name!r}")
    raw = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    match = _HEADER.match(raw)
    if match is None or match.group(1) != name:
        raise ValueError(f"prompt {name!r} has no valid version header")
    return Prompt(name=name, version=int(match.group(2)), text=raw[match.end() :].strip())


def pipeline_version() -> str:
    return ",".join(f"{n}@{load_prompt(n).version}" for n in PIPELINE_PROMPTS)
