"""Static discovery of test/benchmark commands for a Runtime Audit.

Nothing here executes repository code: configuration is read with ``tomllib``/``configparser``/
``json`` and Python files are parsed with ``ast``. Each result is a *proposal* (provenance
``STATIC``) that a user must explicitly approve before it can run in the sandbox.

Runnable proposals are restricted to three argv shapes (see :func:`validate_command_argv`):

* ``python -m pytest [paths inside the workspace] [-k EXPR] [-x] [-q]``
* ``python <relative .py path inside the workspace>``
* ``python -m <module that exists inside the workspace>``

tox/nox environments, Makefile targets and asv suites are reported as information only; PerfPilot
never runs ``tox``, ``nox``, ``make`` or ``asv``.
"""

from __future__ import annotations

import ast
import configparser
import fnmatch
import hashlib
import json
import keyword
import os
import posixpath
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.core.exceptions import PerfPilotError, SecurityViolationError, ValidationFailedError
from backend.core.redaction import redact_text
from backend.models.audit_schemas import RepositoryInventory
from backend.models.enums import Provenance
from backend.sandbox.paths import resolve_inside, safe_relative_path

CommandKind = Literal["pytest", "pytest_benchmark", "script", "module", "asv", "info_only"]
RiskLevel = Literal["low", "medium", "high"]

EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "build",
        "dist",
        "site-packages",
        ".next",
        ".eggs",
        "htmlcov",
        ".idea",
        ".vscode",
        ".asv",
    }
)
TEST_DIR_NAMES = frozenset({"test", "tests", "testing"})
SCRIPT_DIR_NAMES = frozenset(
    {"benchmarks", "benchmark", "bench", "benches", "perf", "performance", "profiling", "scripts"}
)
MAKE_KEYWORDS = frozenset(
    {"test", "tests", "bench", "benchmark", "benchmarks", "perf", "performance", "profile"}
    | {"profiling"}
)
PROJECT_MARKERS = frozenset(
    {"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "requirements.txt"}
)
# Importable inside the sandbox image without a dependency install.
PREINSTALLED_MODULES = frozenset(
    {"pytest", "_pytest", "pytest_benchmark", "psutil", "pluggy", "iniconfig", "packaging"}
    | {"cpuinfo", "py"}
)
HIGH_RISK_MODULES = frozenset(
    {
        "requests",
        "httpx",
        "urllib3",
        "aiohttp",
        "boto3",
        "botocore",
        "psycopg2",
        "psycopg",
        "asyncpg",
        "pymongo",
        "motor",
        "redis",
        "sqlalchemy",
        "smtplib",
        "paramiko",
        "ftplib",
        "openai",
        "anthropic",
        "stripe",
        "supabase",
        "firebase_admin",
        "google",
        "azure",
        "kubernetes",
        "docker",
        "pika",
        "kafka",
        "celery",
    }
)
MEDIUM_RISK_MODULES = frozenset(
    {"subprocess", "shutil", "socket", "ctypes", "multiprocessing", "urllib", "http", "signal"}
)

MAX_FILES = 50_000
MAX_READ_BYTES = 1024 * 1024
MAX_CANDIDATES = 100
MAX_PATH_ARGS = 20
MAX_EVIDENCE = 10

_TEST_FILE = re.compile(r"^(test_.*|.*_test)\.py$")
_K_EXPR = re.compile(r"^[A-Za-z0-9_ .\[\]()\-]{1,200}$")
_NODE_PART = re.compile(r"^[A-Za-z0-9_.\-\[\]]{1,200}$")
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*:(?![=:])")
_PYTEST_FLAGS = frozenset({"-q", "-x"})


class CandidateCommand(BaseModel):
    id: str = Field(description="Stable hash of kind, workdir and argv")
    kind: CommandKind
    argv: list[str] = Field(
        description="Starts with 'python'; empty for information-only entries PerfPilot never runs"
    )
    workdir_rel: str
    description: str
    evidence: list[str] = Field(description="file:line references")
    requires_dependencies: bool
    third_party_imports: list[str] = Field(default_factory=list)
    estimated_risk: RiskLevel
    runnable: bool
    reason_if_not_runnable: str | None = None
    provenance: Provenance = Provenance.STATIC


class DiscoveryReport(BaseModel):
    commands: list[CandidateCommand]
    files_scanned: int
    limits_hit: list[str] = Field(default_factory=list)


class CommandRejectedError(ValidationFailedError):
    code = "command_rejected"


# --------------------------------------------------------------------------------------
# argv policy
# --------------------------------------------------------------------------------------


def validate_command_argv(argv: list[str], *, workspace_dir: Path, workdir_rel: str) -> None:
    """Raise unless ``argv`` is one of the three allowed shapes, confined to the workspace."""
    if not argv or argv[0] != "python":
        raise CommandRejectedError("commands must start with 'python'")
    if any(not isinstance(a, str) or "\0" in a or len(a) > 4096 for a in argv):
        raise CommandRejectedError("argv items must be strings without NUL bytes")
    workspace = workspace_dir.resolve()
    workdir = (
        workspace if workdir_rel == "." else resolve_inside(workspace, workdir_rel, what="dir")
    )
    if not workdir.is_dir():
        raise CommandRejectedError("working directory does not exist in the workspace")
    if len(argv) >= 3 and argv[1:3] == ["-m", "pytest"]:
        _validate_pytest_args(argv[3:], workspace, workdir)
    elif len(argv) == 3 and argv[1] == "-m":
        _validate_module(argv[2], workspace, workdir)
    elif len(argv) == 2 and not argv[1].startswith("-"):
        target = _contained_path(argv[1], workspace, workdir)
        if not argv[1].endswith(".py") or not target.is_file():
            raise CommandRejectedError(f"script must be an existing .py file: {argv[1]!r}")
    else:
        raise CommandRejectedError(
            "only 'python -m pytest ...', 'python <script.py>' and 'python -m <module>' are allowed"
        )


def _contained_path(value: str, workspace: Path, workdir: Path) -> Path:
    rel = safe_relative_path(value, what="path")
    target = (workdir / rel).resolve()
    if not target.is_relative_to(workspace):
        raise SecurityViolationError(f"path escapes the workspace: {value!r}")
    return target


def _validate_pytest_args(args: list[str], workspace: Path, workdir: Path) -> None:
    paths = 0
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in _PYTEST_FLAGS:
            index += 1
            continue
        if arg == "-k":
            if index + 1 >= len(args):
                raise CommandRejectedError("-k requires an expression")
            expr = args[index + 1]
            if expr.startswith("-") or not _K_EXPR.match(expr):
                raise CommandRejectedError(f"unsupported -k expression: {expr!r}")
            index += 2
            continue
        if arg.startswith("-"):
            raise CommandRejectedError(f"pytest option not allowed: {arg!r}")
        path_part, *node_parts = arg.split("::")
        if any(not _NODE_PART.match(part) for part in node_parts):
            raise CommandRejectedError(f"invalid pytest node id: {arg!r}")
        if not _contained_path(path_part, workspace, workdir).exists():
            raise CommandRejectedError(f"test path does not exist: {path_part!r}")
        paths += 1
        if paths > 50:
            raise CommandRejectedError("too many test paths")
        index += 1


def _validate_module(module: str, workspace: Path, workdir: Path) -> None:
    parts = module.split(".")
    if len(parts) > 10 or any(not p.isidentifier() or keyword.iskeyword(p) for p in parts):
        raise CommandRejectedError(f"invalid module name: {module!r}")
    base = "/".join(parts)
    for rel in (f"{base}.py", f"{base}/__main__.py"):
        target = _contained_path(rel, workspace, workdir)
        if target.is_file():
            return
    raise CommandRejectedError(f"module {module!r} is not a runnable module inside the workspace")


# --------------------------------------------------------------------------------------
# Workspace scanning
# --------------------------------------------------------------------------------------


@dataclass
class _PyInfo:
    imports: set[str] = field(default_factory=set)
    has_main_guard: bool = False
    benchmark_tests: list[tuple[str, int]] = field(default_factory=list)
    defines_benchmark_fixture: bool = False
    uses_unittest: bool = False
    uses_pytest: bool = False
    nox_sessions: list[tuple[str, int]] = field(default_factory=list)


def _parent(rel: str) -> str:
    parent = PurePosixPath(rel).parent.as_posix()
    return parent if parent else "."


def _ancestors(directory: str) -> list[str]:
    """``directory`` and all of its ancestors, deepest first, ending with ``"."``."""
    out = []
    current = PurePosixPath(directory)
    while current.as_posix() not in (".", ""):
        out.append(current.as_posix())
        current = current.parent
    out.append(".")
    return out


def _depth(directory: str) -> int:
    return 0 if directory == "." else directory.count("/") + 1


def _under(rel: str, directory: str) -> bool:
    return directory == "." or rel == directory or rel.startswith(directory + "/")


def _relative(rel: str, base: str) -> str:
    return rel if base == "." else PurePosixPath(rel).relative_to(base).as_posix()


def _is_main_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    sides = [test.left, test.comparators[0]]
    names = [s.id for s in sides if isinstance(s, ast.Name)]
    consts = [s.value for s in sides if isinstance(s, ast.Constant)]
    return names == ["__name__"] and consts == ["__main__"]


def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _analyze_python(source: str) -> _PyInfo:
    info = _PyInfo()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return info
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            info.imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            info.imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            decorators = {_decorator_name(d) for d in node.decorator_list}
            arg_names = {a.arg for a in [*node.args.args, *node.args.kwonlyargs]}
            if node.name.startswith("test") and "benchmark" in arg_names:
                info.benchmark_tests.append((node.name, node.lineno))
            if node.name == "benchmark" and decorators & {"pytest.fixture", "fixture"}:
                info.defines_benchmark_fixture = True
            if decorators & {"nox.session", "session"}:
                info.nox_sessions.append((node.name, node.lineno))
    info.has_main_guard = any(_is_main_guard(stmt) for stmt in tree.body)
    info.uses_unittest = "unittest" in info.imports
    info.uses_pytest = "pytest" in info.imports
    return info


class _Workspace:
    def __init__(self, root: Path, inventory: RepositoryInventory | None) -> None:
        self.root = root.resolve()
        self.limits_hit: list[str] = []
        self.files = self._list_files(inventory)
        self.file_set = set(self.files)
        self._py_cache: dict[str, _PyInfo] = {}
        self.python_files = [f for f in self.files if f.endswith(".py")]
        self.local_names = {
            PurePosixPath(part).stem for f in self.python_files for part in f.split("/")
        }
        self.project_roots = {
            _parent(f)
            for f in self.files
            if PurePosixPath(f).name in PROJECT_MARKERS
            or fnmatch.fnmatch(PurePosixPath(f).name, "requirements*.txt")
        }

    @staticmethod
    def _excluded(rel: str) -> bool:
        return any(
            part in EXCLUDED_DIRS or part.endswith(".egg-info")
            for part in PurePosixPath(rel).parts[:-1]
        )

    def _list_files(self, inventory: RepositoryInventory | None) -> list[str]:
        if inventory is not None:
            files = [f.path for f in inventory.files if not f.skipped]
            files = [f for f in files if not self._excluded(f)]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
                dirnames[:] = sorted(
                    d for d in dirnames if d not in EXCLUDED_DIRS and not d.endswith(".egg-info")
                )
                base = Path(dirpath)
                for name in sorted(filenames):
                    if (base / name).is_symlink():
                        continue
                    files.append((base / name).relative_to(self.root).as_posix())
                if len(files) > MAX_FILES:
                    break
        if len(files) > MAX_FILES:
            self.limits_hit.append(f"only the first {MAX_FILES} files were scanned")
            files = files[:MAX_FILES]
        return sorted(files)

    def read(self, rel: str) -> str | None:
        try:
            path = resolve_inside(self.root, rel, what="file")
        except PerfPilotError:
            return None
        if (self.root / rel).is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_READ_BYTES:
            self.limits_hit.append(f"{rel} larger than {MAX_READ_BYTES} bytes was not read")
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def py(self, rel: str) -> _PyInfo:
        if rel not in self._py_cache:
            text = self.read(rel)
            self._py_cache[rel] = _analyze_python(text) if text is not None else _PyInfo()
        return self._py_cache[rel]

    def nearest_project_root(self, directory: str) -> str:
        for candidate in _ancestors(directory):
            if candidate in self.project_roots:
                return candidate
        return "."

    def third_party(self, files: list[str]) -> list[str]:
        imports: set[str] = set()
        for rel in files:
            imports |= self.py(rel).imports
        stdlib = set(sys.stdlib_module_names) | {"__future__"}
        return sorted(imports - stdlib - PREINSTALLED_MODULES - self.local_names)

    def imports(self, files: list[str]) -> set[str]:
        result: set[str] = set()
        for rel in files:
            result |= self.py(rel).imports
        return result


def _line_of(text: str, needle: str) -> int:
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    return 1


def _read_ini(text: str) -> configparser.ConfigParser | None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    return parser


def _read_toml(text: str) -> dict[str, Any] | None:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _split_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [v for v in re.split(r"[\s,]+", value) if v]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _risk(imports: set[str], *, floor: RiskLevel = "low") -> RiskLevel:
    if imports & HIGH_RISK_MODULES:
        return "high"
    if imports & MEDIUM_RISK_MODULES or floor == "medium":
        return "medium"
    return floor


def _command_id(kind: str, workdir: str, argv: list[str], evidence: list[str]) -> str:
    payload = json.dumps([kind, workdir, argv, [] if argv else evidence[:1]])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class _PytestConfig:
    directory: str
    evidence: str
    testpaths: list[str]
    python_files: list[str]


@dataclass
class _Discovery:
    ws: _Workspace
    commands: list[CandidateCommand] = field(default_factory=list)

    def add(
        self,
        kind: CommandKind,
        argv: list[str],
        workdir: str,
        description: str,
        evidence: list[str],
        *,
        files: list[str] | None = None,
        risk_floor: RiskLevel = "low",
        requires_dependencies: bool | None = None,
        reason: str | None = None,
    ) -> None:
        files = files or []
        third_party = self.ws.third_party(files)
        needs_deps = bool(third_party) if requires_dependencies is None else requires_dependencies
        floor: RiskLevel = "medium" if needs_deps and risk_floor == "low" else risk_floor
        runnable = reason is None and bool(argv)
        if runnable:
            try:
                validate_command_argv(argv, workspace_dir=self.ws.root, workdir_rel=workdir)
            except PerfPilotError as exc:
                runnable, reason = False, exc.message
        self.commands.append(
            CandidateCommand(
                id=_command_id(kind, workdir, argv, evidence),
                kind=kind,
                argv=argv,
                workdir_rel=workdir,
                description=description,
                evidence=evidence[:MAX_EVIDENCE],
                requires_dependencies=needs_deps,
                third_party_imports=third_party[:20],
                estimated_risk=_risk(self.ws.imports(files), floor=floor),
                runnable=runnable,
                reason_if_not_runnable=None if runnable else reason,
            )
        )


# --------------------------------------------------------------------------------------
# Detectors
# --------------------------------------------------------------------------------------


def _pytest_configs(ws: _Workspace) -> list[_PytestConfig]:
    by_dir: dict[str, _PytestConfig] = {}
    # pytest precedence within one directory: pytest.ini > pyproject.toml > tox.ini > setup.cfg.
    for name in ("setup.cfg", "tox.ini", "pyproject.toml", ".pytest.ini", "pytest.ini"):
        for rel in (f for f in ws.files if PurePosixPath(f).name == name):
            text = ws.read(rel)
            if text is None:
                continue
            options: dict[str, Any] | None = None
            marker = ""
            if name == "pyproject.toml":
                data = _read_toml(text) or {}
                section = data.get("tool", {}).get("pytest", {}).get("ini_options")
                options, marker = (section, "[tool.pytest.ini_options]")
            else:
                section_name = {"setup.cfg": "tool:pytest"}.get(name, "pytest")
                parser = _read_ini(text)
                if parser is not None and parser.has_section(section_name):
                    options, marker = dict(parser[section_name]), f"[{section_name}]"
                elif name.endswith("pytest.ini"):
                    options, marker = {}, ""
            if not isinstance(options, dict):
                continue
            directory = _parent(rel)
            line = _line_of(text, marker) if marker else 1
            by_dir[directory] = _PytestConfig(
                directory=directory,
                evidence=f"{rel}:{line}",
                testpaths=_split_list(options.get("testpaths")),
                python_files=_split_list(options.get("python_files")),
            )
    return sorted(by_dir.values(), key=lambda c: c.directory)


def _is_test_file(rel: str, patterns: list[str]) -> bool:
    name = PurePosixPath(rel).name
    if patterns:
        return any(fnmatch.fnmatch(name, p) for p in patterns)
    return bool(_TEST_FILE.match(name))


def _conftests(ws: _Workspace, workdir: str, files: list[str]) -> list[str]:
    dirs = {d for f in files for d in _ancestors(_parent(f)) if _under(d, workdir)}
    return sorted(
        f
        for f in ws.files
        if PurePosixPath(f).name == "conftest.py" and _parent(f) in (dirs | {workdir})
    )


def _in_testpaths(rel: str, config: _PytestConfig) -> bool:
    if not config.testpaths:
        return True
    for testpath in config.testpaths:
        joined = testpath if config.directory == "." else f"{config.directory}/{testpath}"
        normalized = posixpath.normpath(joined)
        if _under(rel, normalized) or fnmatch.fnmatch(rel, normalized):
            return True
    return False


def _test_root(rel: str) -> str:
    """Topmost ancestor directory named like a test dir, else the file's own directory."""
    parts = PurePosixPath(rel).parts[:-1]
    for index, part in enumerate(parts):
        if part in TEST_DIR_NAMES:
            return "/".join(parts[: index + 1])
    return _parent(rel)


def _discover_pytest(d: _Discovery) -> None:
    ws = d.ws
    configs = _pytest_configs(ws)

    def owning_config(rel: str) -> _PytestConfig | None:
        owners = [c for c in configs if _under(rel, c.directory)]
        return max(owners, key=lambda c: _depth(c.directory)) if owners else None

    groups: dict[tuple[str, str], list[str]] = {}
    config_for_group: dict[tuple[str, str], _PytestConfig] = {}
    for rel in ws.python_files:
        config = owning_config(rel)
        patterns = config.python_files if config else []
        if not _is_test_file(rel, patterns):
            continue
        if config is not None:
            if not _in_testpaths(rel, config):
                continue
            key = (config.directory, ".")
            config_for_group[key] = config
        else:
            root = _test_root(rel)
            workdir = ws.nearest_project_root(root)
            key = (workdir, _relative(root, workdir) if root != workdir else ".")
        groups.setdefault(key, []).append(rel)

    for (workdir, path_arg), files in sorted(groups.items()):
        config = config_for_group.get((workdir, path_arg))
        conftests = _conftests(ws, workdir, files)
        scope = files + conftests
        argv = ["python", "-m", "pytest", "-q"] + ([] if path_arg == "." else [path_arg])
        evidence = ([config.evidence] if config else []) + [f"{c}:1" for c in conftests[:3]]
        evidence += [f"{f}:1" for f in files[:5]]
        infos = [ws.py(f) for f in files]
        style = (
            "unittest-style tests (collected by pytest)"
            if all(i.uses_unittest and not i.uses_pytest for i in infos)
            else "pytest test suite"
        )
        where = f"configured in {config.evidence.split(':')[0]}" if config else "no pytest config"
        d.add(
            "pytest",
            argv,
            workdir,
            f"{style}: {len(files)} test file(s), {where}",
            evidence,
            files=scope,
        )
        _discover_benchmarks(d, workdir, path_arg, files, conftests)


def _discover_benchmarks(
    d: _Discovery, workdir: str, path_arg: str, files: list[str], conftests: list[str]
) -> None:
    ws = d.ws
    if any(ws.py(f).defines_benchmark_fixture for f in files + conftests):
        return  # a project-defined ``benchmark`` fixture, not pytest-benchmark
    bench_files = [f for f in files if ws.py(f).benchmark_tests]
    if not bench_files:
        return
    if len(bench_files) <= MAX_PATH_ARGS:
        targets = [_relative(f, workdir) for f in bench_files]
    else:
        targets = [] if path_arg == "." else [path_arg]
    evidence = [f"{f}:{line}" for f in bench_files for _, line in ws.py(f).benchmark_tests]
    count = sum(len(ws.py(f).benchmark_tests) for f in bench_files)
    d.add(
        "pytest_benchmark",
        ["python", "-m", "pytest", "-q", *targets],
        workdir,
        f"pytest-benchmark: {count} test(s) using the 'benchmark' fixture in "
        f"{len(bench_files)} file(s)",
        evidence,
        files=bench_files + conftests,
    )


def _discover_scripts(d: _Discovery) -> None:
    ws = d.ws
    for rel in ws.python_files:
        dirs = PurePosixPath(rel).parts[:-1]
        if not set(dirs) & SCRIPT_DIR_NAMES or _TEST_FILE.match(PurePosixPath(rel).name):
            continue
        info = ws.py(rel)
        workdir = ws.nearest_project_root(_parent(rel))
        floor: RiskLevel = "medium" if "scripts" in dirs else "low"
        if PurePosixPath(rel).name == "__main__.py":
            package = _relative(_parent(rel), workdir)
            module = package.replace("/", ".")
            if package == "." or not all(p.isidentifier() for p in module.split(".")):
                continue
            d.add(
                "module",
                ["python", "-m", module],
                workdir,
                f"package entry point {module}/__main__.py",
                [f"{rel}:1"],
                files=[rel],
                risk_floor=floor,
            )
        elif info.has_main_guard:
            text = ws.read(rel) or ""
            d.add(
                "script",
                ["python", _relative(rel, workdir)],
                workdir,
                f"script with a __main__ guard in {'/'.join(dirs)}/",
                [f"{rel}:{_line_of(text, '__main__')}"],
                files=[rel],
                risk_floor=floor,
            )


def _resolve_entry_module(ws: _Workspace, project_dir: str, module: str) -> tuple[str, str] | None:
    """Return (base dir, module file) for ``module`` in a flat or ``src/`` layout."""
    base_path = module.replace(".", "/")
    for base in (project_dir, f"{project_dir}/src" if project_dir != "." else "src"):
        for candidate in (f"{base_path}.py", f"{base_path}/__init__.py"):
            rel = candidate if base == "." else f"{base}/{candidate}"
            if rel in ws.file_set:
                return base, rel
    return None


def _discover_project_scripts(d: _Discovery) -> None:
    ws = d.ws
    for rel in (f for f in ws.files if PurePosixPath(f).name == "pyproject.toml"):
        text = ws.read(rel)
        data = _read_toml(text) if text else None
        if not data:
            continue
        project_dir = _parent(rel)
        entries: dict[str, object] = {}
        project_scripts = data.get("project", {}).get("scripts", {})
        poetry_scripts = data.get("tool", {}).get("poetry", {}).get("scripts", {})
        for table in (project_scripts, poetry_scripts):
            if isinstance(table, dict):
                entries.update(table)
        for name, target in sorted(entries.items()):
            reference = target.get("reference") if isinstance(target, dict) else target
            if not isinstance(reference, str) or ":" not in reference:
                continue
            module = reference.split(":", 1)[0].strip()
            evidence = [f"{rel}:{_line_of(text or '', name)}"]
            description = f"console script '{name}' -> {reference}"
            if not all(p.isidentifier() for p in module.split(".")):
                continue
            found = _resolve_entry_module(ws, project_dir, module)
            if found is None:
                d.add(
                    "info_only",
                    [],
                    project_dir,
                    description,
                    evidence,
                    reason=f"module {module!r} not found in the workspace",
                )
                continue
            base, module_file = found
            main_file = f"{_parent(module_file)}/__main__.py"
            runnable = (module_file.endswith("__init__.py") and main_file in ws.file_set) or (
                not module_file.endswith("__init__.py") and ws.py(module_file).has_main_guard
            )
            if runnable:
                d.add(
                    "module",
                    ["python", "-m", module],
                    base,
                    description,
                    [*evidence, f"{module_file}:1"],
                    files=[module_file],
                    risk_floor="medium",
                )
            else:
                d.add(
                    "info_only",
                    [],
                    base,
                    description,
                    [*evidence, f"{module_file}:1"],
                    files=[module_file],
                    reason="entry-point function needs an installed project; the module has no "
                    "__main__ guard",
                )


def _discover_asv(d: _Discovery) -> None:
    ws = d.ws
    for rel in (f for f in ws.files if PurePosixPath(f).name == "asv.conf.json"):
        text = ws.read(rel) or ""
        stripped = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("//")
        )
        try:
            config = json.loads(stripped)
        except json.JSONDecodeError:
            config = {}
        bench_dir = (
            str(config.get("benchmark_dir", "benchmarks")) if isinstance(config, dict) else ""
        )
        directory = _parent(rel)
        bench_rel = bench_dir if directory == "." else f"{directory}/{bench_dir}"
        bench_files = [f for f in ws.python_files if _under(f, bench_rel)]
        d.add(
            "asv",
            [],
            directory,
            f"airspeed velocity suite ({len(bench_files)} file(s) in {bench_dir}/)",
            [f"{rel}:1", *(f"{f}:1" for f in bench_files[:5])],
            requires_dependencies=True,
            reason="asv builds its own environments from the network; PerfPilot does not run asv",
        )


def _discover_tox_nox(d: _Discovery) -> None:
    ws = d.ws
    reason = "tox/nox are not run; PerfPilot proposes the equivalent pytest command instead"
    for rel in ws.files:
        name = PurePosixPath(rel).name
        envs: list[str] = []
        evidence: list[str] = []
        tool = "tox"
        if name in ("tox.ini", "setup.cfg"):
            text = ws.read(rel) or ""
            parser = _read_ini(text)
            if parser is None:
                continue
            header = "tox" if name == "tox.ini" else "tox:tox"
            if parser.has_section(header):
                envs += _split_list(parser[header].get("envlist", ""))
                envs += _split_list(parser[header].get("env_list", ""))
                evidence.append(f"{rel}:{_line_of(text, f'[{header}]')}")
            for section in parser.sections():
                if section.startswith("testenv:"):
                    envs.append(section.split(":", 1)[1])
                    evidence.append(f"{rel}:{_line_of(text, f'[{section}]')}")
            if name == "tox.ini" and not evidence and parser.has_section("testenv"):
                evidence.append(f"{rel}:{_line_of(text, '[testenv]')}")
        elif name == "pyproject.toml":
            text = ws.read(rel) or ""
            data = (_read_toml(text) or {}).get("tool", {}).get("tox")
            if not isinstance(data, dict):
                continue
            envs += _split_list(data.get("env_list") or data.get("envlist"))
            envs += (
                [str(k) for k in data.get("env", {})] if isinstance(data.get("env"), dict) else []
            )
            evidence.append(f"{rel}:{_line_of(text, '[tool.tox')}")
        elif name == "noxfile.py":
            tool = "nox"
            sessions = ws.py(rel).nox_sessions
            envs = [s for s, _ in sessions]
            evidence = [f"{rel}:{line}" for _, line in sessions]
        if not evidence:
            continue
        unique = list(dict.fromkeys(envs))
        listed = ", ".join(unique[:15]) or "default"
        d.add(
            "info_only",
            [],
            _parent(rel),
            f"{tool} environments: {listed}",
            evidence,
            requires_dependencies=False,
            reason=reason,
        )


def _discover_make(d: _Discovery) -> None:
    ws = d.ws
    for rel in ws.files:
        if PurePosixPath(rel).name not in ("Makefile", "makefile", "GNUmakefile"):
            continue
        lines = (ws.read(rel) or "").splitlines()
        for number, line in enumerate(lines, start=1):
            match = _MAKE_TARGET.match(line)
            if not match or match.group(1).startswith("."):
                continue
            target = match.group(1)
            if not set(re.split(r"[-_.]", target.lower())) & MAKE_KEYWORDS:
                continue
            recipe = next(
                (
                    lines[i].strip()
                    for i in range(number, min(number + 5, len(lines)))
                    if lines[i].startswith("\t") and lines[i].strip()
                ),
                "",
            )
            shown = redact_text(recipe).text[:160]
            d.add(
                "info_only",
                [],
                _parent(rel),
                f"make {target}" + (f" (runs: {shown})" if shown else ""),
                [f"{rel}:{number}"],
                requires_dependencies=False,
                reason="Makefile targets are information only; PerfPilot never runs make",
            )


_KIND_ORDER = {"pytest": 0, "pytest_benchmark": 1, "script": 2, "module": 3, "asv": 4}


def discover_commands(
    workspace_dir: Path, inventory: RepositoryInventory | None = None
) -> DiscoveryReport:
    """Propose candidate commands for ``workspace_dir`` without executing anything."""
    if not workspace_dir.is_dir():
        raise ValidationFailedError("workspace directory does not exist")
    ws = _Workspace(workspace_dir, inventory)
    discovery = _Discovery(ws)
    _discover_pytest(discovery)
    _discover_scripts(discovery)
    _discover_project_scripts(discovery)
    _discover_asv(discovery)
    _discover_tox_nox(discovery)
    _discover_make(discovery)

    unique: dict[str, CandidateCommand] = {}
    for command in discovery.commands:
        unique.setdefault(command.id, command)
    commands = sorted(
        unique.values(),
        key=lambda c: (not c.runnable, _KIND_ORDER.get(c.kind, 9), c.workdir_rel, c.argv),
    )
    limits = list(dict.fromkeys(ws.limits_hit))
    if len(commands) > MAX_CANDIDATES:
        limits.append(f"only the first {MAX_CANDIDATES} candidate commands are reported")
        commands = commands[:MAX_CANDIDATES]
    return DiscoveryReport(commands=commands, files_scanned=len(ws.files), limits_hit=limits)
