"""Optional py-spy sampling profiler (flamegraph/speedscope output).

py-spy is an external binary. When it is missing, or cannot attach (macOS requires root), the
profile still completes and a clear warning is recorded instead.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from pydantic import BaseModel

PY_SPY_RATE_HZ = 250


class SamplerStatus(BaseModel):
    available: bool
    binary: str | None = None
    reason: str | None = None


def detect_py_spy() -> SamplerStatus:
    binary = shutil.which("py-spy")
    if binary is None:
        return SamplerStatus(
            available=False,
            reason="py-spy binary not found on PATH (install the 'sampling' extra to enable it)",
        )
    if sys.platform == "darwin" and os.geteuid() != 0:
        return SamplerStatus(
            available=False,
            binary=binary,
            reason="py-spy needs root privileges on macOS to sample another process",
        )
    return SamplerStatus(available=True, binary=str(Path(binary).resolve()))


def py_spy_wrapper(binary: str, output: Path, rate_hz: int = PY_SPY_RATE_HZ) -> list[str]:
    """argv prefix that makes py-spy launch and sample the harness process."""
    return [
        binary,
        "record",
        "--format",
        "speedscope",
        "--output",
        str(output),
        "--rate",
        str(rate_hz),
        "--",
    ]
