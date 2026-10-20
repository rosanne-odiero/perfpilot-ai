"""Structured log context, spans and the optional OpenTelemetry adapter."""

from __future__ import annotations

import importlib.machinery
import json
import logging
import sys
import types
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from llm_support import FAKE_KEY, llm_settings
from log_support import JsonLines, capture_json_logs

from backend.ai.llm_client import LLMClient
from backend.ai.outbound import ChatMessage
from backend.core import observability
from backend.core.config import Settings
from backend.core.observability import (
    configure_tracing,
    current_context,
    log_context,
    span,
    traced,
    tracing_active,
)
from backend.jobs.service import enqueue_job
from backend.jobs.worker import Worker
from backend.models.database import session_scope


@pytest.fixture
def json_logs() -> Iterator[JsonLines]:
    with capture_json_logs() as lines:
        yield lines


@pytest.fixture
def no_tracing() -> Iterator[None]:
    yield
    configure_tracing(False)


def test_log_context_fields_are_added_and_restored(json_logs: JsonLines) -> None:
    log = logging.getLogger("perfpilot.test")
    with log_context(job_id="j-1", audit_id="a-1", snapshot_id=None):
        with log_context(run_id="r-1"):
            log.info("inner")
        log.info("outer")
    log.info("after")
    inner, outer, after = json_logs.records()
    assert inner["job_id"] == "j-1"
    assert inner["run_id"] == "r-1"
    assert inner["audit_id"] == "a-1"
    assert "snapshot_id" not in inner
    assert outer["job_id"] == "j-1"
    assert "run_id" not in outer
    assert "job_id" not in after
    assert current_context() == {}


def test_context_values_are_scrubbed(json_logs: JsonLines) -> None:
    with log_context(operation="token=supersecretvalue123"):
        logging.getLogger("perfpilot.test").info("x")
    [record] = json_logs.records()
    assert "supersecretvalue123" not in json.dumps(record)


def test_span_logs_duration_and_status(json_logs: JsonLines) -> None:
    with log_context(job_id="j-2"), span("unit.op", files=3) as handle:
        handle.set(findings=7, skipped=None)
    with pytest.raises(RuntimeError), span("unit.fail"):
        raise RuntimeError("boom")
    ok, failed = json_logs.named("operation finished")
    assert ok["operation"] == "unit.op"
    assert ok["status"] == "ok"
    assert ok["files"] == 3
    assert ok["findings"] == 7
    assert "skipped" not in ok
    assert ok["job_id"] == "j-2"
    assert isinstance(ok["duration_ms"], float)
    assert ok["duration_ms"] >= 0
    assert failed["status"] == "error"
    assert failed["error_type"] == "RuntimeError"


def test_traced_decorator_preserves_function(json_logs: JsonLines) -> None:
    @traced("unit.decorated")
    def add(a: int, b: int) -> int:
        """Adds."""
        return a + b

    assert add(2, 3) == 5
    assert add.__name__ == "add"
    assert add.__doc__ == "Adds."
    [record] = json_logs.named("operation finished")
    assert record["operation"] == "unit.decorated"


def test_tracing_stays_off_without_opentelemetry(
    monkeypatch: pytest.MonkeyPatch, json_logs: JsonLines, no_tracing: None
) -> None:
    monkeypatch.setattr(observability.importlib.util, "find_spec", lambda name: None)
    assert configure_tracing(True) is False
    assert not tracing_active()
    assert json_logs.named("PERFPILOT_OTEL_ENABLED is set but opentelemetry is not installed")
    assert configure_tracing(False) is False


class _FakeSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, Any] = {}
        self.exceptions: list[BaseException] = []
        self.status: object = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def record_exception(self, exception: BaseException) -> None:
        self.exceptions.append(exception)

    def set_status(self, status: object) -> None:
        self.status = status

    def __enter__(self) -> _FakeSpan:
        return self

    def __exit__(self, *_: object) -> None:
        return None


class _FakeTracer:
    def __init__(self) -> None:
        self.spans: list[_FakeSpan] = []

    def start_as_current_span(self, name: str) -> _FakeSpan:
        self.spans.append(_FakeSpan(name))
        return self.spans[-1]


@pytest.fixture
def fake_otel(monkeypatch: pytest.MonkeyPatch) -> _FakeTracer:
    """A minimal stand-in for the ``opentelemetry`` API package (not installed here)."""
    tracer = _FakeTracer()
    package = types.ModuleType("opentelemetry")
    package.__spec__ = importlib.machinery.ModuleSpec("opentelemetry", None)
    trace = types.ModuleType("opentelemetry.trace")
    trace.get_tracer = lambda name: tracer  # type: ignore[attr-defined]
    trace.StatusCode = types.SimpleNamespace(ERROR="ERROR")  # type: ignore[attr-defined]
    trace.Status = lambda code: ("status", code)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry", package)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", trace)
    return tracer


def test_opentelemetry_adapter_forwards_spans(fake_otel: _FakeTracer, no_tracing: None) -> None:
    assert configure_tracing(False) is False
    with span("not.exported"):
        pass
    assert fake_otel.spans == []

    assert configure_tracing(True) is True
    with log_context(audit_id="a-9"), span("audit.static_analysis", files=2):
        pass
    with pytest.raises(ValueError), span("llm.call"):
        raise ValueError("bad")
    exported, failed = fake_otel.spans
    assert exported.name == "audit.static_analysis"
    assert exported.attributes["perfpilot.audit_id"] == "a-9"
    assert exported.attributes["perfpilot.files"] == 2
    assert exported.attributes["perfpilot.status"] == "ok"
    assert "perfpilot.duration_ms" in exported.attributes
    assert failed.attributes["perfpilot.status"] == "error"
    assert failed.status == ("status", "ERROR")
    assert len(failed.exceptions) == 1


def test_job_logs_carry_ids_stages_and_durations(settings: Settings, json_logs: JsonLines) -> None:
    with session_scope() as session:
        job = enqueue_job(
            session,
            "system.echo",
            {"message": "hi", "sleep_seconds": 0.3, "audit_id": "a-42"},
            subject_type="snapshot",
            subject_id="s-7",
        )
        job_id = job.id
    Worker(settings, isolate=False).drain()

    [done] = json_logs.named("job succeeded")
    assert done["job_id"] == job_id
    assert done["job_type"] == "system.echo"
    assert done["audit_id"] == "a-42"
    assert done["snapshot_id"] == "s-7"
    assert done["status"] == "succeeded"
    assert done["duration_ms"] > 0
    [stage] = json_logs.named("job stage finished")
    assert stage["stage"] == "sleeping"
    assert stage["job_id"] == job_id
    assert stage["status"] == "ok"
    [op] = [r for r in json_logs.named("operation finished") if r["job_id"] == job_id]
    assert op["operation"] == "job.system.echo"
    assert op["status"] == "ok"


def test_failed_job_logs_status_and_error_code(settings: Settings, json_logs: JsonLines) -> None:
    with session_scope() as session:
        enqueue_job(session, "system.echo", {"fail": True})
    Worker(settings, isolate=False).drain()
    [crashed] = json_logs.named("job crashed")
    assert crashed["status"] == "failed"
    assert "duration_ms" in crashed


def test_llm_call_is_logged_with_usage_and_cost_but_no_prompt(
    settings: Settings, json_logs: JsonLines
) -> None:
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "served-model",
                "choices": [{"message": {"content": "SECRET-COMPLETION-TEXT"}}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
            },
        )

    client = LLMClient(
        llm_settings(settings, llm_input_cost_per_1k=0.002, llm_output_cost_per_1k=0.004),
        transport=httpx.MockTransport(respond),
    )
    client.complete([ChatMessage("user", "PRIVATE-PROMPT-TEXT")], max_tokens=100)
    [record] = [r for r in json_logs.named("operation finished") if r["operation"] == "llm.call"]
    assert record["input_tokens"] == 1000
    assert record["output_tokens"] == 500
    assert record["cost_usd"] == pytest.approx(0.004)
    assert record["model"] == "fake-model"
    assert record["endpoint_host"] == "llm.example.test"
    dumped = json.dumps(json_logs.records())
    for leaked in ("PRIVATE-PROMPT-TEXT", "SECRET-COMPLETION-TEXT", FAKE_KEY):
        assert leaked not in dumped
