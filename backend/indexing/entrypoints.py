"""Entry-point detection from the AST: why a module or function is reachable from outside."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import TypeGuard

from backend.indexing.ast_index import iter_own_nodes
from backend.indexing.names import ImportAliases, dotted_name

ENTRY_FILE_NAMES = frozenset({"__main__.py", "main.py", "manage.py", "wsgi.py", "asgi.py"})
ROUTE_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "trace", "route", "api_route"}
    | {"websocket", "websocket_route"}
)
APP_CONSTRUCTORS = {
    "fastapi.FastAPI": "FastAPI",
    "fastapi.APIRouter": "APIRouter",
    "flask.Flask": "Flask",
    "flask.Blueprint": "Blueprint",
    "starlette.applications.Starlette": "Starlette",
    "starlette.routing.Router": "Router",
    "quart.Quart": "Quart",
    "sanic.Sanic": "Sanic",
    "celery.Celery": "Celery",
    "django.core.wsgi.get_wsgi_application": "Django WSGI",
    "django.core.asgi.get_asgi_application": "Django ASGI",
}
_FACTORY_KINDS = frozenset({"FastAPI", "Flask", "Starlette", "Quart", "Sanic", "Celery"})
_ROUTER_NAME = re.compile(
    r"(?i)^(app|application|api|router|bp|blueprint|blp|routes?|web|server)$"
    r"|_(app|api|router|bp|blueprint|routes?)$|router$"
)
_REGISTRATION_CALLS = frozenset(
    {"add_api_route", "add_url_rule", "add_route", "add_websocket_route", "add_api_websocket_route"}
)
_VIEW_BASE = re.compile(r"(View|ViewSet|APIView)$")
MAX_REASONS = 25


@dataclass
class EntrypointInfo:
    module_reasons: list[str] = field(default_factory=list)
    symbol_reasons: dict[str, str] = field(default_factory=dict)

    def add_module(self, reason: str) -> None:
        if reason not in self.module_reasons:
            self.module_reasons.append(reason)

    def add_symbol(self, qualname: str, reason: str) -> None:
        self.symbol_reasons.setdefault(qualname, reason)

    def all_reasons(self) -> list[str]:
        reasons = list(self.module_reasons)
        symbol_lines = [f"{reason}: {name}" for name, reason in self.symbol_reasons.items()]
        reasons.extend(symbol_lines[: max(0, MAX_REASONS - len(reasons))])
        hidden = len(self.module_reasons) + len(symbol_lines) - len(reasons)
        if hidden > 0:
            reasons.append(f"+{hidden} more entry points")
        return reasons


def is_main_guard(node: ast.AST) -> TypeGuard[ast.If]:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    if len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    sides = [test.left, test.comparators[0]]
    has_name = any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides)
    has_main = any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides)
    return has_name and has_main


def _const_str(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


class _Detector:
    def __init__(self, path: str, tree: ast.Module, aliases: ImportAliases) -> None:
        self.path = path
        self.tree = tree
        self.aliases = aliases
        self.info = EntrypointInfo()
        self.app_names: dict[str, str] = {}
        self.factory_calls: dict[int, str] = {}
        self.top_level_functions = {
            n.name for n in tree.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        self.uses_cli_lib = aliases.imports_module("click") or aliases.imports_module("typer")
        self.uses_celery = aliases.imports_module("celery")
        self.is_django = aliases.imports_module("django") or aliases.imports_module(
            "rest_framework"
        )

    def run(self) -> EntrypointInfo:
        basename = self.path.rsplit("/", 1)[-1]
        if basename in ENTRY_FILE_NAMES:
            self.info.add_module(f"conventional entry file ({basename})")
        self._collect_app_objects()
        for stmt in self.tree.body:
            if is_main_guard(stmt):
                self.info.add_module('`if __name__ == "__main__"` guard')
                self._main_guard_calls(stmt)
            if basename == "urls.py" and self._assigns(stmt, "urlpatterns"):
                self.info.add_module("Django URLconf (urlpatterns)")
        self._walk_definitions(self.tree.body, [])
        return self.info

    @staticmethod
    def _assigns(stmt: ast.stmt, name: str) -> bool:
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign | ast.AugAssign):
            targets = [stmt.target]
        return any(isinstance(t, ast.Name) and t.id == name for t in targets)

    def _collect_app_objects(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                qualified = self.aliases.qualify(node.func)
                if qualified == "argparse.ArgumentParser":
                    self.info.add_module("argparse command-line interface")
                kind = APP_CONSTRUCTORS.get(qualified or "")
                if kind and kind in _FACTORY_KINDS | {"Django WSGI", "Django ASGI"}:
                    self.info.add_module(f"application object ({kind})")
                    self.factory_calls[id(node)] = kind
                if isinstance(node.func, ast.Attribute) and node.func.attr in _REGISTRATION_CALLS:
                    self._registered_endpoints(node)
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                kind = APP_CONSTRUCTORS.get(self.aliases.qualify(node.value.func) or "")
                if kind:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            self.app_names[target.id] = kind
                        elif isinstance(target, ast.Attribute):
                            self.app_names[target.attr] = kind

    def _registered_endpoints(self, call: ast.Call) -> None:
        candidates = [a for a in call.args[1:] if isinstance(a, ast.Name)]
        candidates += [
            k.value
            for k in call.keywords
            if k.arg in {"endpoint", "view_func"} and isinstance(k.value, ast.Name)
        ]
        for name in candidates:
            if isinstance(name, ast.Name) and name.id in self.top_level_functions:
                path = _const_str(call.args[0]) if call.args else None
                self.info.add_symbol(name.id, f"HTTP route {path or ''}".rstrip())

    def _main_guard_calls(self, guard: ast.If) -> None:
        for node in ast.walk(guard):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in self.top_level_functions
            ):
                self.info.add_symbol(node.func.id, "called from __main__ guard")

    def _walk_definitions(self, body: list[ast.stmt], stack: list[str]) -> None:
        for node in body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                qualname = ".".join([*stack, node.name])
                reason = self._function_reason(node, in_class=bool(stack))
                if reason:
                    self.info.add_symbol(qualname, reason)
                self._walk_definitions(node.body, [*stack, node.name])
            elif isinstance(node, ast.ClassDef):
                qualname = ".".join([*stack, node.name])
                if self.is_django and any(
                    _VIEW_BASE.search(dotted_name(b) or "") for b in node.bases
                ):
                    self.info.add_symbol(qualname, "Django class-based view")
                self._walk_definitions(node.body, [*stack, node.name])
            elif isinstance(node, ast.If | ast.Try | ast.With):
                for child_body in _child_bodies(node):
                    self._walk_definitions(child_body, stack)

    def _function_reason(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, in_class: bool
    ) -> str | None:
        for decorator in node.decorator_list:
            reason = self._decorator_reason(decorator)
            if reason:
                return reason
        if self.factory_calls:
            for child in iter_own_nodes(list(node.body)):
                kind = self.factory_calls.get(id(child))
                if kind in _FACTORY_KINDS:
                    return f"application factory ({kind})"
        if (
            not in_class
            and self.is_django
            and self.path.rsplit("/", 1)[-1] == "views.py"
            and node.args.args
            and node.args.args[0].arg == "request"
        ):
            return "Django view"
        return None

    def _decorator_reason(self, decorator: ast.expr) -> str | None:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call else decorator
        qualified = self.aliases.qualify(target) or ""
        if qualified in {"celery.shared_task", "shared_task"} or qualified.endswith(".shared_task"):
            return "Celery task"
        if qualified in {"rest_framework.decorators.api_view"}:
            return "Django REST view"
        if not isinstance(target, ast.Attribute):
            return None
        receiver = dotted_name(target.value) or ""
        receiver_last = receiver.rsplit(".", 1)[-1]
        attr = target.attr
        if attr in ROUTE_METHODS and call is not None:
            is_router = receiver_last in self.app_names or bool(_ROUTER_NAME.search(receiver_last))
            if is_router and self.app_names.get(receiver_last) != "Celery":
                path = _const_str(call.args[0]) if call.args else None
                verb = "WS" if attr.startswith("websocket") else attr.upper()
                if attr in {"route", "api_route"}:
                    verb = "ROUTE"
                return f"HTTP route {verb} {path or ''}".rstrip()
        if attr == "task" and (
            self.uses_celery
            or self.app_names.get(receiver_last) == "Celery"
            or "celery" in receiver.lower()
        ):
            return "Celery task"
        if attr in {"command", "group", "callback"} and (
            self.uses_cli_lib or qualified.startswith(("click.", "typer."))
        ):
            return "CLI command"
        return None


def _child_bodies(node: ast.If | ast.Try | ast.With) -> list[list[ast.stmt]]:
    if isinstance(node, ast.If):
        return [node.body, node.orelse]
    if isinstance(node, ast.Try):
        return [node.body, node.orelse, node.finalbody, *[h.body for h in node.handlers]]
    return [node.body]


def detect_entrypoints(path: str, tree: ast.Module, aliases: ImportAliases) -> EntrypointInfo:
    return _Detector(path, tree, aliases).run()
