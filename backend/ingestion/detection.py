"""Language, framework and tooling detection with the evidence that triggered each result.

Evidence comes from manifests, declared dependencies and a cheap regex scan of Python import
lines. Nothing is imported or executed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from backend.ingestion.dependencies import normalize_name
from backend.models.audit_schemas import DependencySpec, DetectedTechnology

TechKind = Literal["language", "framework", "library", "tool", "database", "runtime"]

MAX_EVIDENCE = 5


@dataclass(frozen=True)
class Tech:
    name: str
    kind: TechKind


_FASTAPI = Tech("FastAPI", "framework")
_DJANGO = Tech("Django", "framework")
_ALEMBIC = Tech("Alembic", "tool")
_PYTEST = Tech("pytest", "tool")
_ASYNCIO = Tech("asyncio", "library")
_POSTGRES = Tech("PostgreSQL", "database")
_MONGO = Tech("MongoDB", "database")
_DOCKER = Tech("Docker", "tool")
_NODE = Tech("Node.js", "runtime")

PYTHON_MODULES: dict[str, Tech] = {
    "fastapi": _FASTAPI,
    "starlette": Tech("Starlette", "framework"),
    "flask": Tech("Flask", "framework"),
    "django": _DJANGO,
    "rest_framework": Tech("Django REST framework", "framework"),
    "tornado": Tech("Tornado", "framework"),
    "streamlit": Tech("Streamlit", "framework"),
    "sqlalchemy": Tech("SQLAlchemy", "library"),
    "sqlmodel": Tech("SQLModel", "library"),
    "pydantic": Tech("Pydantic", "library"),
    "pydantic_settings": Tech("Pydantic", "library"),
    "celery": Tech("Celery", "library"),
    "pandas": Tech("pandas", "library"),
    "numpy": Tech("NumPy", "library"),
    "scipy": Tech("SciPy", "library"),
    "sklearn": Tech("scikit-learn", "library"),
    "polars": Tech("Polars", "library"),
    "dask": Tech("Dask", "library"),
    "numba": Tech("Numba", "library"),
    "torch": Tech("PyTorch", "library"),
    "tensorflow": Tech("TensorFlow", "library"),
    "asyncio": _ASYNCIO,
    "requests": Tech("requests", "library"),
    "httpx": Tech("HTTPX", "library"),
    "aiohttp": Tech("aiohttp", "library"),
    "jinja2": Tech("Jinja", "library"),
    "openai": Tech("OpenAI SDK", "library"),
    "anthropic": Tech("Anthropic SDK", "library"),
    "langchain": Tech("LangChain", "library"),
    "boto3": Tech("AWS SDK (boto3)", "library"),
    "supabase": Tech("Supabase", "database"),
    "pytest": _PYTEST,
    "alembic": _ALEMBIC,
    "uvicorn": Tech("Uvicorn", "runtime"),
    "gunicorn": Tech("Gunicorn", "runtime"),
    "redis": Tech("Redis", "database"),
    "psycopg": _POSTGRES,
    "psycopg2": _POSTGRES,
    "asyncpg": _POSTGRES,
    "pymongo": _MONGO,
    "motor": _MONGO,
    "sqlite3": Tech("SQLite", "database"),
    "aiosqlite": Tech("SQLite", "database"),
}

_DISTRIBUTIONS: dict[str, Tech] = {
    **{normalize_name(module): tech for module, tech in PYTHON_MODULES.items()},
    "scikit-learn": PYTHON_MODULES["sklearn"],
    "djangorestframework": PYTHON_MODULES["rest_framework"],
    "psycopg2-binary": _POSTGRES,
    "psycopg-binary": _POSTGRES,
    "pydantic-settings": PYTHON_MODULES["pydantic"],
    "flask-sqlalchemy": PYTHON_MODULES["sqlalchemy"],
}

JS_PACKAGES: dict[str, Tech] = {
    "next": Tech("Next.js", "framework"),
    "react": Tech("React", "library"),
    "vue": Tech("Vue", "framework"),
    "nuxt": Tech("Nuxt", "framework"),
    "svelte": Tech("Svelte", "framework"),
    "@angular/core": Tech("Angular", "framework"),
    "express": Tech("Express", "framework"),
    "typescript": Tech("TypeScript", "language"),
    "tailwindcss": Tech("Tailwind CSS", "library"),
    "vite": Tech("Vite", "tool"),
    "jest": Tech("Jest", "tool"),
    "vitest": Tech("Vitest", "tool"),
    "prisma": Tech("Prisma", "library"),
    "@supabase/supabase-js": Tech("Supabase", "database"),
}

_LOCK_TOOLS: dict[str, Tech] = {
    "poetry.lock": Tech("Poetry", "tool"),
    "Pipfile.lock": Tech("Pipenv", "tool"),
    "uv.lock": Tech("uv", "tool"),
    "pdm.lock": Tech("PDM", "tool"),
    "package-lock.json": Tech("npm", "tool"),
    "yarn.lock": Tech("Yarn", "tool"),
    "pnpm-lock.yaml": Tech("pnpm", "tool"),
    "bun.lock": Tech("Bun", "runtime"),
    "bun.lockb": Tech("Bun", "runtime"),
}

_PROGRAMMING_LANGUAGES = frozenset(
    {
        "Python", "Cython", "JavaScript", "TypeScript", "Go", "Rust", "Java", "Kotlin", "Scala",
        "Ruby", "PHP", "C", "C++", "C#", "Swift", "Objective-C", "R", "Julia", "Lua", "Dart",
        "Shell", "SQL", "Vue", "Svelte",
    }
)  # fmt: skip

_COMPOSE_NAMES = frozenset(
    {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
)

_IMPORT_LINE = re.compile(
    rb"^[ \t]*(?:from[ \t]+(?P<from>[A-Za-z_]\w*)[\w.]*[ \t]+import\b"
    rb"|import[ \t]+(?P<names>[A-Za-z_][\w.]*(?:[ \t]*,[ \t]*[A-Za-z_][\w.]*)*))",
    re.MULTILINE,
)
_ASYNC_DEF = re.compile(rb"^[ \t]*async[ \t]+def\b", re.MULTILINE)


class TechnologyDetector:
    def __init__(self) -> None:
        self._evidence: dict[Tech, list[str]] = {}
        self._overflow: dict[Tech, int] = {}

    def add(self, tech: Tech, evidence: str) -> None:
        items = self._evidence.setdefault(tech, [])
        if evidence in items:
            return
        if len(items) < MAX_EVIDENCE:
            items.append(evidence)
        else:
            self._overflow[tech] = self._overflow.get(tech, 0) + 1

    def observe_python(self, rel_path: str, data: bytes) -> None:
        """Scan import lines (and ``async def``) of one Python file; never executes it."""
        found: set[Tech] = set()
        for match in _IMPORT_LINE.finditer(data):
            if match["from"]:
                modules = [match["from"]]
            else:
                modules = [n.strip().split(b".")[0] for n in match["names"].split(b",")]
            for module in modules:
                tech = PYTHON_MODULES.get(module.decode("ascii", errors="ignore"))
                if tech is not None and tech not in found:
                    found.add(tech)
                    line = data.count(b"\n", 0, match.start()) + 1
                    self.add(tech, f"imports {module.decode()} in {rel_path}:{line}")
        if _ASYNCIO not in found:
            async_def = _ASYNC_DEF.search(data)
            if async_def is not None:
                line = data.count(b"\n", 0, async_def.start()) + 1
                self.add(_ASYNCIO, f"async def in {rel_path}:{line}")

    def observe_languages(self, languages: Mapping[str, int]) -> None:
        for language, count in languages.items():
            if language in _PROGRAMMING_LANGUAGES:
                tech = Tech(language, "language")
                self.add(tech, f"{count} {language} file{'s' if count != 1 else ''}")

    def observe_dependencies(self, dependencies: Iterable[DependencySpec]) -> None:
        for dep in dependencies:
            tech = _DISTRIBUTIONS.get(dep.name)
            if tech is not None:
                self.add(tech, f"declared dependency {dep.name} in {dep.source_file}")

    def observe_files(
        self, root: Path, kept_paths: Iterable[str], all_paths: Iterable[str]
    ) -> None:
        """Manifest and well-known-file evidence. Only small, non-skipped files are read."""
        for rel in sorted(all_paths):
            name = PurePosixPath(rel).name
            if name in _LOCK_TOOLS:
                self.add(_LOCK_TOOLS[name], f"lock file {rel}")
        for rel in sorted(kept_paths):
            path = PurePosixPath(rel)
            name = path.name
            if name in ("Dockerfile", "Containerfile") or name.startswith("Dockerfile."):
                self.add(_DOCKER, f"{rel}")
            elif name in _COMPOSE_NAMES:
                self.add(Tech("Docker Compose", "tool"), f"{rel}")
            elif name == "alembic.ini":
                self.add(_ALEMBIC, f"{rel}")
            elif name in ("pytest.ini", "conftest.py"):
                self.add(_PYTEST, f"{rel}")
            elif name == "package.json":
                self._observe_package_json(root, rel)
            elif name == "manage.py" and b"DJANGO_SETTINGS_MODULE" in _read_bytes(root, rel):
                self.add(_DJANGO, f"{rel} sets DJANGO_SETTINGS_MODULE")
            elif path.parent.as_posix() == ".github/workflows" and path.suffix in (".yml", ".yaml"):
                self.add(Tech("GitHub Actions", "tool"), f"{rel}")

    def _observe_package_json(self, root: Path, rel: str) -> None:
        try:
            data = json.loads(_read_bytes(root, rel) or b"null")
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(data, dict):
            return
        self.add(_NODE, f"{rel}")
        for section in ("dependencies", "devDependencies", "peerDependencies"):
            deps = data.get(section)
            if isinstance(deps, dict):
                for package in deps:
                    tech = JS_PACKAGES.get(str(package))
                    if tech is not None:
                        self.add(tech, f"{section} {package} in {rel}")

    def result(self) -> list[DetectedTechnology]:
        technologies: list[DetectedTechnology] = []
        for tech, evidence in self._evidence.items():
            items = list(evidence)
            extra = self._overflow.get(tech, 0)
            if extra:
                items.append(f"... and {extra} more")
            technologies.append(DetectedTechnology(name=tech.name, kind=tech.kind, evidence=items))
        order = {
            "language": 0,
            "framework": 1,
            "library": 2,
            "database": 3,
            "runtime": 4,
            "tool": 5,
        }
        return sorted(technologies, key=lambda t: (order[t.kind], t.name.lower()))


def _read_bytes(root: Path, rel: str) -> bytes:
    path = root.joinpath(*rel.split("/"))
    try:
        if path.is_symlink() or not path.is_file():
            return b""
        return path.read_bytes()
    except OSError:
        return b""
