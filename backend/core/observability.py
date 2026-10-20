"""Log context propagation and lightweight tracing.

* :func:`log_context` binds identifiers (``job_id``, ``audit_id``, ``snapshot_id``, ``run_id``,
  ``operation``...) to the current execution context; every JSON log record emitted inside the
  block carries them (see :class:`backend.core.logging.JsonFormatter`).
* :func:`span` times an operation, logs one ``operation finished`` record with ``duration_ms`` and
  ``status`` (``ok``/``error``), and forwards the same span to OpenTelemetry when — and only when —
  ``opentelemetry`` is importable **and** ``PERFPILOT_OTEL_ENABLED=true``. PerfPilot does not
  depend on OpenTelemetry; exporters/SDK configuration are the deployer's choice.

Never put prompts, source code or secrets into span attributes: only identifiers, counts, sizes
and durations.
"""

from __future__ import annotations

import contextlib
import functools
import importlib
import importlib.util
import time
from collections.abc import Callable, Iterator
from typing import Any, ParamSpec, Protocol, TypeVar

from backend.core.logging import LOG_CONTEXT, get_logger

log = get_logger("perfpilot.trace")

Scalar = str | int | float | bool
P = ParamSpec("P")
R = TypeVar("R")

_context = LOG_CONTEXT


def current_context() -> dict[str, Scalar]:
    return dict(_context.get())


@contextlib.contextmanager
def log_context(**fields: Scalar | None) -> Iterator[None]:
    """Bind non-``None`` fields to every log record emitted inside the block."""
    merged = {**_context.get(), **{k: v for k, v in fields.items() if v is not None}}
    token = _context.set(merged)
    try:
        yield
    finally:
        _context.reset(token)


class _OtelSpan(Protocol):
    def set_attribute(self, key: str, value: Scalar) -> None: ...

    def record_exception(self, exception: BaseException) -> None: ...


class SpanHandle:
    """Handle yielded by :func:`span` for adding attributes discovered during the operation."""

    def __init__(self, otel_span: _OtelSpan | None) -> None:
        self._otel = otel_span
        self.attributes: dict[str, Scalar] = {}

    def set(self, **attributes: Scalar | None) -> None:
        for key, value in attributes.items():
            if value is None:
                continue
            self.attributes[key] = value
            if self._otel is not None:
                self._otel.set_attribute(f"perfpilot.{key}", value)


class _Tracing:
    enabled: bool = False
    _tracer: Any = None
    _status_error: Any = None

    def configure(self, enabled: bool) -> bool:
        """Activate the OpenTelemetry adapter if requested and the API package is importable."""
        self._tracer = None
        self.enabled = False
        if not enabled or importlib.util.find_spec("opentelemetry") is None:
            return False
        trace = importlib.import_module("opentelemetry.trace")
        self._tracer = trace.get_tracer("perfpilot")
        self._status_error = trace.Status(trace.StatusCode.ERROR)
        self.enabled = True
        return True

    @contextlib.contextmanager
    def start(self, name: str) -> Iterator[_OtelSpan | None]:
        if self._tracer is None:
            yield None
            return
        with self._tracer.start_as_current_span(name) as otel_span:
            yield otel_span

    def mark_error(self, otel_span: _OtelSpan | None, exc: BaseException) -> None:
        if otel_span is None:
            return
        otel_span.record_exception(exc)
        set_status = getattr(otel_span, "set_status", None)
        if callable(set_status):
            set_status(self._status_error)


_tracing = _Tracing()


def configure_tracing(enabled: bool) -> bool:
    """Called at process start; returns whether OpenTelemetry spans are emitted."""
    active = _tracing.configure(enabled)
    if enabled and not active:
        log.warning("PERFPILOT_OTEL_ENABLED is set but opentelemetry is not installed")
    return active


def tracing_active() -> bool:
    return _tracing.enabled


@contextlib.contextmanager
def span(operation: str, **attributes: Scalar | None) -> Iterator[SpanHandle]:
    """Time ``operation`` and log its outcome; mirrors it to OpenTelemetry when enabled."""
    started = time.perf_counter()
    with log_context(operation=operation), _tracing.start(operation) as otel_span:
        handle = SpanHandle(otel_span)
        handle.set(**{k: v for k, v in current_context().items() if k != "operation"})
        handle.set(**attributes)
        status = "ok"
        try:
            yield handle
        except BaseException as exc:
            status = "error"
            handle.set(error_type=type(exc).__name__)
            _tracing.mark_error(otel_span, exc)
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            if otel_span is not None:
                otel_span.set_attribute("perfpilot.duration_ms", duration_ms)
                otel_span.set_attribute("perfpilot.status", status)
            log.info(
                "operation finished",
                extra={**handle.attributes, "duration_ms": duration_ms, "status": status},
            )


def traced(operation: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator form of :func:`span` for whole-function operations."""

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            with span(operation):
                return fn(*args, **kwargs)

        return wrapper

    return decorator
