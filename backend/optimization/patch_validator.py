"""Patch validator: the gate every proposed diff passes before it can be approved or applied.

It applies to *any* proposer (deterministic or LLM). Checks, in order:

1. strict unified-diff parsing (no binary patches, mode changes, renames or copies);
2. path safety: relative, no ``..``/absolute/backslash/NUL, inside the workspace, no symlinks on
   the path, an existing regular file (new/deleted files only when explicitly allowed);
3. protected paths: secrets/env files, CI configs, security configs, VCS metadata, build and
   dependency files, benchmark harness files, fixtures, reference oracles and tests can never be
   patched, even when listed as allowed - a candidate must not improve its score by weakening
   tests, altering fixtures, touching the timer, or reducing the measured work;
4. an explicit allow-list (normally just the recommendation's target file);
5. the diff applies cleanly (exact context, no fuzz);
6. patched Python compiles, keeps every public signature, and introduces no constructs that can
   game a benchmark (cross-call caches, global state, timer/interpreter tampering, I/O).
"""

from __future__ import annotations

import ast
import fnmatch
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from backend.optimization.diffs import (
    DiffParseError,
    FilePatch,
    PatchApplyError,
    apply_file_patch,
    parse_unified_diff,
)

MAX_CHANGED_LINES = 400
MAX_FILE_BYTES = 512 * 1024

_SECRET_NAMES = (
    ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".netrc", ".pypirc", ".npmrc",
    ".dockercfg", "credentials*", "*secret*", "*.kdbx",
)  # fmt: skip
_CI_DIRS = {".github", ".gitlab", ".circleci", ".buildkite", ".azure-pipelines", ".woodpecker"}
_CI_NAMES = (
    ".gitlab-ci.yml", ".travis.yml", "azure-pipelines.yml", "jenkinsfile",
    "bitbucket-pipelines.yml", "appveyor.yml", ".drone.yml", "cloudbuild.yaml",
)  # fmt: skip
_SECURITY_NAMES = (
    "codeowners", "security.md", ".pre-commit-config.yaml", ".gitleaks.toml", ".bandit",
    "bandit.yaml", "bandit.yml", ".semgrep.yml", ".semgrep.yaml", "dependabot.yml", ".snyk",
    "renovate.json", ".trivyignore",
)  # fmt: skip
_VCS_DIRS = {".git", ".hg", ".svn"}
_BUILD_NAMES = (
    "pyproject.toml", "setup.py", "setup.cfg", "uv.lock", "poetry.lock", "pipfile",
    "pipfile.lock", "requirements*.txt", "constraints*.txt", "makefile", "dockerfile",
    "*.dockerfile", "docker-compose*.yml", "manifest.in", ".python-version",
)  # fmt: skip
_HARNESS_NAMES = (
    "workload.json", "conftest.py", "pytest.ini", "tox.ini", "noxfile.py", "asv.conf.json",
    "harness_entry.py", ".coveragerc", "sitecustomize.py", "usercustomize.py", "*.pth",
)  # fmt: skip
_HARNESS_DIRS = {"benchmarks", "benchmark", "asv_bench", ".benchmarks"}
_FIXTURE_NAMES = ("fixtures.py", "fixture.py")
_FIXTURE_DIRS = {"fixtures", "testdata", "test_data", "reference"}
_TEST_NAMES = ("test_*.py", "*_test.py", "tests.py")
_TEST_DIRS = {"tests", "test", "testing"}

# Introduced by a patch, these can make a benchmark measure something other than the work.
_TAMPER_MODULES = {
    "atexit", "builtins", "cprofile", "ctypes", "gc", "importlib", "inspect", "marshal",
    "multiprocessing", "os", "pickle", "profile", "psutil", "pstats", "resource", "signal",
    "socket", "subprocess", "sys", "threading", "time", "timeit", "tracemalloc",
}  # fmt: skip
_TAMPER_CALLS = {
    "__import__", "breakpoint", "compile", "delattr", "eval", "exec", "globals", "input",
    "locals", "open", "setattr", "vars",
}  # fmt: skip
_CACHE_DECORATORS = ("cache", "lru_cache", "cached_property", "memoize", "memoized")
_MUTABLE_FACTORIES = {"dict", "list", "set", "defaultdict", "OrderedDict", "Counter", "deque"}


class PatchViolation(BaseModel):
    code: str
    message: str
    path: str | None = None


class PatchValidationReport(BaseModel):
    valid: bool
    violations: list[PatchViolation] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    changed_lines: int = 0
    checks: dict[str, str] = Field(default_factory=dict, description="check -> passed|failed")


class ValidatedPatch(BaseModel):
    report: PatchValidationReport
    patched: dict[str, str] = Field(default_factory=dict, description="path -> patched text")
    original: dict[str, str] = Field(default_factory=dict, description="path -> original text")
    deleted: list[str] = Field(default_factory=list, description="files the patch removes")


# ------------------------------------------------------------------------------------------
# Path rules
# ------------------------------------------------------------------------------------------


def path_problem(relative: str) -> str | None:
    """Why ``relative`` is not an acceptable workspace-relative POSIX path, or ``None``."""
    if not relative or "\x00" in relative:
        return "empty path or NUL byte"
    if "\\" in relative:
        return "backslashes are not allowed in patch paths"
    if relative.startswith("/") or relative.startswith("~") or ":" in relative.split("/")[0]:
        return "absolute paths are not allowed"
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return "path segments '', '.' and '..' are not allowed"
    return None


def protected_category(relative: str) -> str | None:
    """Category of a path that must never be patched (``None`` if it is ordinary source)."""
    path = PurePosixPath(relative)
    name = path.name.lower()
    dirs = {part.lower() for part in path.parts[:-1]}

    def matches(patterns: Iterable[str]) -> bool:
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)

    if matches(_SECRET_NAMES):
        return "secrets"
    if dirs & _VCS_DIRS:
        return "vcs"
    if dirs & _CI_DIRS or matches(_CI_NAMES):
        return "ci"
    if matches(_SECURITY_NAMES):
        return "security_config"
    if matches(_HARNESS_NAMES) or dirs & _HARNESS_DIRS:
        return "benchmark_harness"
    if matches(_FIXTURE_NAMES) or dirs & _FIXTURE_DIRS:
        return "fixtures"
    if matches(_TEST_NAMES) or dirs & _TEST_DIRS:
        return "tests"
    if matches(_BUILD_NAMES):
        return "build_config"
    return None


def _symlink_on_path(root: Path, relative: str) -> bool:
    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            return True
    return False


# ------------------------------------------------------------------------------------------
# Python checks
# ------------------------------------------------------------------------------------------


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = node.args
    shape = {
        "async": isinstance(node, ast.AsyncFunctionDef),
        "posonly": [a.arg for a in args.posonlyargs],
        "args": [a.arg for a in args.args],
        "vararg": args.vararg.arg if args.vararg else None,
        "kwonly": [a.arg for a in args.kwonlyargs],
        "kwarg": args.kwarg.arg if args.kwarg else None,
        "defaults": [ast.dump(d) for d in args.defaults],
        "kw_defaults": [ast.dump(d) if d else None for d in args.kw_defaults],
    }
    return repr(shape)


def public_signatures(tree: ast.Module) -> dict[str, str]:
    """``qualname -> signature`` of public top-level functions and public class methods."""
    signatures: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if not node.name.startswith("_"):
                signatures[node.name] = _signature(node)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            signatures[node.name] = "class"
            for item in node.body:
                public = not item.name.startswith("_") if hasattr(item, "name") else False
                dunder = getattr(item, "name", "").startswith("__")
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and (public or dunder):
                    signatures[f"{node.name}.{item.name}"] = _signature(item)
    return signatures


def _imported_modules(tree: ast.Module) -> dict[str, str]:
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound[alias.asname or alias.name.split(".")[0]] = alias.name.split(".")[0]
    return bound


def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    return target.id if isinstance(target, ast.Name) else ""


def tamper_features(tree: ast.Module) -> Counter[str]:
    """Counts of constructs that can change what a benchmark measures."""
    features: Counter[str] = Counter()
    modules = _imported_modules(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Global | ast.Nonlocal):
            features["global/nonlocal statement (shared state across calls)"] += 1
        elif isinstance(node, ast.Import | ast.ImportFrom):
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            names += [a.name for a in node.names] if isinstance(node, ast.Import) else []
            for name in names:
                root = name.split(".")[0]
                if root.lower() in _TAMPER_MODULES:
                    features[f"import of `{root}`"] += 1
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for decorator in node.decorator_list:
                if _decorator_name(decorator) in _CACHE_DECORATORS:
                    features["caching decorator (results reused across calls)"] += 1
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _TAMPER_CALLS
        ):
            features[f"call to `{node.func.id}()`"] += 1
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store | ast.Del)
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        ):
            features[f"assignment to module attribute `{node.value.id}.{node.attr}`"] += 1
    for stmt in tree.body:
        value = stmt.value if isinstance(stmt, ast.Assign | ast.AnnAssign) else None
        mutable = isinstance(
            value, ast.Dict | ast.List | ast.Set | ast.DictComp | ast.ListComp | ast.SetComp
        ) or (isinstance(value, ast.Call) and _decorator_name(value) in _MUTABLE_FACTORIES)
        if mutable:
            features["module-level mutable container (can carry results across calls)"] += 1
    return features


def python_problems(path: str, original: str, patched: str) -> list[PatchViolation]:
    try:
        compile(patched, path, "exec", dont_inherit=True)
        patched_tree = ast.parse(patched, filename=path)
    except (SyntaxError, ValueError) as exc:
        return [
            PatchViolation(
                code="compile_error", message=f"patched file does not compile: {exc}", path=path
            )
        ]
    try:
        original_tree = ast.parse(original, filename=path)
    except (SyntaxError, ValueError):
        return []  # the original did not parse either; nothing to compare signatures against
    problems: list[PatchViolation] = []
    before, after = public_signatures(original_tree), public_signatures(patched_tree)
    for name, signature in before.items():
        if name not in after:
            problems.append(
                PatchViolation(
                    code="public_api_removed", message=f"public `{name}` was removed", path=path
                )
            )
        elif after[name] != signature:
            problems.append(
                PatchViolation(
                    code="signature_changed",
                    message=f"the signature of public `{name}` changed",
                    path=path,
                )
            )
    introduced = tamper_features(patched_tree) - tamper_features(original_tree)
    for feature, count in sorted(introduced.items()):
        problems.append(
            PatchViolation(
                code="benchmark_tampering",
                message=f"patch introduces {feature} (x{count}); not allowed in an optimization",
                path=path,
            )
        )
    return problems


# ------------------------------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------------------------------


def _effective_path(patch: FilePatch) -> str:
    return patch.old_path if patch.is_deleted_file else patch.new_path


def _structure_problems(
    patch: FilePatch, allow_new_files: bool, allow_deleted_files: bool
) -> list[PatchViolation]:
    path = _effective_path(patch) or None
    problems: list[PatchViolation] = []
    if patch.binary:
        problems.append(
            PatchViolation(code="binary_patch", message="binary patches are not allowed", path=path)
        )
    if patch.mode_lines:
        problems.append(
            PatchViolation(
                code="mode_change", message="file mode changes are not allowed", path=path
            )
        )
    if patch.rename_or_copy or (
        not patch.is_new_file and not patch.is_deleted_file and patch.old_path != patch.new_path
    ):
        problems.append(
            PatchViolation(
                code="rename_or_copy", message="renames/copies are not allowed", path=path
            )
        )
    if patch.is_new_file and not allow_new_files:
        problems.append(
            PatchViolation(code="new_file", message="creating files is not allowed", path=path)
        )
    if patch.is_deleted_file and not allow_deleted_files:
        problems.append(
            PatchViolation(code="deleted_file", message="deleting files is not allowed", path=path)
        )
    if patch.git_paths is not None and path is not None:
        expected = (f"a/{patch.old_path}", f"b/{patch.new_path}")
        if not patch.is_new_file and not patch.is_deleted_file and patch.git_paths != expected:
            problems.append(
                PatchViolation(
                    code="inconsistent_paths",
                    message="the diff --git header names different files than ---/+++",
                    path=path,
                )
            )
    return problems


def validate_patch(
    diff: str,
    *,
    workspace_root: Path,
    allowed_files: Sequence[str],
    extra_protected: Sequence[str] = (),
    allow_new_files: bool = False,
    allow_deleted_files: bool = False,
) -> ValidatedPatch:
    """Validate ``diff`` against the files under ``workspace_root`` without modifying them."""
    report = PatchValidationReport(valid=False)
    result = ValidatedPatch(report=report)

    def fail(code: str, message: str, path: str | None = None) -> None:
        report.violations.append(PatchViolation(code=code, message=message, path=path))

    try:
        patches = parse_unified_diff(diff)
    except DiffParseError as exc:
        fail("malformed_diff", str(exc))
        report.checks["parse"] = "failed"
        return result
    report.checks["parse"] = "passed"
    root = workspace_root.resolve()
    allowed = set(allowed_files)
    protected_extra = {p.strip("/") for p in extra_protected}
    seen: set[str] = set()
    for patch in patches:
        report.violations.extend(_structure_problems(patch, allow_new_files, allow_deleted_files))
        path = _effective_path(patch)
        if not path:
            continue
        if problem := path_problem(path):
            fail("unsafe_path", problem, path)
            continue
        if path in seen:
            fail("duplicate_file", "the same file is patched twice", path)
            continue
        seen.add(path)
        report.files.append(path)
        if category := protected_category(path):
            fail("protected_path", f"{category} files may not be modified", path)
        if any(path == p or path.startswith(f"{p}/") for p in protected_extra):
            fail("protected_path", "validation inputs (tests/fixtures) may not be modified", path)
        if path not in allowed:
            fail("not_allowed", "only the recommendation's target file may change", path)
        target = root / path
        if _symlink_on_path(root, path):
            fail("symlink", "symlinks may not be patched or traversed", path)
            continue
        if not target.resolve().is_relative_to(root):
            fail("outside_workspace", "path resolves outside the workspace", path)
            continue
        if patch.is_new_file and (target.exists() or target.is_symlink()):
            fail("file_exists", "a 'new' file already exists", path)
            continue
        if not patch.is_new_file and not target.is_file():
            fail("missing_file", "the file to patch does not exist", path)
            continue
        if patch.is_new_file:
            original = ""
        elif target.stat().st_size > MAX_FILE_BYTES:
            fail("too_large", f"files larger than {MAX_FILE_BYTES} bytes are not patched", path)
            continue
        else:
            try:
                original = target.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                fail("not_text", "only UTF-8 text files can be patched", path)
                continue
        try:
            patched = apply_file_patch(original, patch)
        except PatchApplyError as exc:
            fail("does_not_apply", str(exc), path)
            continue
        report.changed_lines += sum(
            1 for hunk in patch.hunks for line in hunk.lines if line.tag in ("+", "-")
        )
        result.original[path] = original
        if patch.is_deleted_file:
            if patched:
                fail("does_not_apply", "a deletion must remove the whole file", path)
            result.deleted.append(path)
            continue
        result.patched[path] = patched
        if path.endswith(".py"):
            report.violations.extend(python_problems(path, original, patched))
    if report.changed_lines > MAX_CHANGED_LINES:
        fail("too_large", f"patch changes more than {MAX_CHANGED_LINES} lines")
    codes = {v.code for v in report.violations}
    report.checks.update(
        {
            "paths": "failed"
            if codes & {"unsafe_path", "symlink", "outside_workspace", "missing_file"}
            else "passed",
            "protected_files": "failed" if codes & {"protected_path", "not_allowed"} else "passed",
            "structure": "failed"
            if codes
            & {
                "binary_patch",
                "mode_change",
                "rename_or_copy",
                "new_file",
                "deleted_file",
                "inconsistent_paths",
                "duplicate_file",
            }
            else "passed",
            "applies_cleanly": "failed"
            if codes & {"does_not_apply", "not_text", "too_large"}
            else "passed",
            "compiles": "failed" if "compile_error" in codes else "passed",
            "public_signatures": "failed"
            if codes & {"signature_changed", "public_api_removed"}
            else "passed",
            "benchmark_integrity": "failed" if "benchmark_tampering" in codes else "passed",
        }
    )
    report.valid = not report.violations and bool(result.patched or result.deleted)
    if not (result.patched or result.deleted) and not report.violations:
        fail("empty_patch", "the diff changes no file")
    return result
