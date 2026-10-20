"""Name helpers: dotted names of expressions and module-wide import aliases."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field


def dotted_name(node: ast.expr) -> str | None:
    """``a.b.c`` for Name/Attribute chains, otherwise ``None``."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def root_name(node: ast.expr) -> str | None:
    """Innermost Name of an attribute/call/subscript chain (``a`` in ``a.b().c[0].d``)."""
    current: ast.expr = node
    while True:
        if isinstance(current, ast.Attribute | ast.Subscript):
            current = current.value
        elif isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Name):
            return current.id
        else:
            return None


@dataclass
class ImportAliases:
    """Maps names bound by imports anywhere in a module to their qualified origin.

    ``import numpy as np`` -> ``np: numpy``; ``from time import sleep`` -> ``sleep: time.sleep``;
    relative imports keep their leading dots (``from . import x`` -> ``x: .x``).
    """

    names: dict[str, str] = field(default_factory=dict)

    @classmethod
    def build(cls, tree: ast.Module) -> ImportAliases:
        names: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        names.setdefault(alias.asname, alias.name)
                    else:
                        top = alias.name.split(".", 1)[0]
                        names.setdefault(top, top)
            elif isinstance(node, ast.ImportFrom):
                base = "." * node.level + (node.module or "")
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    sep = "" if base.endswith(".") or not base else "."
                    names.setdefault(alias.asname or alias.name, f"{base}{sep}{alias.name}")
        return cls(names)

    def qualify(self, node: ast.expr) -> str | None:
        """Qualified dotted name of ``node`` with import aliases expanded."""
        name = dotted_name(node)
        if name is None:
            return None
        head, _, rest = name.partition(".")
        origin = self.names.get(head)
        if origin is None:
            return name
        return f"{origin}.{rest}" if rest else origin

    def imports_module(self, top_level: str) -> bool:
        return any(
            origin == top_level or origin.startswith(top_level + ".")
            for origin in self.names.values()
        )
