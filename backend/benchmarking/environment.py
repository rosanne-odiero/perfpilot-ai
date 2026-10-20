"""Environment fingerprint stored with every profile and benchmark.

Anything that cannot be determined on this host is recorded explicitly in ``notes`` instead of
being omitted, so readers know which sources of noise were not controlled.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

import psutil

from backend import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PLAUSIBLE_MIN_MHZ = 100.0
_TRACKED_PACKAGES = ("numpy", "psutil", "pydantic", "sqlalchemy", "fastapi", "scipy")


def _cpu_model(notes: list[str]) -> str | None:
    system = platform.system()
    try:
        if system == "Darwin":
            out = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            return out.stdout.strip() or None
        if system == "Linux":
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.lower().startswith(("model name", "hardware", "cpu model")):
                    return line.split(":", 1)[1].strip()
    except (OSError, subprocess.SubprocessError) as exc:
        notes.append(f"CPU model unavailable: {type(exc).__name__}")
        return None
    notes.append(f"CPU model lookup not implemented for {system}")
    return platform.processor() or None


def _cpu_frequency(notes: list[str]) -> dict[str, float] | None:
    try:
        freq = psutil.cpu_freq()
    except (NotImplementedError, OSError, AttributeError):
        freq = None
    if freq is None or not freq.current or freq.current < _PLAUSIBLE_MIN_MHZ:
        notes.append(
            "CPU frequency unavailable (not reported, or implausible value from the OS); "
            "frequency scaling was not controlled"
        )
        return None
    notes.append("CPU frequency scaling/turbo state was not controlled")
    return {"current_mhz": freq.current, "min_mhz": freq.min, "max_mhz": freq.max}


def _affinity(notes: list[str]) -> list[int] | None:
    getter = getattr(os, "sched_getaffinity", None)
    if getter is None:
        notes.append("CPU affinity unavailable on this OS; processes were not pinned to a core")
        return None
    notes.append("processes were not pinned to a core (affinity inherited)")
    return sorted(getter(0))


def _packages() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in _TRACKED_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _lock_hash(notes: list[str]) -> str | None:
    lock = PROJECT_ROOT / "uv.lock"
    if not lock.is_file():
        notes.append("uv.lock not found; dependency lock hash unavailable")
        return None
    return hashlib.sha256(lock.read_bytes()).hexdigest()


def capture_environment(**context: Any) -> dict[str, Any]:
    """Fingerprint the host. ``context`` (size, source hashes, settings...) is stored verbatim."""
    notes: list[str] = []
    try:
        load = list(os.getloadavg())
    except (OSError, AttributeError):
        load = None
        notes.append("load average unavailable")
    notes.append("thermal state unavailable (not exposed to unprivileged processes)")
    fingerprint: dict[str, Any] = {
        "captured_at": datetime.now(UTC).isoformat(),
        "perfpilot_version": __version__,
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "cpu_model": _cpu_model(notes),
        "logical_cpus": os.cpu_count(),
        "physical_cpus": psutil.cpu_count(logical=False),
        "cpu_frequency": _cpu_frequency(notes),
        "cpu_affinity": _affinity(notes),
        "load_average_1_5_15": load,
        "total_memory_bytes": psutil.virtual_memory().total,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "python_executable": sys.executable,
        "packages": _packages(),
        "uv_lock_sha256": _lock_hash(notes),
        "notes": notes,
    }
    fingerprint.update(context)
    return fingerprint
