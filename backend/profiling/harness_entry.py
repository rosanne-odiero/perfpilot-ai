"""PerfPilot workload harness: the child-process side of profiling and benchmarking.

This file is deliberately **self-contained and stdlib-only** (``psutil`` is used when present).
It never imports from ``backend`` so it can be copied into an isolated sandbox container and run
against arbitrary code::

    python -P -s -B harness_entry.py <request.json> <result.json>

The request is a JSON object; the result is always written (atomically) as a JSON object with
``status`` ``"ok"`` or ``"error"``. Modes:

``profile``  one call under cProfile (stats dumped to ``<output_dir>/profile.prof``), then one
             call on a freshly generated input under tracemalloc.
``time``     ``warmups`` untimed calls, then ``iterations`` timed calls
             (``perf_counter_ns`` wall time and ``process_time_ns`` CPU time, separately).
``memory``   one call under tracemalloc (input generated *before* tracing starts).
``command``  run a module, script, or pytest target from the workspace under cProfile.
``cases``    call the workload once per listed case (a fixture call or a literal JSON input)
             and report each output digest or raised exception plus input mutation.

Every target (entrypoint, fixture, script, pytest path) must resolve inside ``workspace_root``.
"""

from __future__ import annotations

import cProfile
import gc
import hashlib
import importlib
import json
import math
import os
import platform
import re
import runpy
import sys
import time
import traceback
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
MODES = ("profile", "time", "memory", "command", "cases")
INVOCATION_MODES = ("single", "positional", "keyword", "none")
COMMAND_KINDS = ("module", "script", "pytest")
_DOTTED = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_PREVIEW_CHARS = 300
_DEFAULT_MAX_OUTPUT_VALUE_BYTES = 8 * 1024 * 1024
_MAX_CASES = 500
_MAX_EXCEPTION_MESSAGE_CHARS = 500


class HarnessError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------------------------
# Canonical serialization (used for input/output hashing and output comparison)
# --------------------------------------------------------------------------------------------


def canonical(value: Any) -> Any:
    """Convert ``value`` into a JSON-compatible form whose equality mirrors Python ``==`` for
    common data types: dict and set ordering is ignored, list/tuple order is kept and the
    tuple/list distinction is preserved. Unknown objects fall back to ``repr``."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return {"__float__": repr(value)}
        return value
    if isinstance(value, list):
        return [canonical(item) for item in value]
    if isinstance(value, tuple):
        return {"__tuple__": [canonical(item) for item in value]}
    if isinstance(value, dict):
        pairs = [[canonical(k), canonical(v)] for k, v in value.items()]
        pairs.sort(key=lambda pair: _dumps(pair[0]))
        return {"__dict__": pairs}
    if isinstance(value, (set, frozenset)):
        items = [canonical(item) for item in value]
        items.sort(key=_dumps)
        return {"__set__": items}
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes__": bytes(value).hex()}
    return {"__repr__": repr(value)}


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_digest(value: Any) -> tuple[str, Any, bool]:
    """Return ``(sha256, canonical_value, fully_canonical)``."""
    canon = canonical(value)
    encoded = _dumps(canon)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), canon, "__repr__" not in encoded


def preview(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "...[truncated]"


# --------------------------------------------------------------------------------------------
# Target resolution (containment inside the workspace)
# --------------------------------------------------------------------------------------------


def _workspace_root(request: dict[str, Any]) -> Path:
    raw = request.get("workspace_root")
    if not isinstance(raw, str) or not raw:
        raise HarnessError("bad_request", "workspace_root is required")
    root = Path(raw)
    if not root.is_absolute():
        raise HarnessError("bad_request", "workspace_root must be absolute")
    root = root.resolve()
    if not root.is_dir():
        raise HarnessError("bad_request", "workspace_root is not a directory")
    return root


def _contained(root: Path, path: Path, what: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise HarnessError("outside_workspace", f"{what} resolves outside the workspace")
    return resolved


def _check_dotted(name: str, what: str) -> list[str]:
    if not _DOTTED.match(name):
        raise HarnessError("invalid_target", f"{what} {name!r} is not a dotted identifier")
    parts = name.split(".")
    if any(part.startswith("__") for part in parts):
        raise HarnessError("invalid_target", f"{what} {name!r} may not use dunder names")
    return parts


def module_path(root: Path, module: str) -> Path:
    """Locate ``module`` as a file under ``root`` without searching ``sys.path``."""
    parts = _check_dotted(module, "module")
    for depth in range(1, len(parts)):
        package_init = root.joinpath(*parts[:depth], "__init__.py")
        if package_init.exists():
            _contained(root, package_init, f"package {'.'.join(parts[:depth])}")
    base = root.joinpath(*parts)
    for candidate in (base.parent / f"{parts[-1]}.py", base / "__init__.py"):
        if candidate.is_file():
            return _contained(root, candidate, f"module {module}")
    raise HarnessError("module_not_found", f"module {module!r} not found in workspace")


def load_callable(root: Path, target: str, what: str) -> Any:
    if not isinstance(target, str) or target.count(":") != 1:
        raise HarnessError("invalid_target", f"{what} must look like 'module:function'")
    module_name, attribute = target.split(":")
    module_path(root, module_name)
    attr_parts = _check_dotted(attribute, f"{what} attribute")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    module = importlib.import_module(module_name)
    module_file = getattr(module, "__file__", None)
    if not module_file:
        raise HarnessError("outside_workspace", f"{what} module has no source file")
    _contained(root, Path(module_file), f"{what} module (possibly shadowed by an installed one)")
    obj: Any = module
    for part in attr_parts:
        if not hasattr(obj, part):
            raise HarnessError("invalid_target", f"{what} {target!r} does not exist")
        obj = getattr(obj, part)
    if not callable(obj):
        raise HarnessError("invalid_target", f"{what} {target!r} is not callable")
    return obj


# --------------------------------------------------------------------------------------------
# Workload invocation
# --------------------------------------------------------------------------------------------


class Workload:
    def __init__(self, request: dict[str, Any], root: Path) -> None:
        self.function = load_callable(root, request.get("entrypoint", ""), "entrypoint")
        self.fixture = load_callable(root, request.get("fixture", ""), "fixture")
        fixture_kwargs = request.get("fixture_kwargs", {})
        if not isinstance(fixture_kwargs, dict):
            raise HarnessError("bad_request", "fixture_kwargs must be an object")
        self.fixture_kwargs = fixture_kwargs
        mode = request.get("invocation_mode", "single")
        if mode not in INVOCATION_MODES:
            raise HarnessError("bad_request", f"invocation_mode must be one of {INVOCATION_MODES}")
        self.invocation_mode = mode

    def make_input(self, fixture_kwargs: dict[str, Any] | None = None) -> Any:
        data = self.fixture(**(self.fixture_kwargs if fixture_kwargs is None else fixture_kwargs))
        return self.check_shape(data)

    def check_shape(self, data: Any) -> Any:
        if self.invocation_mode == "positional" and not isinstance(data, (list, tuple)):
            raise HarnessError("bad_fixture", "positional invocation needs a list/tuple input")
        if self.invocation_mode == "keyword" and not isinstance(data, dict):
            raise HarnessError("bad_fixture", "keyword invocation needs a dict input")
        return data

    def call(self, data: Any) -> Any:
        if self.invocation_mode == "keyword":
            return self.function(**data)
        if self.invocation_mode == "positional":
            return self.function(*data)
        if self.invocation_mode == "none":
            return self.function()
        return self.function(data)


def _output_summary(request: dict[str, Any], output: Any) -> dict[str, Any]:
    digest, canon, exact = canonical_digest(output)
    summary: dict[str, Any] = {
        "output_sha256": digest,
        "output_canonical": exact,
        "output_type": type(output).__name__,
        "output_preview": preview(output),
    }
    if request.get("include_output_value"):
        limit = int(request.get("max_output_value_bytes", _DEFAULT_MAX_OUTPUT_VALUE_BYTES))
        encoded = _dumps(canon)
        if len(encoded.encode("utf-8")) <= limit:
            summary["output_value"] = canon
        else:
            summary["output_value_omitted"] = f"canonical output exceeds {limit} bytes"
    return summary


def _input_digest(data: Any) -> str:
    return canonical_digest(data)[0]


def _output_dir(request: dict[str, Any]) -> Path:
    raw = request.get("output_dir")
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise HarnessError("bad_request", "output_dir must be an absolute path")
    path = Path(raw).resolve()
    if not path.is_dir():
        raise HarnessError("bad_request", "output_dir does not exist")
    return path


def _traced_call(workload: Workload, data: Any) -> dict[str, Any]:
    """Call once under tracemalloc. ``data`` is created before tracing starts, so the reported
    numbers cover only allocations made by the workload call itself."""
    gc.collect()
    tracemalloc.start()
    try:
        workload.call(data)
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {"tracemalloc_peak_bytes": peak, "tracemalloc_retained_bytes": current}


def run_profile(request: dict[str, Any], root: Path) -> dict[str, Any]:
    workload = Workload(request, root)
    out_dir = _output_dir(request)
    data = workload.make_input()
    input_sha = _input_digest(data)
    profiler = cProfile.Profile()
    wall0, cpu0 = time.perf_counter_ns(), time.process_time_ns()
    profiler.enable()
    output = workload.call(data)
    profiler.disable()
    wall1, cpu1 = time.perf_counter_ns(), time.process_time_ns()
    prof_path = out_dir / "profile.prof"
    profiler.dump_stats(str(prof_path))
    result: dict[str, Any] = {
        "input_sha256": input_sha,
        "profile": {
            "prof_file": prof_path.name,
            "wall_ns_under_profiler": wall1 - wall0,
            "cpu_ns_under_profiler": cpu1 - cpu0,
        },
    }
    result.update(_output_summary(request, output))
    del output
    result["memory"] = _traced_call(workload, workload.make_input())
    return result


def run_time(request: dict[str, Any], root: Path) -> dict[str, Any]:
    workload = Workload(request, root)
    warmups = int(request.get("warmups", 0))
    iterations = int(request.get("iterations", 1))
    if warmups < 0 or iterations < 1:
        raise HarnessError("bad_request", "warmups must be >= 0 and iterations >= 1")
    data = workload.make_input()
    input_sha = _input_digest(data)
    for _ in range(warmups):
        workload.call(data)
    wall: list[int] = []
    cpu: list[int] = []
    output: Any = None
    for _ in range(iterations):
        output = None
        wall0, cpu0 = time.perf_counter_ns(), time.process_time_ns()
        output = workload.call(data)
        cpu1, wall1 = time.process_time_ns(), time.perf_counter_ns()
        wall.append(wall1 - wall0)
        cpu.append(cpu1 - cpu0)
    result: dict[str, Any] = {
        "input_sha256": input_sha,
        "timing": {
            "warmups": warmups,
            "iterations": iterations,
            "wall_ns": wall,
            "cpu_ns": cpu,
            "gc_enabled": gc.isenabled(),
            "clock": "time.perf_counter_ns / time.process_time_ns",
        },
    }
    result.update(_output_summary(request, output))
    if request.get("check_input_mutation", True):
        result["input_mutated"] = _input_digest(data) != input_sha
    return result


def run_memory(request: dict[str, Any], root: Path) -> dict[str, Any]:
    workload = Workload(request, root)
    data = workload.make_input()
    input_sha = _input_digest(data)
    result: dict[str, Any] = {"input_sha256": input_sha, "memory": _traced_call(workload, data)}
    return result


def _case_input(workload: Workload, case: dict[str, Any]) -> Any:
    kind = case.get("kind")
    if kind == "fixture":
        kwargs = case.get("fixture_kwargs")
        if not isinstance(kwargs, dict):
            raise HarnessError("bad_request", "fixture cases need a fixture_kwargs object")
        return workload.make_input(kwargs)
    if kind == "literal":
        return workload.check_shape(case.get("input"))
    raise HarnessError("bad_request", "case.kind must be 'fixture' or 'literal'")


def _run_case(workload: Workload, case: Any, request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(case, dict) or not isinstance(case.get("id"), str):
        raise HarnessError("bad_request", "every case needs a string id")
    entry: dict[str, Any] = {"id": case["id"]}
    try:
        data = _case_input(workload, case)
    except HarnessError as exc:
        if exc.code == "bad_request":
            raise
        return {**entry, "status": "bad_case", "error": str(exc)}
    input_sha = _input_digest(data)
    entry["input_sha256"] = input_sha
    try:
        output = workload.call(data)
    except (Exception, SystemExit) as exc:  # the workload's own exceptions are the result
        entry["status"] = "exception"
        entry["exception"] = {
            "type": type(exc).__name__,
            "module": type(exc).__module__,
            "message": str(exc)[:_MAX_EXCEPTION_MESSAGE_CHARS],
        }
    else:
        entry["status"] = "ok"
        entry.update(_output_summary(request, output))
    try:
        entry["input_mutated"] = _input_digest(data) != input_sha
    except Exception:  # an exotic input may not survive re-hashing; report it as unknown
        entry["input_mutated"] = None
    return entry


def run_cases(request: dict[str, Any], root: Path) -> dict[str, Any]:
    workload = Workload(request, root)
    cases = request.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > _MAX_CASES:
        raise HarnessError("bad_request", f"cases must be a list of 1..{_MAX_CASES} objects")
    return {"cases": [_run_case(workload, case, request) for case in cases]}


def _command_argv(command: dict[str, Any]) -> tuple[str, str, list[str]]:
    kind = command.get("kind")
    target = command.get("target")
    args = command.get("args", [])
    if kind not in COMMAND_KINDS:
        raise HarnessError("bad_request", f"command.kind must be one of {COMMAND_KINDS}")
    if not isinstance(target, str) or not target or "\x00" in target:
        raise HarnessError("bad_request", "command.target must be a non-empty string")
    if not isinstance(args, list) or not all(isinstance(a, str) and "\x00" not in a for a in args):
        raise HarnessError("bad_request", "command.args must be a list of strings")
    return kind, target, args


def _workspace_file(root: Path, relative: str, what: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise HarnessError(
            "outside_workspace", f"{what} must be a relative path inside the workspace"
        )
    path = _contained(root, root / candidate, what)
    if not path.exists():
        raise HarnessError("module_not_found", f"{what} {relative!r} does not exist")
    return path


def _module_runner(target: str, args: list[str]) -> Callable[[], int]:
    def run() -> int:
        sys.argv = [target, *args]
        runpy.run_module(target, run_name="__main__", alter_sys=True)
        return 0

    return run


def _script_runner(script: Path, args: list[str]) -> Callable[[], int]:
    def run() -> int:
        sys.argv = [str(script), *args]
        runpy.run_path(str(script), run_name="__main__")
        return 0

    return run


def _pytest_runner(target: str, args: list[str]) -> Callable[[], int]:
    try:
        import pytest
    except ImportError as exc:
        raise HarnessError("pytest_unavailable", "pytest is not installed") from exc

    def run() -> int:
        return int(pytest.main([target, *args, "-p", "no:cacheprovider"]))

    return run


def run_command(request: dict[str, Any], root: Path) -> dict[str, Any]:
    command = request.get("command")
    if not isinstance(command, dict):
        raise HarnessError("bad_request", "command mode needs a 'command' object")
    kind, target, args = _command_argv(command)
    out_dir = _output_dir(request)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    runner: Callable[[], int]
    if kind == "module":
        module_path(root, target)
        runner = _module_runner(target, args)
    elif kind == "script":
        runner = _script_runner(_workspace_file(root, target, "script"), args)
    else:
        path_part, sep, node = target.partition("::")
        test_path = _workspace_file(root, path_part, "pytest target")
        runner = _pytest_runner(f"{test_path}{sep}{node}", args)

    profiler = cProfile.Profile()
    exit_code = 0
    wall0, cpu0 = time.perf_counter_ns(), time.process_time_ns()
    profiler.enable()
    try:
        exit_code = runner()
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        profiler.disable()
    wall1, cpu1 = time.perf_counter_ns(), time.process_time_ns()
    prof_path = out_dir / "profile.prof"
    profiler.dump_stats(str(prof_path))
    return {
        "command": {"kind": kind, "target": target, "args": args, "exit_code": exit_code},
        "profile": {
            "prof_file": prof_path.name,
            "wall_ns_under_profiler": wall1 - wall0,
            "cpu_ns_under_profiler": cpu1 - cpu0,
        },
    }


# --------------------------------------------------------------------------------------------
# Process information and entry point
# --------------------------------------------------------------------------------------------


def _process_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "pid": os.getpid(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "executable": sys.executable,
        "platform": platform.platform(),
    }
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        scale = 1 if sys.platform == "darwin" else 1024
        info["ru_maxrss_bytes"] = int(usage.ru_maxrss) * scale
        info["ru_utime_s"] = usage.ru_utime
        info["ru_stime_s"] = usage.ru_stime
    except ImportError:
        info["ru_maxrss_bytes"] = None
    try:
        import psutil

        info["rss_at_exit_bytes"] = psutil.Process().memory_info().rss
    except ImportError:
        info["rss_at_exit_bytes"] = None
    return info


_DISPATCH = {
    "profile": run_profile,
    "time": run_time,
    "memory": run_memory,
    "command": run_command,
    "cases": run_cases,
}


def execute(request: dict[str, Any]) -> dict[str, Any]:
    mode = request.get("mode")
    if mode not in MODES:
        raise HarnessError("bad_request", f"mode must be one of {MODES}")
    root = _workspace_root(request)
    return _DISPATCH[mode](request, root)


def _write_result(path: Path, result: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write("usage: harness_entry.py <request.json> <result.json>\n")
        return 2
    request_path, result_path = Path(argv[1]), Path(argv[2])
    result: dict[str, Any] = {"protocol_version": PROTOCOL_VERSION, "status": "error"}
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise HarnessError("bad_request", "request must be a JSON object")
        result["mode"] = request.get("mode")
        result.update(execute(request))
        result["status"] = "ok"
    except HarnessError as exc:
        result["error"] = {"type": "HarnessError", "code": exc.code, "message": str(exc)}
    except BaseException as exc:  # user code may raise anything, including SystemExit
        result["error"] = {
            "type": type(exc).__name__,
            "code": "workload_exception",
            "message": str(exc)[:2000],
            "traceback": traceback.format_exc()[-8000:],
        }
    result["process"] = _process_info()
    _write_result(result_path, result)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
