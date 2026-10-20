"""Build a :class:`CodeIndex` for a repository working copy using only ``ast``.

Two phases mirror the audit stages: :func:`index_repository` (INDEXING: parse files, infer import
roots, name modules, extract symbols and entry points) and :func:`link_repository` (GRAPHING:
resolve imports, build the import graph and the best-effort call graph).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import networkx as nx

from backend.core.redaction import SENSITIVE_FILE_NAMES
from backend.indexing.ast_index import extract_symbols
from backend.indexing.call_graph import build_call_graph
from backend.indexing.entrypoints import EntrypointInfo, detect_entrypoints
from backend.indexing.import_graph import build_import_graph, to_dependency_graph
from backend.indexing.module_resolution import (
    ModuleNaming,
    RawImport,
    collect_raw_imports,
    infer_import_roots,
)
from backend.indexing.source import ParsedModule, parse_file
from backend.models.audit_schemas import CodeIndex, ModuleInfo

_TEST_PATH = re.compile(
    r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_tests?\.py$|(^|/)conftest\.py$"
)
_MAX_PYPROJECT_BYTES = 1_000_000

ProgressFn = Callable[[int, int], None]


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


@dataclass
class IndexBuild:
    root: Path
    parsed: dict[str, ParsedModule]
    naming: ModuleNaming
    raw_imports: dict[str, list[RawImport]]
    modules: dict[str, ModuleInfo]
    skipped: dict[str, str] = field(default_factory=dict)
    limits_hit: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    import_graph: nx.DiGraph = field(default_factory=nx.DiGraph)
    call_graph: nx.DiGraph = field(default_factory=nx.DiGraph)
    index: CodeIndex = field(default_factory=CodeIndex)

    @property
    def parse_errors(self) -> dict[str, str]:
        return {p: m.error for p, m in sorted(self.parsed.items()) if m.error}


def select_files(
    root: Path, python_files: Sequence[str], *, max_files: int, max_file_bytes: int | None
) -> tuple[list[str], dict[str, str], list[str]]:
    """Validate and contain candidate paths. Returns (selected, skipped->reason, limits hit)."""
    root_resolved = root.resolve()
    skipped: dict[str, str] = {}
    eligible: list[str] = []
    for raw in dict.fromkeys(python_files):
        reason = _reject_reason(root, root_resolved, raw, max_file_bytes)
        if reason:
            skipped[raw] = reason
        else:
            eligible.append(PurePosixPath(raw).as_posix())
    eligible.sort(key=lambda p: (is_test_path(p), p))
    limits: list[str] = []
    if len(eligible) > max_files:
        for path in eligible[max_files:]:
            skipped[path] = "max_python_files limit reached"
        limits.append(
            f"analyzed {max_files} of {len(eligible)} Python files (max_python_files limit)"
        )
        eligible = eligible[:max_files]
    return sorted(eligible), skipped, limits


def _reject_reason(
    root: Path, root_resolved: Path, raw: str, max_file_bytes: int | None
) -> str | None:
    pure = PurePosixPath(raw)
    if not raw or "\\" in raw or "\x00" in raw or pure.is_absolute() or ".." in pure.parts:
        return "invalid path"
    if pure.suffix != ".py":
        return "not a .py file"
    if SENSITIVE_FILE_NAMES.search(pure.as_posix()):
        return "sensitive file"
    full = root / pure
    if full.is_symlink():
        return "symlink"
    resolved = full.resolve()
    if not resolved.is_relative_to(root_resolved):
        return "outside repository root"
    if not resolved.is_file():
        return "missing"
    if max_file_bytes is not None and resolved.stat().st_size > max_file_bytes:
        return "exceeds max_file_bytes"
    return None


def _line_count(text: str) -> int:
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def index_repository(
    root: Path,
    python_files: Sequence[str],
    *,
    max_files: int,
    max_file_bytes: int | None = None,
    progress: ProgressFn | None = None,
) -> IndexBuild:
    selected, skipped, limits = select_files(
        root, python_files, max_files=max_files, max_file_bytes=max_file_bytes
    )
    parsed: dict[str, ParsedModule] = {}
    raw_imports: dict[str, list[RawImport]] = {}
    warnings: list[str] = []
    for i, path in enumerate(selected):
        module = parse_file(root, path)
        parsed[path] = module
        raw_imports[path] = collect_raw_imports(module.tree) if module.tree else []
        if module.decode_warning:
            warnings.append(f"{path}: {module.decode_warning}")
        if progress and (i % 50 == 0 or i == len(selected) - 1):
            progress(i + 1, len(selected))

    naming = ModuleNaming(selected, infer_import_roots(selected, raw_imports))
    entrypoints: dict[str, EntrypointInfo] = {}
    for path, module in parsed.items():
        module.module_name = naming.module_name(path)
        if module.tree is not None:
            entrypoints[path] = detect_entrypoints(path, module.tree, module.aliases)
    for path, info in _console_scripts(root, naming).items():
        target = entrypoints.setdefault(path, EntrypointInfo())
        for reason in info.module_reasons:
            target.add_module(reason)
        for name, reason in info.symbol_reasons.items():
            target.add_symbol(name, reason)

    modules = {path: _module_info(module, entrypoints.get(path)) for path, module in parsed.items()}
    return IndexBuild(
        root=root,
        parsed=parsed,
        naming=naming,
        raw_imports=raw_imports,
        modules=modules,
        skipped=skipped,
        limits_hit=limits,
        warnings=warnings,
    )


def _module_info(module: ParsedModule, entry: EntrypointInfo | None) -> ModuleInfo:
    entry = entry or EntrypointInfo()
    symbols = extract_symbols(module.path, module.tree) if module.tree else []
    for symbol in symbols:
        symbol.is_entrypoint = symbol.qualname in entry.symbol_reasons
    reasons = entry.all_reasons()
    return ModuleInfo(
        module_name=module.module_name,
        file_path=module.path,
        is_package=module.is_package,
        line_count=_line_count(module.source),
        symbols=symbols,
        is_entrypoint=bool(reasons),
        entrypoint_reasons=reasons,
        parse_error=module.error,
    )


def _console_scripts(root: Path, naming: ModuleNaming) -> dict[str, EntrypointInfo]:
    """``[project.scripts]`` / ``[tool.poetry.scripts]`` targets (``pkg.mod:func``)."""
    pyproject = root / "pyproject.toml"
    if pyproject.is_symlink() or not pyproject.is_file():
        return {}
    if pyproject.stat().st_size > _MAX_PYPROJECT_BYTES:
        return {}
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
    except tomllib.TOMLDecodeError:
        return {}
    project = _table(data, "project")
    tables = [
        _table(project, "scripts"),
        _table(project, "gui-scripts"),
        _table(_table(_table(data, "tool"), "poetry"), "scripts"),
    ]
    found: dict[str, EntrypointInfo] = {}
    for table in tables:
        for script, target in table.items():
            if not isinstance(target, str) or ":" not in target:
                continue
            module_name, _, attr = target.partition(":")
            file_path = naming.resolve_absolute("pyproject.toml", module_name.strip())
            if file_path is None:
                continue
            info = found.setdefault(file_path, EntrypointInfo())
            info.add_module(f"console script '{script}'")
            func = attr.strip().split(".", 1)[0]
            if func.isidentifier():
                info.add_symbol(func, f"console script '{script}'")
    return found


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def link_repository(build: IndexBuild) -> CodeIndex:
    for path, module in build.modules.items():
        refs = [ref for raw in build.raw_imports[path] for ref in build.naming.resolve(path, raw)]
        module.imports = sorted(refs, key=lambda r: (r.line, r.module, r.names))
    modules = sorted(build.modules.values(), key=lambda m: m.module_name)
    build.import_graph = build_import_graph(modules)
    build.call_graph = build_call_graph(build.parsed, build.naming)
    build.index = CodeIndex(
        modules=modules,
        graph=to_dependency_graph(build.import_graph, modules),
        parse_errors=build.parse_errors,
        import_roots=build.naming.roots,
    )
    return build.index


def build_code_index(
    root: Path,
    python_files: list[str],
    *,
    max_files: int,
    max_file_bytes: int | None = None,
) -> CodeIndex:
    """Parse (never import) the given repository-relative Python files and build the index."""
    build = index_repository(root, python_files, max_files=max_files, max_file_bytes=max_file_bytes)
    return link_repository(build)
