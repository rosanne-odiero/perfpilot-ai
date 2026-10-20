"""Dependency discovery from manifests. Nothing is imported or executed: TOML/INI are parsed as
data, requirements files as text, and ``setup.py`` only through :mod:`ast`."""

from __future__ import annotations

import ast
import configparser
import posixpath
import re
import tomllib
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath

from backend.ingestion.git_meta import strip_url_credentials
from backend.models.audit_schemas import DependencySpec

LOCK_FILE_NAMES = frozenset(
    {
        "poetry.lock",
        "Pipfile.lock",
        "uv.lock",
        "pdm.lock",
        "pylock.toml",
        "conda-lock.yml",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lock",
        "bun.lockb",
    }
)

_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?:\[[^\]]*\])?\s*(?P<rest>.*)$"
)
_URL_LINE = re.compile(r"^[a-z][a-z0-9+.-]*(?:\+[a-z]+)?://", re.IGNORECASE)
_SPEC_OK = re.compile(r"^[A-Za-z0-9<>=!~*^.,+\- ]{1,200}$")
_REQUIREMENTS_NAME = re.compile(
    r"(?i)^(?:(?P<pre>.+?)[-_.])?requirements(?:[-_.](?P<post>.+?))?\.(?:txt|in)$"
)
_INCLUDE = re.compile(r"^(?:-r|--requirement)(?:\s+|=)(?P<target>\S+)")
_COMMENT = re.compile(r"(^|\s)#.*$")


def normalize_name(name: str) -> str:
    """PEP 503 normalization so the same package is matched however it is spelled."""
    return re.sub(r"[-_.]+", "-", name).lower()


def find_lock_files(paths: Iterable[str]) -> list[str]:
    return sorted(p for p in paths if PurePosixPath(p).name in LOCK_FILE_NAMES)


def parse_requirement(raw: str, source_file: str, group: str | None) -> DependencySpec | None:
    """Parse one PEP 508 requirement string; returns None for URLs/paths/unparsable lines."""
    line = raw.strip()
    if not line or line.startswith(("-", ".", "/")) or _URL_LINE.match(line):
        return None
    match = _REQUIREMENT.match(line)
    if match is None:
        return None
    rest = match["rest"].split(";", 1)[0].strip()
    spec: str | None
    if rest.startswith("@"):
        spec = "@ " + strip_url_credentials(rest[1:].strip())
    else:
        rest = rest.strip("() ")
        spec = rest if rest and _SPEC_OK.match(rest) else None
        if rest and spec is None:
            return None
    return DependencySpec(
        name=normalize_name(match["name"]), version_spec=spec, source_file=source_file, group=group
    )


def _poetry_spec(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        version = value.get("version")
        return version.strip() if isinstance(version, str) and version.strip() else None
    return None


def _str_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


class _Collector:
    def __init__(self) -> None:
        self.items: list[DependencySpec] = []
        self._seen: set[tuple[str, str | None, str, str | None]] = set()

    def add(self, dep: DependencySpec | None) -> None:
        if dep is None:
            return
        key = (dep.name, dep.version_spec, dep.source_file, dep.group)
        if key not in self._seen:
            self._seen.add(key)
            self.items.append(dep)

    def requirement(self, raw: str, source: str, group: str | None) -> None:
        self.add(parse_requirement(raw, source, group))

    def named(self, name: str, spec: object, source: str, group: str | None) -> None:
        if not _REQUIREMENT.match(name):
            return
        self.add(
            DependencySpec(
                name=normalize_name(name),
                version_spec=_poetry_spec(spec),
                source_file=source,
                group=group,
            )
        )


def _parse_pyproject(text: str, source: str, out: _Collector) -> None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return
    project = data.get("project")
    if isinstance(project, dict):
        for req in _str_list(project.get("dependencies")):
            out.requirement(req, source, None)
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for extra, reqs in optional.items():
                for req in _str_list(reqs):
                    out.requirement(req, source, f"optional:{extra}")
    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        for group, reqs in groups.items():
            for req in _str_list(reqs):
                out.requirement(req, source, str(group))
    tool = data.get("tool")
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    if not isinstance(poetry, dict):
        return
    sections: list[tuple[object, str | None]] = [
        (poetry.get("dependencies"), None),
        (poetry.get("dev-dependencies"), "dev"),
    ]
    poetry_groups = poetry.get("group")
    if isinstance(poetry_groups, dict):
        for group, body in poetry_groups.items():
            if isinstance(body, dict):
                sections.append((body.get("dependencies"), str(group)))
    for table, group in sections:
        if isinstance(table, dict):
            for name, spec in table.items():
                if str(name).lower() != "python":
                    out.named(str(name), spec, source, group)


def _parse_pipfile(text: str, source: str, out: _Collector) -> None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return
    for section, group in (("packages", None), ("dev-packages", "dev")):
        table = data.get(section)
        if isinstance(table, dict):
            for name, spec in table.items():
                out.named(str(name), spec, source, group)


def _parse_setup_cfg(text: str, source: str, out: _Collector) -> None:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return

    def lines(value: str) -> list[str]:
        return [ln.strip() for ln in value.splitlines() if ln.strip()]

    if parser.has_section("options"):
        for key, group in (("install_requires", None), ("tests_require", "test")):
            if parser.has_option("options", key):
                for req in lines(parser.get("options", key)):
                    out.requirement(req, source, group)
    if parser.has_section("options.extras_require"):
        for extra, value in parser.items("options.extras_require"):
            for req in lines(value):
                out.requirement(req, source, f"optional:{extra}")


def _literal_strings(node: ast.expr, names: Mapping[str, ast.expr]) -> list[str]:
    if isinstance(node, ast.Name) and node.id in names:
        node = names[node.id]
    if isinstance(node, ast.List | ast.Tuple):
        return [
            e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
    return []


def _parse_setup_py(text: str, source: str, out: _Collector) -> None:
    """Read literal ``setup(...)`` keyword arguments with ``ast``; the file is never executed."""
    try:
        tree = ast.parse(text, filename=source)
    except (SyntaxError, ValueError):
        return
    names: dict[str, ast.expr] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if isinstance(target, ast.Name):
                names[target.id] = stmt.value
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        func_name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if func_name != "setup":
            continue
        for keyword in node.keywords:
            if keyword.arg in ("install_requires", "tests_require"):
                group = None if keyword.arg == "install_requires" else "test"
                for req in _literal_strings(keyword.value, names):
                    out.requirement(req, source, group)
            elif keyword.arg == "extras_require":
                value = keyword.value
                if isinstance(value, ast.Name) and value.id in names:
                    value = names[value.id]
                if isinstance(value, ast.Dict):
                    for key, reqs in zip(value.keys, value.values, strict=True):
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            for req in _literal_strings(reqs, names):
                                out.requirement(req, source, f"optional:{key.value}")


def is_requirements_file(rel_path: str) -> bool:
    path = PurePosixPath(rel_path)
    if _REQUIREMENTS_NAME.match(path.name):
        return True
    return path.parent.name == "requirements" and path.suffix in (".txt", ".in")


_MAIN_GROUP_LABELS = frozenset({"base", "main", "prod", "production", "common", "runtime"})


def _requirements_group(rel_path: str) -> str | None:
    """``requirements-dev.txt``/``dev-requirements.txt``/``requirements/dev.txt`` -> ``dev``."""
    path = PurePosixPath(rel_path)
    match = _REQUIREMENTS_NAME.match(path.name)
    label = (match["post"] or match["pre"]) if match else path.stem
    if not label or label.lower() in _MAIN_GROUP_LABELS:
        return None
    return label.lower()


def _parse_requirements(
    root: Path, rel: str, kept: set[str], visited: set[str], out: _Collector
) -> None:
    if rel in visited:
        return
    visited.add(rel)
    text = _read(root, rel)
    if text is None:
        return
    group = _requirements_group(rel)
    for raw in text.replace("\\\n", " ").splitlines():
        line = _COMMENT.sub("", raw).strip()
        if not line:
            continue
        include = _INCLUDE.match(line)
        if include:
            target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), include["target"]))
            if not target.startswith(("../", "/")) and target != ".." and target in kept:
                _parse_requirements(root, target, kept, visited, out)
            continue
        if line.startswith("-"):
            continue
        out.requirement(line, rel, group)


def _read(root: Path, rel: str) -> str | None:
    path = root.joinpath(*rel.split("/"))
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def discover_dependencies(root: Path, kept_paths: Iterable[str]) -> list[DependencySpec]:
    """Collect declared dependencies from every manifest among ``kept_paths`` (repo-relative)."""
    kept = set(kept_paths)
    out = _Collector()
    visited_requirements: set[str] = set()
    parsers: dict[str, Callable[[str, str, _Collector], None]] = {
        "pyproject.toml": _parse_pyproject,
        "Pipfile": _parse_pipfile,
        "setup.cfg": _parse_setup_cfg,
        "setup.py": _parse_setup_py,
    }
    for rel in sorted(kept):
        name = PurePosixPath(rel).name
        if name in parsers:
            text = _read(root, rel)
            if text is not None:
                parsers[name](text, rel, out)
        elif is_requirements_file(rel):
            _parse_requirements(root, rel, kept, visited_requirements, out)
    return out.items
