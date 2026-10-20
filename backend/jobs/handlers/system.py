"""Built-in diagnostic job used to verify the queue end to end."""

from __future__ import annotations

import time
from typing import Any

from backend.jobs.registry import JobContext, job_handler


@job_handler("system.echo")
def echo(ctx: JobContext) -> dict[str, Any]:
    """Echo the payload back. ``sleep_seconds`` (capped at 30) exercises timeouts/cancellation."""
    sleep_for = min(float(ctx.payload.get("sleep_seconds", 0)), 30.0)
    steps = max(1, int(sleep_for * 4))
    for i in range(steps if sleep_for else 0):
        time.sleep(sleep_for / steps)
        ctx.progress("sleeping", (i + 1) / steps)
    if ctx.payload.get("fail"):
        raise ValueError("requested failure")
    return {"echo": ctx.payload.get("message")}
