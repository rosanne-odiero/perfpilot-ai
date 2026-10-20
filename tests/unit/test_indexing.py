"""Indexing: parsing, module naming/import roots, import graph, entry points, call graph."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from backend.indexing.call_graph import symbol_id
from backend.indexing.code_index import (
    build_code_index,
    index_repository,
    link_repository,
    select_files,
)
from backend.indexing.module_resolution import ModuleNaming, RawImport
from backend.indexing.source import decode_source, parse_file
from backend.models.audit_schemas import CodeIndex, ModuleInfo


def write(root: Path, files: dict[str, str]) -> list[str]:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return sorted(files)


def index(root: Path, files: dict[str, str]) -> CodeIndex:
    return build_code_index(root, write(root, files), max_files=1000)


def module(idx: CodeIndex, path: str) -> ModuleInfo:
    return next(m for m in idx.modules if m.file_path == path)


def edges(idx: CodeIndex) -> set[tuple[str, str]]:
    return {(e.source, e.target) for e in idx.graph.edges}


# --- parsing --------------------------------------------------------------------------


def test_parse_errors_are_recorded_not_fatal(tmp_path: Path) -> None:
    idx = index(tmp_path, {"good.py": "x = 1\n", "bad.py": "def broken(:\n    pass\n"})
    assert set(idx.parse_errors) == {"bad.py"}
    assert "SyntaxError at line 1" in idx.parse_errors["bad.py"]
    assert module(idx, "bad.py").parse_error
    assert module(idx, "good.py").parse_error is None


def test_encodings_are_tolerated(tmp_path: Path) -> None:
    (tmp_path / "latin.py").write_bytes(b"# -*- coding: latin-1 -*-\nname = '\xe9t\xe9'\n")
    (tmp_path / "bom.py").write_bytes(b"\xef\xbb\xbfvalue = 1\n")
    (tmp_path / "junk.py").write_bytes(b"value = '\xff\xfe'\n")
    (tmp_path / "nul.py").write_bytes(b"x = 1\x00\n")
    (tmp_path / "crlf.py").write_bytes(b"def f():\r\n    return 1\r\n")
    build = index_repository(
        tmp_path, ["latin.py", "bom.py", "junk.py", "nul.py", "crlf.py"], max_files=10
    )
    assert build.parsed["latin.py"].tree is not None
    assert "été" in build.parsed["latin.py"].source
    assert build.parsed["bom.py"].tree is not None
    assert build.parsed["junk.py"].tree is not None
    assert any("junk.py" in w for w in build.warnings)
    assert build.parsed["nul.py"].error
    assert build.parsed["crlf.py"].lines[:2] == ["def f():", "    return 1"]
    assert decode_source(b"a\r\nb\rc")[0] == "a\nb\nc"


def test_repository_code_is_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    payload = f"open({str(marker)!r}, 'w').write('x')\nraise SystemExit(3)\n"
    idx = index(tmp_path, {"evil.py": payload, "pkg/__init__.py": payload})
    assert not marker.exists()
    assert len(idx.modules) == 2


def test_select_files_contains_paths(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    write(root, {"ok.py": "", "notes.txt": ""})
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n")
    os.symlink(outside, root / "link.py")
    selected, skipped, limits = select_files(
        root,
        [
            "ok.py",
            "../outside.py",
            "/etc/passwd.py",
            "link.py",
            "notes.txt",
            "missing.py",
            "a\\b.py",
        ],
        max_files=10,
        max_file_bytes=None,
    )
    assert selected == ["ok.py"]
    assert skipped == {
        "../outside.py": "invalid path",
        "/etc/passwd.py": "invalid path",
        "link.py": "symlink",
        "notes.txt": "not a .py file",
        "missing.py": "missing",
        "a\\b.py": "invalid path",
    }
    assert limits == []


def test_select_files_limits_prefer_source_over_tests(tmp_path: Path) -> None:
    files = write(tmp_path, {"tests/test_a.py": "", "pkg/a.py": "", "pkg/b.py": "x" * 50})
    selected, skipped, limits = select_files(tmp_path, files, max_files=1, max_file_bytes=10)
    assert selected == ["pkg/a.py"]
    assert skipped["pkg/b.py"] == "exceeds max_file_bytes"
    assert skipped["tests/test_a.py"] == "max_python_files limit reached"
    assert limits == ["analyzed 1 of 2 Python files (max_python_files limit)"]


# --- module naming and import resolution ----------------------------------------------


def test_package_relative_and_external_imports(tmp_path: Path) -> None:
    idx = index(
        tmp_path,
        {
            "pkg/__init__.py": "from . import util\n",
            "pkg/util.py": "import os.path\nimport requests\n",
            "pkg/sub/__init__.py": "",
            "pkg/sub/core.py": (
                "from ..util import helper\nfrom pkg.sub import other\nfrom . import missing\n"
                "from typing import TYPE_CHECKING\n"
                "if TYPE_CHECKING:\n    from pkg import typing_only\n"
            ),
            "pkg/sub/other.py": "",
            "script.py": "from pkg.util import helper\nimport numpy as np\n",
        },
    )
    assert idx.import_roots == [""]
    names = {m.file_path: m.module_name for m in idx.modules}
    assert names["pkg/sub/core.py"] == "pkg.sub.core"
    assert names["pkg/__init__.py"] == "pkg"
    assert edges(idx) == {
        ("pkg", "pkg.util"),
        ("pkg.sub.core", "pkg.util"),
        ("pkg.sub.core", "pkg.sub.other"),
        ("pkg.sub.core", "pkg.sub"),
        ("script", "pkg.util"),
    }
    core = module(idx, "pkg/sub/core.py")
    refs = {(r.module, tuple(r.names), r.level): r for r in core.imports}
    assert refs[("util", ("helper",), 2)].resolved_module == "pkg.util"
    assert refs[("pkg.sub", ("other",), 0)].resolved_module == "pkg.sub.other"
    assert refs[("", ("missing",), 1)].resolved_module == "pkg.sub"
    assert not any("typing_only" in r.names for r in core.imports)
    assert idx.graph.external_imports == {"numpy": 1, "os": 1, "requests": 1, "typing": 1}


def test_src_layout(tmp_path: Path) -> None:
    idx = index(
        tmp_path,
        {
            "src/mylib/__init__.py": "",
            "src/mylib/core.py": "from mylib import helpers\n",
            "src/mylib/helpers.py": "",
            "tests/test_core.py": "from mylib.core import x\n",
        },
    )
    assert "src" in idx.import_roots
    assert module(idx, "src/mylib/core.py").module_name == "mylib.core"
    assert ("mylib.core", "mylib.helpers") in edges(idx)
    assert ("tests.test_core", "mylib.core") in edges(idx)


def test_nested_project_root_is_inferred(tmp_path: Path) -> None:
    idx = index(
        tmp_path,
        {
            "frontend/build.py": "",
            "backend/main.py": "from app.api import router\nfrom core import db\n",
            "backend/app/__init__.py": "",
            "backend/app/api.py": "from core.db import session\nfrom app.models import User\n",
            "backend/app/models.py": "",
            "backend/core/__init__.py": "",
            "backend/core/db.py": "",
        },
    )
    assert "backend" in idx.import_roots
    names = {m.file_path: m.module_name for m in idx.modules}
    assert names["backend/app/api.py"] == "app.api"
    assert names["backend/main.py"] == "main"
    assert {("main", "app.api"), ("main", "core.db"), ("app.api", "core.db")} <= edges(idx)
    assert ("app.api", "app.models") in edges(idx)


def test_namespace_packages_and_script_dirs(tmp_path: Path) -> None:
    idx = index(
        tmp_path,
        {
            "ns/plugins/alpha.py": "from ns.plugins import beta\n",
            "ns/plugins/beta.py": "",
            "tools/run.py": "import helpers\n",
            "tools/helpers.py": "",
        },
    )
    assert "tools" in idx.import_roots
    assert ("ns.plugins.alpha", "ns.plugins.beta") in edges(idx)
    assert ("run", "helpers") in edges(idx)


def test_colliding_names_prefer_the_nearest_file(tmp_path: Path) -> None:
    idx = index(
        tmp_path,
        {
            "services/api/app/__init__.py": "",
            "services/api/app/main.py": "from app import settings\n",
            "services/api/app/settings.py": "",
            "services/worker/app/__init__.py": "",
            "services/worker/app/main.py": "from app import settings\n",
            "services/worker/app/settings.py": "",
        },
    )
    names = {m.file_path: m.module_name for m in idx.modules}
    assert len(set(names.values())) == len(names)
    api_main = names["services/api/app/main.py"]
    worker_main = names["services/worker/app/main.py"]
    assert (api_main, names["services/api/app/settings.py"]) in edges(idx)
    assert (worker_main, names["services/worker/app/settings.py"]) in edges(idx)


def test_module_naming_resolve_origin() -> None:
    naming = ModuleNaming(["pkg/__init__.py", "pkg/a.py", "pkg/b/__init__.py"], [""])
    assert naming.resolve_origin("pkg/a.py", "pkg.b") == ("pkg.b", None)
    assert naming.resolve_origin("pkg/a.py", "pkg.a.func") == ("pkg.a", "func")
    assert naming.resolve_origin("pkg/a.py", ".b") == ("pkg.b", None)
    assert naming.resolve_origin("pkg/a.py", "...too_far") is None
    refs = naming.resolve("pkg/a.py", RawImport("", ("b", "thing"), 1, 3, True))
    assert {(tuple(r.names), r.resolved_module) for r in refs} == {
        (("thing",), "pkg"),
        (("b",), "pkg.b"),
    }


# --- graph ----------------------------------------------------------------------------


def test_graph_fan_in_out_and_cycles(tmp_path: Path) -> None:
    idx = index(
        tmp_path,
        {
            "a.py": "import b\n",
            "b.py": "import c\n",
            "c.py": "import a\nimport d\n",
            "d.py": "",
            "e.py": "import d\nimport e\n",
        },
    )
    nodes = {n.module_name: n for n in idx.graph.nodes}
    assert nodes["d"].fan_in == 2
    assert nodes["d"].fan_out == 0
    assert nodes["c"].fan_out == 2
    assert idx.graph.cycles == [["a", "b", "c"]]
    assert {n for n, node in nodes.items() if node.in_cycle} == {"a", "b", "c"}
    assert ("e", "e") not in edges(idx)
    line = next(e.line for e in idx.graph.edges if (e.source, e.target) == ("c", "d"))
    assert line == 2


# --- symbols and entry points ----------------------------------------------------------


def test_symbols(tmp_path: Path) -> None:
    idx = index(
        tmp_path,
        {
            "mod.py": '''
            import functools

            class Shop:
                """A shop."""

                def total(self, items):
                    return sum(i.price for i in items)

                async def refresh(self):
                    for a in range(3):
                        for b in range(3):
                            self.total([])

            @functools.lru_cache
            def fib(n):
                return n if n < 2 else fib(n - 1) + fib(n - 2)

            def outer():
                def inner():
                    return 1
                return inner()
            '''
        },
    )
    symbols = {s.qualname: s for s in module(idx, "mod.py").symbols}
    assert symbols["Shop"].kind == "class"
    assert symbols["Shop"].docstring == "A shop."
    assert symbols["Shop.total"].kind == "method"
    assert symbols["Shop.total"].max_loop_depth == 1
    assert symbols["Shop.refresh"].kind == "async_method"
    assert symbols["Shop.refresh"].max_loop_depth == 2
    assert "self.total" in symbols["Shop.refresh"].calls
    assert symbols["fib"].is_recursive
    assert symbols["fib"].decorators == ["functools.lru_cache"]
    assert (symbols["fib"].line_start, symbols["fib"].line_end) == (14, 16)
    assert symbols["outer.inner"].kind == "function"
    assert not symbols["outer"].is_recursive


def test_entrypoint_detection(tmp_path: Path) -> None:
    idx = index(
        tmp_path,
        {
            "api.py": """
            from fastapi import APIRouter, FastAPI

            app = FastAPI()
            users_router = APIRouter()

            @users_router.get("/users/{uid}")
            async def get_user(uid: int): ...

            @app.post("/items")
            def create_item(): ...

            def helper(): ...
            """,
            "web.py": """
            from flask import Blueprint, Flask

            bp = Blueprint("x", __name__)

            def create_app():
                return Flask(__name__)

            @bp.route("/health")
            def health(): ...
            """,
            "tasks.py": """
            from celery import Celery, shared_task

            celery_app = Celery("t")

            @celery_app.task
            def send(): ...

            @shared_task(bind=True)
            def cleanup(self): ...
            """,
            "cli.py": """
            import click

            @click.group()
            def cli(): ...

            @cli.command()
            def sync(): ...
            """,
            "tool.py": """
            import argparse

            def main():
                argparse.ArgumentParser()

            if __name__ == "__main__":
                main()
            """,
            "urls.py": "urlpatterns = []\n",
            "views.py": """
            from django.views import View

            def index(request): ...

            class Detail(View): ...
            """,
            "plain.py": """
            cache = {}

            @cache.get
            def nothing(): ...
            """,
        },
    )
    entry = {m.file_path: m for m in idx.modules}
    sym = {(m.file_path, s.qualname): s.is_entrypoint for m in idx.modules for s in m.symbols}
    assert sym[("api.py", "get_user")]
    assert sym[("api.py", "create_item")]
    assert not sym[("api.py", "helper")]
    assert any("HTTP route GET /users/{uid}" in r for r in entry["api.py"].entrypoint_reasons)
    assert any("application object (FastAPI)" in r for r in entry["api.py"].entrypoint_reasons)
    assert sym[("web.py", "health")]
    assert sym[("web.py", "create_app")]
    assert sym[("tasks.py", "send")]
    assert sym[("tasks.py", "cleanup")]
    assert sym[("cli.py", "cli")]
    assert sym[("cli.py", "sync")]
    assert sym[("tool.py", "main")]
    assert '`if __name__ == "__main__"` guard' in entry["tool.py"].entrypoint_reasons
    assert "argparse command-line interface" in entry["tool.py"].entrypoint_reasons
    assert entry["urls.py"].entrypoint_reasons == ["Django URLconf (urlpatterns)"]
    assert sym[("views.py", "index")]
    assert sym[("views.py", "Detail")]
    assert not entry["plain.py"].is_entrypoint


def test_conventional_entry_files_and_console_scripts(tmp_path: Path) -> None:
    idx = index(
        tmp_path,
        {
            "pyproject.toml": '[project.scripts]\nrun-it = "pkg.cli:main"\n',
            "pkg/__init__.py": "",
            "pkg/cli.py": "def main(): ...\n",
            "manage.py": "",
            "pkg/__main__.py": "",
        },
    )
    entry = {m.file_path: m for m in idx.modules}
    assert entry["manage.py"].is_entrypoint
    assert entry["pkg/__main__.py"].is_entrypoint
    assert "console script 'run-it'" in entry["pkg/cli.py"].entrypoint_reasons
    assert entry["pkg/cli.py"].symbols[0].is_entrypoint


@pytest.mark.parametrize("bad_toml", ["[project", "project = 1\n"])
def test_bad_pyproject_is_ignored(tmp_path: Path, bad_toml: str) -> None:
    idx = index(tmp_path, {"pyproject.toml": bad_toml, "a.py": ""})
    assert not module(idx, "a.py").is_entrypoint


# --- call graph -----------------------------------------------------------------------


def test_call_graph_resolution(tmp_path: Path) -> None:
    root = tmp_path
    files = write(
        root,
        {
            "pkg/__init__.py": "",
            "pkg/db.py": "def query(): ...\nclass Repo:\n    def __init__(self): ...\n",
            "pkg/service.py": """
            from pkg import db
            from pkg.db import Repo

            class Service:
                def run(self):
                    self.load()
                    return Repo()

                def load(self):
                    return db.query()

            def entry():
                return Service().run()
            """,
        },
    )
    build = index_repository(root, files, max_files=10)
    link_repository(build)
    graph = build.call_graph
    assert graph.has_edge(symbol_id("pkg.service", "Service.run"), "pkg.service:Service.load")
    assert graph.has_edge("pkg.service:Service.run", "pkg.db:Repo.__init__")
    assert graph.has_edge("pkg.service:Service.load", "pkg.db:query")
    assert graph.has_edge("pkg.service:entry", "pkg.service:Service.run") is False
    assert graph.has_edge("pkg.service:entry", "pkg.service:Service")


def test_parse_file_reports_unreadable(tmp_path: Path) -> None:
    module_ = parse_file(tmp_path, "missing.py")
    assert module_.tree is None
    assert module_.error
    assert module_.error.startswith("unreadable")
