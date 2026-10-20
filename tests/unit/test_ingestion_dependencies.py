from __future__ import annotations

from pathlib import Path

import pytest

from backend.ingestion.dependencies import (
    discover_dependencies,
    find_lock_files,
    is_requirements_file,
    normalize_name,
    parse_requirement,
)
from backend.models.audit_schemas import DependencySpec


def _write(root: Path, files: dict[str, str]) -> list[str]:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return list(files)


def _by_name(deps: list[DependencySpec]) -> dict[str, DependencySpec]:
    return {d.name: d for d in deps}


@pytest.mark.parametrize(
    ("line", "name", "spec"),
    [
        ("requests==2.31.0", "requests", "==2.31.0"),
        ("Django (>=4.2)", "django", ">=4.2"),
        ("pydantic[email]>=2,<3", "pydantic", ">=2,<3"),
        ("uvicorn ; python_version > '3.10'", "uvicorn", None),
        ("Flask_SQLAlchemy~=3.1", "flask-sqlalchemy", "~=3.1"),
        ("pkg @ https://user:pw@example.com/pkg.whl", "pkg", "@ https://example.com/pkg.whl"),
    ],
)
def test_parse_requirement(line: str, name: str, spec: str | None) -> None:
    dep = parse_requirement(line, "requirements.txt", None)
    assert dep is not None
    assert (dep.name, dep.version_spec) == (name, spec)


@pytest.mark.parametrize(
    "line", ["", "-e .", "./local", "/abs/path", "https://example.com/x.whl", "git+https://x/y.git"]
)
def test_unparsable_requirement_lines_are_ignored(line: str) -> None:
    assert parse_requirement(line, "requirements.txt", None) is None


def test_normalize_name() -> None:
    assert normalize_name("Foo_Bar.baz") == "foo-bar-baz"


def test_pyproject_pep621_groups_and_poetry(tmp_path: Path) -> None:
    paths = _write(
        tmp_path,
        {
            "pyproject.toml": """
[project]
dependencies = ["fastapi>=0.110", "numpy"]
[project.optional-dependencies]
dev = ["pytest>=8"]
[dependency-groups]
lint = ["ruff==0.6.0", {include-group = "dev"}]
[tool.poetry.dependencies]
python = "^3.11"
pandas = "^2.2"
httpx = {version = "^0.27", extras = ["http2"]}
mylib = {git = "https://github.com/x/mylib.git"}
[tool.poetry.dev-dependencies]
black = "*"
[tool.poetry.group.docs.dependencies]
mkdocs = "^1.6"
""",
        },
    )
    deps = discover_dependencies(tmp_path, paths)
    found = {(d.name, d.version_spec, d.group) for d in deps}
    assert ("fastapi", ">=0.110", None) in found
    assert ("numpy", None, None) in found
    assert ("pytest", ">=8", "optional:dev") in found
    assert ("ruff", "==0.6.0", "lint") in found
    assert ("pandas", "^2.2", None) in found
    assert ("httpx", "^0.27", None) in found
    assert ("mylib", None, None) in found
    assert ("black", "*", "dev") in found
    assert ("mkdocs", "^1.6", "docs") in found
    assert "python" not in {d.name for d in deps}
    assert all(d.source_file == "pyproject.toml" for d in deps)


def test_requirements_includes_options_and_groups(tmp_path: Path) -> None:
    paths = _write(
        tmp_path,
        {
            "requirements.txt": "-r requirements/base.txt\nrequests==2.31  # http\n"
            "--index-url https://pypi.example.org\n-c constraints.txt\n"
            "-e git+https://github.com/x/y.git#egg=y\nlong-name \\\n  >=1.0\n",
            "requirements/base.txt": "sqlalchemy>=2\n-r ../requirements.txt\n",
            "requirements-dev.txt": "pytest\n-r ../../etc/passwd\n",
            "constraints.txt": "urllib3<3\n",
        },
    )
    deps = discover_dependencies(tmp_path, paths)
    by_name = _by_name(deps)
    assert by_name["requests"].source_file == "requirements.txt"
    assert by_name["requests"].group is None
    assert by_name["sqlalchemy"].source_file == "requirements/base.txt"
    assert by_name["long-name"].version_spec == ">=1.0"
    assert by_name["pytest"].group == "dev"
    assert "urllib3" not in by_name
    assert "y" not in by_name


def test_requirements_include_outside_repo_is_ignored(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("evil-package\n")
    repo = tmp_path / "repo"
    paths = _write(repo, {"requirements.txt": "-r ../outside.txt\nok-package\n"})
    assert {d.name for d in discover_dependencies(repo, paths)} == {"ok-package"}


def test_setup_cfg(tmp_path: Path) -> None:
    paths = _write(
        tmp_path,
        {
            "setup.cfg": """
[options]
install_requires =
    click>=8
    rich
tests_require = pytest
[options.extras_require]
yaml = pyyaml>=6
""",
        },
    )
    found = {(d.name, d.group) for d in discover_dependencies(tmp_path, paths)}
    assert found == {
        ("click", None),
        ("rich", None),
        ("pytest", "test"),
        ("pyyaml", "optional:yaml"),
    }


def test_setup_py_is_read_with_ast_and_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    paths = _write(
        tmp_path,
        {
            "setup.py": f"""
import pathlib
pathlib.Path({str(marker)!r}).write_text("pwned")
REQS = ["numpy>=1.26", "scipy"]
from setuptools import setup
setup(
    name="x",
    install_requires=REQS,
    extras_require={{"plot": ["matplotlib"]}},
    tests_require=("pytest",),
)
""",
        },
    )
    found = {(d.name, d.group) for d in discover_dependencies(tmp_path, paths)}
    assert found == {
        ("numpy", None),
        ("scipy", None),
        ("matplotlib", "optional:plot"),
        ("pytest", "test"),
    }
    assert not marker.exists()


def test_setup_py_with_dynamic_values_and_syntax_errors(tmp_path: Path) -> None:
    paths = _write(
        tmp_path,
        {
            "setup.py": "from setuptools import setup\nsetup(install_requires=open('r').read())\n",
            "broken/setup.py": "def (:\n",
        },
    )
    assert discover_dependencies(tmp_path, paths) == []


def test_pipfile(tmp_path: Path) -> None:
    paths = _write(
        tmp_path,
        {
            "Pipfile": '[packages]\nflask = "*"\ncelery = {version = ">=5"}\n'
            '[dev-packages]\npytest = ">=8"\n',
        },
    )
    found = {(d.name, d.version_spec, d.group) for d in discover_dependencies(tmp_path, paths)}
    assert found == {("flask", "*", None), ("celery", ">=5", None), ("pytest", ">=8", "dev")}


def test_invalid_manifests_are_ignored(tmp_path: Path) -> None:
    paths = _write(
        tmp_path, {"pyproject.toml": "[project\n", "Pipfile": "=", "setup.cfg": "[options\nx"}
    )
    assert discover_dependencies(tmp_path, paths) == []


def test_lock_files_and_requirements_detection() -> None:
    assert find_lock_files(["a/poetry.lock", "uv.lock", "x.py", "web/package-lock.json"]) == [
        "a/poetry.lock",
        "uv.lock",
        "web/package-lock.json",
    ]
    assert is_requirements_file("requirements.txt")
    assert is_requirements_file("dev-requirements.txt")
    assert is_requirements_file("requirements_test.in")
    assert is_requirements_file("requirements/prod.txt")
    assert not is_requirements_file("docs/requirements.md")
