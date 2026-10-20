"""Static command discovery on synthetic repositories (nothing is executed)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from backend.models.audit_schemas import RepositoryInventory, SourceFileInfo
from backend.sandbox.command_discovery import (
    CandidateCommand,
    DiscoveryReport,
    discover_commands,
    validate_command_argv,
)


def _write(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip())
    return root


def _by_kind(report: DiscoveryReport, kind: str) -> list[CandidateCommand]:
    return [c for c in report.commands if c.kind == kind]


def _one(report: DiscoveryReport, kind: str) -> CandidateCommand:
    found = _by_kind(report, kind)
    assert len(found) == 1, [c.model_dump() for c in report.commands]
    return found[0]


def _assert_invariants(report: DiscoveryReport, root: Path) -> None:
    ids = [c.id for c in report.commands]
    assert len(ids) == len(set(ids))
    for command in report.commands:
        assert command.provenance == "STATIC"
        assert not command.argv or command.argv[0] == "python"
        if command.runnable:
            validate_command_argv(command.argv, workspace_dir=root, workdir_rel=command.workdir_rel)
        else:
            assert command.reason_if_not_runnable


def test_pyproject_pytest_config(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "pyproject.toml": """
                [project]
                name = "demo"

                [tool.pytest.ini_options]
                testpaths = ["tests"]
            """,
            "demo/__init__.py": "",
            "tests/conftest.py": "import pytest\n",
            "tests/test_api.py": "import requests\nimport demo\n\ndef test_a():\n    pass\n",
            "other/test_ignored.py": "def test_b():\n    pass\n",
        },
    )
    report = discover_commands(root)
    command = _one(report, "pytest")
    assert command.argv == ["python", "-m", "pytest", "-q"]
    assert command.workdir_rel == "."
    assert command.runnable
    assert command.evidence[0] == "pyproject.toml:4"
    assert "tests/conftest.py:1" in command.evidence
    assert "1 test file(s)" in command.description  # other/ is outside testpaths
    assert command.requires_dependencies
    assert command.third_party_imports == ["requests"]
    assert command.estimated_risk == "high"
    _assert_invariants(report, root)


def test_pytest_ini_wins_over_pyproject_and_ini_variants(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "pytest.ini": "[pytest]\naddopts = -q\n",
            "pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-x'\n",
            "test_root.py": "def test_x():\n    pass\n",
            "a/setup.cfg": "[metadata]\nname = a\n\n[tool:pytest]\ntestpaths = t\n",
            "a/t/test_a.py": "def test_a():\n    pass\n",
            "b/tox.ini": "[tox]\nenvlist = py312\n\n[pytest]\npython_files = check_*.py\n",
            "b/check_b.py": "def test_b():\n    pass\n",
            "b/test_not_matching.py": "def test_c():\n    pass\n",
        },
    )
    report = discover_commands(root)
    pytest_cmds = {c.workdir_rel: c for c in _by_kind(report, "pytest")}
    assert set(pytest_cmds) == {".", "a", "b"}
    assert pytest_cmds["."].evidence[0] == "pytest.ini:1"
    assert pytest_cmds["a"].evidence[0] == "a/setup.cfg:4"
    assert pytest_cmds["b"].evidence[0] == "b/tox.ini:4"
    assert "1 test file(s)" in pytest_cmds["b"].description
    assert not pytest_cmds["."].requires_dependencies
    assert pytest_cmds["."].estimated_risk == "low"
    _assert_invariants(report, root)


def test_unconfigured_unittest_suite_in_subproject(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "web/package.json": "{}",
            "backend/requirements.txt": "fastapi\n",
            "backend/app/__init__.py": "",
            "backend/app/core.py": "def f():\n    return 1\n",
            "backend/tests/unit/test_core.py": """
                import unittest
                from app.core import f

                class CoreTest(unittest.TestCase):
                    def test_f(self):
                        self.assertEqual(f(), 1)
            """,
        },
    )
    report = discover_commands(root)
    command = _one(report, "pytest")
    assert command.workdir_rel == "backend"
    assert command.argv == ["python", "-m", "pytest", "-q", "tests"]
    assert command.description.startswith("unittest-style tests")
    assert not command.requires_dependencies  # 'app' is local, unittest is stdlib
    _assert_invariants(report, root)


def test_pytest_benchmark_detection_and_false_positives(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "tests/test_perf.py": """
                def build(n):
                    return list(range(n))

                def test_build(benchmark):
                    benchmark(build, 1000)

                def helper(benchmark):
                    return benchmark
            """,
            "tests/test_plain.py": "def test_plain():\n    pass\n",
            "other/tests/conftest.py": """
                import pytest

                @pytest.fixture
                def benchmark():
                    return object()
            """,
            "other/tests/test_custom.py": "def test_uses_local(benchmark):\n    pass\n",
        },
    )
    report = discover_commands(root)
    bench = _one(report, "pytest_benchmark")
    assert bench.argv == ["python", "-m", "pytest", "-q", "tests/test_perf.py"]
    assert bench.evidence == ["tests/test_perf.py:4"]
    assert "1 test(s)" in bench.description
    assert len(_by_kind(report, "pytest")) == 2
    _assert_invariants(report, root)


def test_scripts_modules_and_false_positives(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "benchmarks/run_sort.py": (
                "def main():\n    pass\n\nif __name__ == '__main__':\n    main()\n"
            ),
            "benchmarks/util.py": "def helper():\n    pass\n",
            "benchmarks/fake_guard.py": (
                "TEXT = \"if __name__ == '__main__':\"\n# if __name__ == '__main__':\n"
            ),
            "bench/__main__.py": "print('bench')\n",
            "scripts/deploy.py": (
                "import subprocess\n\nif '__main__' == __name__:\n    subprocess.run(['ls'])\n"
            ),
            "app/main.py": "if __name__ == '__main__':\n    print('app')\n",
            "perf/test_perf_script.py": "if __name__ == '__main__':\n    pass\n",
        },
    )
    report = discover_commands(root)
    scripts = {tuple(c.argv): c for c in _by_kind(report, "script")}
    assert set(scripts) == {
        ("python", "benchmarks/run_sort.py"),
        ("python", "scripts/deploy.py"),
    }
    assert scripts[("python", "benchmarks/run_sort.py")].estimated_risk == "low"
    assert scripts[("python", "benchmarks/run_sort.py")].evidence == ["benchmarks/run_sort.py:4"]
    assert scripts[("python", "scripts/deploy.py")].estimated_risk == "medium"
    module = _one(report, "module")
    assert module.argv == ["python", "-m", "bench"]
    _assert_invariants(report, root)


def test_project_scripts_entries(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "pyproject.toml": """
                [project]
                name = "tool"

                [project.scripts]
                tool-cli = "tool.cli:main"
                tool-lib = "tool.core:run"
                ghost = "missing.mod:main"
            """,
            "src/tool/__init__.py": "",
            "src/tool/cli.py": "def main():\n    pass\n\nif __name__ == '__main__':\n    main()\n",
            "src/tool/core.py": "def run():\n    pass\n",
        },
    )
    report = discover_commands(root)
    module = _one(report, "module")
    assert module.argv == ["python", "-m", "tool.cli"]
    assert module.workdir_rel == "src"
    assert module.runnable
    assert "tool-cli" in module.description
    info = {c.description: c for c in _by_kind(report, "info_only")}
    lib = info["console script 'tool-lib' -> tool.core:run"]
    assert not lib.runnable
    assert lib.argv == []
    assert "__main__" in (lib.reason_if_not_runnable or "")
    ghost = info["console script 'ghost' -> missing.mod:main"]
    assert "not found" in (ghost.reason_if_not_runnable or "")
    assert ghost.evidence == ["pyproject.toml:7"]
    _assert_invariants(report, root)


def test_asv_tox_nox_and_make_are_information_only(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "asv.conf.json": '{\n  // asv allows comments\n  "benchmark_dir": "asv_bench"\n}\n',
            "asv_bench/benchmarks.py": "def time_sort():\n    sorted(range(100))\n",
            "tox.ini": "[tox]\nenvlist = py311, py312\n\n[testenv:lint]\ncommands = ruff .\n",
            "noxfile.py": """
                import nox

                @nox.session(python=["3.12"])
                def tests(session):
                    session.run("pytest")

                @nox.session
                def lint(session):
                    pass

                def not_a_session():
                    pass
            """,
            "Makefile": (
                ".PHONY: test bench\nTESTS := a\nlint:\n\truff .\ntest:\n\tpytest -q\n"
                "bench-quick: build\n\tpython bench.py\ntest_data:\n\t./gen.sh\n%.o: %.c\n"
                "deploy:\n\t./deploy.sh API_TOKEN=abcdef1234567890\n"
            ),
        },
    )
    report = discover_commands(root)
    asv = _one(report, "asv")
    assert not asv.runnable
    assert asv.requires_dependencies
    assert "1 file(s) in asv_bench/" in asv.description
    infos = {c.description: c for c in _by_kind(report, "info_only")}
    assert "tox environments: py311, py312, lint" in infos
    assert "nox environments: tests, lint" in infos
    assert infos["nox environments: tests, lint"].evidence == ["noxfile.py:4", "noxfile.py:8"]
    make = sorted(d for d in infos if d.startswith("make "))
    assert make == [
        "make bench-quick (runs: python bench.py)",
        "make test (runs: pytest -q)",
        "make test_data (runs: ./gen.sh)",
    ]
    assert infos["make test (runs: pytest -q)"].evidence == ["Makefile:5"]
    assert all(not c.runnable and c.argv == [] for c in infos.values())
    _assert_invariants(report, root)


def test_excluded_dirs_symlinks_and_inventory(tmp_path: Path) -> None:
    outside = _write(tmp_path / "outside", {"test_secret.py": "def test_x():\n    pass\n"})
    root = _write(
        tmp_path / "repo",
        {
            "node_modules/pkg/test_a.py": "def test_a():\n    pass\n",
            ".venv/lib/test_b.py": "def test_b():\n    pass\n",
            "pkg.egg-info/test_c.py": "def test_c():\n    pass\n",
            "tests/test_real.py": "def test_real():\n    pass\n",
        },
    )
    (root / "tests" / "test_link.py").symlink_to(outside / "test_secret.py")
    (root / "linked_dir").symlink_to(outside, target_is_directory=True)
    report = discover_commands(root)
    command = _one(report, "pytest")
    assert "1 test file(s)" in command.description
    assert all("outside" not in e and "node_modules" not in e for e in command.evidence)

    inventory = RepositoryInventory(
        root_name="repo",
        files=[
            SourceFileInfo(path="tests/test_real.py", size_bytes=1, is_python=True),
            SourceFileInfo(path="tests/test_link.py", size_bytes=1, is_python=True, skipped=True),
        ],
    )
    from_inventory = discover_commands(root, inventory)
    assert from_inventory.files_scanned == 1
    assert [c.argv for c in from_inventory.commands] == [command.argv]


def test_ids_are_stable_and_unique(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "tests/test_a.py": "def test_a(benchmark):\n    pass\n",
            "scripts/a.py": "if __name__ == '__main__':\n    pass\n",
            "Makefile": "test:\n\tpytest\nbench:\n\tpytest\n",
        },
    )
    first = discover_commands(root)
    second = discover_commands(root)
    assert [c.id for c in first.commands] == [c.id for c in second.commands]
    assert len({c.id for c in first.commands}) == len(first.commands) == 5
    _assert_invariants(first, root)


def test_empty_and_unparseable_repo(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        {
            "pyproject.toml": "this is [not toml",
            "setup.cfg": "[broken",
            "tests/test_bad.py": "def test(:\n",
        },
    )
    report = discover_commands(root)
    command = _one(report, "pytest")
    assert command.runnable  # syntax errors are pytest's problem, not discovery's
    assert discover_commands(_write(tmp_path / "empty", {"README.md": "hi"})).commands == []


def test_missing_workspace_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="does not exist"):
        discover_commands(tmp_path / "nope")
