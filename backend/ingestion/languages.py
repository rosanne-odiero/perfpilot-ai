"""Language classification by file extension or well-known file name."""

from __future__ import annotations

from pathlib import PurePosixPath

PYTHON_EXTENSIONS = frozenset({".py", ".pyi", ".pyw"})

_BY_EXTENSION: dict[str, str] = {
    ".py": "Python",
    ".pyi": "Python",
    ".pyw": "Python",
    ".pyx": "Cython",
    ".ipynb": "Jupyter Notebook",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".html": "HTML",
    ".htm": "HTML",
    ".jinja": "Jinja",
    ".jinja2": "Jinja",
    ".j2": "Jinja",
    ".css": "CSS",
    ".scss": "SCSS",
    ".sass": "SCSS",
    ".less": "Less",
    ".json": "JSON",
    ".yml": "YAML",
    ".yaml": "YAML",
    ".toml": "TOML",
    ".ini": "INI",
    ".cfg": "INI",
    ".md": "Markdown",
    ".mdx": "Markdown",
    ".rst": "reStructuredText",
    ".txt": "Text",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".ps1": "PowerShell",
    ".sql": "SQL",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".scala": "Scala",
    ".rb": "Ruby",
    ".php": "PHP",
    ".c": "C",
    ".h": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".cs": "C#",
    ".swift": "Swift",
    ".m": "Objective-C",
    ".r": "R",
    ".jl": "Julia",
    ".lua": "Lua",
    ".dart": "Dart",
    ".graphql": "GraphQL",
    ".proto": "Protocol Buffers",
    ".xml": "XML",
    ".svg": "SVG",
    ".csv": "CSV",
}

_BY_NAME: dict[str, str] = {
    "Dockerfile": "Dockerfile",
    "Containerfile": "Dockerfile",
    "Makefile": "Makefile",
    "GNUmakefile": "Makefile",
    "Procfile": "Procfile",
}


def language_for(rel_path: str) -> str | None:
    path = PurePosixPath(rel_path)
    if path.name in _BY_NAME:
        return _BY_NAME[path.name]
    if path.name.startswith("Dockerfile."):
        return "Dockerfile"
    return _BY_EXTENSION.get(path.suffix.lower())


def is_python_path(rel_path: str) -> bool:
    return PurePosixPath(rel_path).suffix.lower() in PYTHON_EXTENSIONS
