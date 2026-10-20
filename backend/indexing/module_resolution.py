"""Module naming and import resolution without executing anything.

Repositories rarely declare their ``sys.path``. Roots are inferred:

* the repository root is always a root;
* the parent of every top-level regular package (directory with ``__init__.py`` whose parent has
  none) is a root, which covers ``src/`` layouts and nested projects such as ``backend/app/...``;
* further candidate directories (directories holding stand-alone scripts, any ancestor of a Python
  file) are added greedily while they make additional absolute imports resolvable.

Every file gets one canonical module name from the deepest root that yields a unique, importable
name. Imports are resolved against all names from all chosen roots; when several files match, the
one closest to the importing file wins. Relative imports are resolved purely by path.
"""

from __future__ import annotations

import ast
import keyword
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from backend.models.audit_schemas import ImportRef

_MAX_GREEDY_ROOTS = 32


@dataclass(frozen=True)
class RawImport:
    module: str
    names: tuple[str, ...]
    level: int
    line: int
    is_from: bool


def _is_type_checking_guard(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def collect_raw_imports(tree: ast.Module) -> list[RawImport]:
    """All runtime imports (any nesting level). ``if TYPE_CHECKING:`` bodies are skipped."""
    found: list[RawImport] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and _is_type_checking_guard(node):
            for stmt in node.orelse:
                visit(stmt)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(RawImport(alias.name, (), 0, node.lineno, False))
            return
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                return
            names = tuple(a.name for a in node.names)
            found.append(RawImport(node.module or "", names, node.level, node.lineno, True))
            return
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _depth(root: str) -> int:
    return 0 if root == "" else root.count("/") + 1


def _under(root: str, path: str) -> str | None:
    """``path`` relative to ``root`` or ``None`` when it is outside."""
    if root == "":
        return path
    prefix = root + "/"
    return path[len(prefix) :] if path.startswith(prefix) else None


def _valid_parts(parts: Sequence[str]) -> bool:
    return bool(parts) and all(p.isidentifier() and not keyword.iskeyword(p) for p in parts)


def name_under_root(root: str, file_path: str) -> str | None:
    rel = _under(root, file_path)
    if rel is None or not rel.endswith(".py"):
        return None
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if _valid_parts(parts) else None


def _dir_names_under_root(root: str, file_path: str) -> list[str]:
    """Importable (namespace) package names for the directories between root and the file."""
    rel = _under(root, file_path)
    if rel is None:
        return []
    parts = rel.split("/")[:-1]
    names: list[str] = []
    for i in range(1, len(parts) + 1):
        if not _valid_parts(parts[:i]):
            break
        names.append(".".join(parts[:i]))
    return names


def _acceptable_names(raw: RawImport) -> list[str]:
    if raw.level or not raw.module:
        return []
    if not raw.is_from:
        return [raw.module]
    return [raw.module] + [f"{raw.module}.{n}" for n in raw.names if n != "*"]


def natural_roots(files: Iterable[str]) -> tuple[set[str], set[str]]:
    """(package roots, script directories)."""
    file_set = set(files)
    package_dirs = {_parent(f) for f in file_set if f.rsplit("/", 1)[-1] == "__init__.py"}
    package_dirs.discard("")
    package_roots: set[str] = set()
    script_dirs: set[str] = set()
    for f in file_set:
        directory = _parent(f)
        if directory in package_dirs:
            top = directory
            while _parent(top) in package_dirs:
                top = _parent(top)
            package_roots.add(_parent(top))
        else:
            script_dirs.add(directory)
    return package_roots, script_dirs


def infer_import_roots(files: Sequence[str], imports: dict[str, list[RawImport]]) -> list[str]:
    package_roots, script_dirs = natural_roots(files)
    package_dirs = {_parent(f) for f in files if f.rsplit("/", 1)[-1] == "__init__.py"}
    ancestors = {a for f in files for a in _ancestors(f)}
    chosen: set[str] = {""} | package_roots
    candidates = (script_dirs | ancestors) - chosen - package_dirs

    providers: dict[str, set[str]] = defaultdict(set)
    all_roots = chosen | candidates
    for f in files:
        for root in ("", *_ancestors(f)):
            if root not in all_roots:
                continue
            name = name_under_root(root, f)
            if name:
                providers[name].add(root)
            for dir_name in _dir_names_under_root(root, f):
                providers[dir_name].add(root)

    satisfiers: list[set[str]] = []
    for file_imports in imports.values():
        for raw in file_imports:
            roots: set[str] = set()
            for name in _acceptable_names(raw):
                roots |= providers.get(name, set())
            if roots:
                satisfiers.append(roots)

    unresolved = [s for s in satisfiers if not s & chosen]
    for _ in range(_MAX_GREEDY_ROOTS):
        gains: dict[str, int] = defaultdict(int)
        for roots in unresolved:
            for root in roots & candidates:
                gains[root] += 1
        if not gains:
            break
        best = min(gains, key=lambda r: (-gains[r], _depth(r), r))
        chosen.add(best)
        candidates.discard(best)
        unresolved = [s for s in unresolved if best not in s]
    return sorted(chosen, key=lambda r: (_depth(r), r))


def _ancestors(path: str) -> list[str]:
    out: list[str] = []
    current = _parent(path)
    while current:
        out.append(current)
        current = _parent(current)
    return out


class ModuleNaming:
    """Canonical module names plus import resolution for a fixed set of files and roots."""

    def __init__(self, files: Sequence[str], roots: Sequence[str]) -> None:
        self.files = sorted(set(files))
        self._file_set = set(self.files)
        self.roots = sorted(set(roots) | {""}, key=lambda r: (_depth(r), r))
        root_set = set(self.roots)
        self._by_name: dict[str, list[str]] = defaultdict(list)
        options: dict[str, list[str]] = {}
        for f in self.files:
            names: list[str] = []
            for root in (*_ancestors(f), ""):
                if root not in root_set:
                    continue
                name = name_under_root(root, f)
                if name and name not in names:
                    names.append(name)
                    self._by_name[name].append(f)
            path_name = f[:-3].replace("/", ".") if f.endswith(".py") else f
            if path_name.endswith(".__init__"):
                path_name = path_name[: -len(".__init__")]
            names.extend([path_name, f])
            options[f] = names
        self.canonical = self._assign_unique(options)
        self._file_by_canonical = {name: f for f, name in self.canonical.items()}

    @staticmethod
    def _assign_unique(options: dict[str, list[str]]) -> dict[str, str]:
        index = dict.fromkeys(options, 0)
        while True:
            claims: dict[str, list[str]] = defaultdict(list)
            for f, i in index.items():
                claims[options[f][i]].append(f)
            changed = False
            for owners in claims.values():
                if len(owners) > 1:
                    for f in owners:
                        if index[f] < len(options[f]) - 1:
                            index[f] += 1
                            changed = True
            if not changed:
                return {f: options[f][i] for f, i in index.items()}

    def module_name(self, file_path: str) -> str:
        return self.canonical[file_path]

    def file_for_module(self, module_name: str) -> str | None:
        return self._file_by_canonical.get(module_name)

    def _nearest(self, importer: str, candidates: list[str]) -> str:
        importer_parts = importer.split("/")[:-1]

        def shared(candidate: str) -> int:
            n = 0
            for a, b in zip(importer_parts, candidate.split("/")[:-1], strict=False):
                if a != b:
                    break
                n += 1
            return n

        return min(candidates, key=lambda c: (-shared(c), c.count("/"), c))

    def resolve_absolute(self, importer: str, dotted: str) -> str | None:
        candidates = self._by_name.get(dotted)
        return self._nearest(importer, candidates) if candidates else None

    def _resolve_absolute_prefix(self, importer: str, dotted: str) -> str | None:
        parts = dotted.split(".")
        for end in range(len(parts), 0, -1):
            found = self.resolve_absolute(importer, ".".join(parts[:end]))
            if found:
                return found
        return None

    def _relative_file(self, importer: str, level: int, module: str) -> tuple[str, str] | None:
        """(directory for the module, file of the module itself if any) for a relative import."""
        base = _parent(importer)
        for _ in range(level - 1):
            if base == "":
                return None
            base = _parent(base)
        parts = [p for p in module.split(".") if p]
        directory = "/".join([base, *parts]) if base else "/".join(parts)
        if parts:
            for candidate in (f"{directory}.py", f"{directory}/__init__.py"):
                if candidate in self._file_set:
                    return directory, candidate
            return directory, ""
        init = f"{directory}/__init__.py" if directory else "__init__.py"
        return directory, init if init in self._file_set else ""

    def _submodule_file(self, directory: str, name: str) -> str | None:
        stem = f"{directory}/{name}" if directory else name
        for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
            if candidate in self._file_set:
                return candidate
        return None

    def resolve_origin(self, importer: str, origin: str) -> tuple[str, str | None] | None:
        """Resolve an import alias origin (``pkg.mod.name`` or ``..mod.name``) to
        ``(module_name, attribute)``; ``attribute`` is ``None`` when the origin is a module."""
        level = len(origin) - len(origin.lstrip("."))
        dotted = origin[level:]
        if level == 0:
            target = self.resolve_absolute(importer, dotted)
            if target:
                return self.canonical[target], None
            module_part, _, attr = dotted.rpartition(".")
            owner = self._resolve_absolute_prefix(importer, module_part) if module_part else None
            return (self.canonical[owner], attr) if owner else None
        module_part, _, attr = dotted.rpartition(".")
        located = self._relative_file(importer, level, module_part)
        if located is None:
            return None
        directory, own_file = located
        submodule = self._submodule_file(directory, attr) if attr else None
        if submodule:
            return self.canonical[submodule], None
        return (self.canonical[own_file], attr or None) if own_file else None

    def resolve(self, importer: str, raw: RawImport) -> list[ImportRef]:
        """Turn one import statement into ImportRefs, one per distinct resolved target."""
        if raw.level == 0 and not raw.is_from:
            target = self._resolve_absolute_prefix(importer, raw.module)
            return [self._ref(raw, list(raw.names), target)]
        if raw.level == 0:
            module_target = self._resolve_absolute_prefix(importer, raw.module)
            submodule = {
                n: self.resolve_absolute(importer, f"{raw.module}.{n}")
                for n in raw.names
                if n != "*"
            }
        else:
            located = self._relative_file(importer, raw.level, raw.module)
            if located is None:
                return [self._ref(raw, list(raw.names), None)]
            directory, own_file = located
            module_target = own_file or None
            submodule = {n: self._submodule_file(directory, n) for n in raw.names if n != "*"}
        refs: list[ImportRef] = []
        plain = [n for n in raw.names if not submodule.get(n)]
        if plain or not raw.names:
            refs.append(self._ref(raw, plain, module_target))
        for name, target in submodule.items():
            if target:
                refs.append(self._ref(raw, [name], target))
        return refs

    def _ref(self, raw: RawImport, names: list[str], target: str | None) -> ImportRef:
        return ImportRef(
            module=raw.module,
            names=names,
            line=raw.line,
            level=raw.level,
            resolved_module=self.canonical[target] if target else None,
            is_internal=target is not None,
        )
