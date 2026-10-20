"""Lightweight, flow-insensitive type ("kind") inference for names used by the rules.

A name gets a kind only when every binding of it in its scope agrees (``x = []`` and
``x = list(y)`` -> LIST; ``x = []`` then ``x = set()`` -> UNKNOWN). Parameters use their annotation.
This is deliberately conservative: rules that depend on a kind stay silent when it is UNKNOWN.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from enum import StrEnum

from backend.indexing.ast_index import iter_own_nodes
from backend.indexing.names import ImportAliases


class Kind(StrEnum):
    LIST = "list"
    SET = "set"
    DICT = "dict"
    STR = "str"
    TUPLE = "tuple"
    DEQUE = "deque"
    RANGE = "range"
    NUMBER = "number"
    FILE = "file"
    HTTP_CLIENT = "http_client"
    ASYNC_HTTP_CLIENT = "async_http_client"
    DB_CONNECTION = "db_connection"
    DB_CURSOR = "db_cursor"
    DATAFRAME = "dataframe"
    FUTURE = "future"
    UNKNOWN = "unknown"


_CALL_KINDS: dict[str, Kind] = {
    "list": Kind.LIST,
    "sorted": Kind.LIST,
    "re.findall": Kind.LIST,
    "re.split": Kind.LIST,
    "os.listdir": Kind.LIST,
    "glob.glob": Kind.LIST,
    "set": Kind.SET,
    "frozenset": Kind.SET,
    "dict": Kind.DICT,
    "collections.defaultdict": Kind.DICT,
    "collections.OrderedDict": Kind.DICT,
    "collections.Counter": Kind.DICT,
    "collections.deque": Kind.DEQUE,
    "tuple": Kind.TUPLE,
    "str": Kind.STR,
    "repr": Kind.STR,
    "range": Kind.RANGE,
    "int": Kind.NUMBER,
    "float": Kind.NUMBER,
    "len": Kind.NUMBER,
    "open": Kind.FILE,
    "requests.Session": Kind.HTTP_CLIENT,
    "requests.session": Kind.HTTP_CLIENT,
    "requests.sessions.Session": Kind.HTTP_CLIENT,
    "httpx.Client": Kind.HTTP_CLIENT,
    "urllib3.PoolManager": Kind.HTTP_CLIENT,
    "httpx.AsyncClient": Kind.ASYNC_HTTP_CLIENT,
    "aiohttp.ClientSession": Kind.ASYNC_HTTP_CLIENT,
    "sqlite3.connect": Kind.DB_CONNECTION,
    "psycopg2.connect": Kind.DB_CONNECTION,
    "psycopg.connect": Kind.DB_CONNECTION,
    "pymysql.connect": Kind.DB_CONNECTION,
    "MySQLdb.connect": Kind.DB_CONNECTION,
    "mysql.connector.connect": Kind.DB_CONNECTION,
    "pyodbc.connect": Kind.DB_CONNECTION,
    "oracledb.connect": Kind.DB_CONNECTION,
    "cx_Oracle.connect": Kind.DB_CONNECTION,
    "pandas.DataFrame": Kind.DATAFRAME,
    "pandas.concat": Kind.DATAFRAME,
}
SYNC_DB_CONNECT = frozenset(q for q, k in _CALL_KINDS.items() if k is Kind.DB_CONNECTION)
HTTP_CLIENT_CONSTRUCTORS = frozenset(
    q for q, k in _CALL_KINDS.items() if k in {Kind.HTTP_CLIENT, Kind.ASYNC_HTTP_CLIENT}
)
_STR_METHODS = frozenset(
    {"join", "format", "strip", "lstrip", "rstrip", "lower", "upper", "replace", "title"}
    | {"capitalize", "casefold", "zfill", "ljust", "rjust", "center", "decode", "removeprefix"}
    | {"removesuffix", "expandtabs", "swapcase", "format_map"}
)
_LIST_METHODS = frozenset({"split", "rsplit", "splitlines", "readlines"})
_ANNOTATION_KINDS: dict[str, Kind] = {
    "list": Kind.LIST,
    "List": Kind.LIST,
    "typing.List": Kind.LIST,
    "set": Kind.SET,
    "Set": Kind.SET,
    "typing.Set": Kind.SET,
    "frozenset": Kind.SET,
    "FrozenSet": Kind.SET,
    "typing.FrozenSet": Kind.SET,
    "dict": Kind.DICT,
    "Dict": Kind.DICT,
    "typing.Dict": Kind.DICT,
    "collections.defaultdict": Kind.DICT,
    "collections.OrderedDict": Kind.DICT,
    "collections.Counter": Kind.DICT,
    "str": Kind.STR,
    "tuple": Kind.TUPLE,
    "Tuple": Kind.TUPLE,
    "typing.Tuple": Kind.TUPLE,
    "collections.deque": Kind.DEQUE,
    "typing.Deque": Kind.DEQUE,
    "pandas.DataFrame": Kind.DATAFRAME,
    "sqlite3.Connection": Kind.DB_CONNECTION,
    "sqlite3.Cursor": Kind.DB_CURSOR,
    "requests.Session": Kind.HTTP_CLIENT,
    "httpx.Client": Kind.HTTP_CLIENT,
    "httpx.AsyncClient": Kind.ASYNC_HTTP_CLIENT,
    "aiohttp.ClientSession": Kind.ASYNC_HTTP_CLIENT,
}

NameLookup = Callable[[str], Kind]
AttrLookup = Callable[[ast.Attribute], Kind]


def _const_kind(value: object) -> Kind:
    if isinstance(value, str | bytes):
        return Kind.STR
    if isinstance(value, bool) or value is None:
        return Kind.UNKNOWN
    if isinstance(value, int | float | complex):
        return Kind.NUMBER
    return Kind.UNKNOWN


class KindInference:
    def __init__(self, aliases: ImportAliases) -> None:
        self.aliases = aliases

    def annotation_kind(self, node: ast.expr | None) -> Kind:
        if node is None:
            return Kind.UNKNOWN
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                node = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return Kind.UNKNOWN
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            sides = [self.annotation_kind(node.left), self.annotation_kind(node.right)]
            known = [k for k in sides if k is not Kind.UNKNOWN]
            none_side = any(
                isinstance(s, ast.Constant) and s.value is None for s in (node.left, node.right)
            )
            return known[0] if len(known) == 1 and none_side else Kind.UNKNOWN
        if isinstance(node, ast.Subscript):
            qualified = self.aliases.qualify(node.value) or ""
            if qualified in {"Optional", "typing.Optional"}:
                return self.annotation_kind(node.slice)
            return _ANNOTATION_KINDS.get(qualified, Kind.UNKNOWN)
        return _ANNOTATION_KINDS.get(self.aliases.qualify(node) or "", Kind.UNKNOWN)

    def expr_kind(self, node: ast.expr, names: NameLookup, attrs: AttrLookup) -> Kind:
        if isinstance(node, ast.List | ast.ListComp):
            return Kind.LIST
        if isinstance(node, ast.Set | ast.SetComp):
            return Kind.SET
        if isinstance(node, ast.Dict | ast.DictComp):
            return Kind.DICT
        if isinstance(node, ast.Tuple):
            return Kind.TUPLE
        if isinstance(node, ast.JoinedStr):
            return Kind.STR
        if isinstance(node, ast.Constant):
            return _const_kind(node.value)
        if isinstance(node, ast.Name):
            return names(node.id)
        if isinstance(node, ast.Attribute):
            return attrs(node)
        if isinstance(node, ast.IfExp):
            body = self.expr_kind(node.body, names, attrs)
            return body if body == self.expr_kind(node.orelse, names, attrs) else Kind.UNKNOWN
        if isinstance(node, ast.BinOp):
            return self._binop_kind(node, names, attrs)
        if isinstance(node, ast.Call):
            return self._call_kind(node, names, attrs)
        return Kind.UNKNOWN

    def _binop_kind(self, node: ast.BinOp, names: NameLookup, attrs: AttrLookup) -> Kind:
        left = self.expr_kind(node.left, names, attrs)
        right = self.expr_kind(node.right, names, attrs)
        if isinstance(node.op, ast.Mod) and left is Kind.STR:
            return Kind.STR
        if isinstance(node.op, ast.Mult) and Kind.LIST in (left, right):
            return Kind.LIST
        if isinstance(node.op, ast.Mult) and Kind.STR in (left, right):
            return Kind.STR
        if isinstance(node.op, ast.Add):
            if left is right and left in {Kind.LIST, Kind.STR, Kind.TUPLE}:
                return left
            literal_str = any(
                isinstance(s, ast.JoinedStr)
                or (isinstance(s, ast.Constant) and isinstance(s.value, str))
                for s in (node.left, node.right)
            )
            if literal_str:
                return Kind.STR
        if isinstance(node.op, ast.BitOr | ast.BitAnd | ast.Sub) and left is Kind.SET:
            return Kind.SET
        return Kind.UNKNOWN

    def _call_kind(self, node: ast.Call, names: NameLookup, attrs: AttrLookup) -> Kind:
        qualified = self.aliases.qualify(node.func) or ""
        if qualified in _CALL_KINDS:
            return _CALL_KINDS[qualified]
        if qualified.startswith("pandas.read_"):
            return Kind.DATAFRAME
        if not isinstance(node.func, ast.Attribute):
            return Kind.UNKNOWN
        attr = node.func.attr
        receiver_kind = self.expr_kind(node.func.value, names, attrs)
        if attr == "cursor" and receiver_kind is Kind.DB_CONNECTION:
            return Kind.DB_CURSOR
        if attr == "submit":
            return Kind.FUTURE
        if attr == "copy" and receiver_kind in {Kind.LIST, Kind.SET, Kind.DICT, Kind.DATAFRAME}:
            return receiver_kind
        if attr in _STR_METHODS and receiver_kind is Kind.STR:
            return Kind.STR
        if attr in _LIST_METHODS and receiver_kind in {Kind.STR, Kind.FILE}:
            return Kind.LIST
        return Kind.UNKNOWN

    def scope_kinds(
        self,
        body: Iterable[ast.AST],
        params: ast.arguments | None,
        outer: NameLookup,
        attrs: AttrLookup,
    ) -> tuple[dict[str, Kind], set[str]]:
        """Kinds of names bound in a scope plus the names declared ``global``/``nonlocal``."""
        kinds: dict[str, Kind] = {}
        declared_outer: set[str] = set()
        nodes = list(iter_own_nodes(list(body)))
        for node in nodes:
            if isinstance(node, ast.Global | ast.Nonlocal):
                declared_outer.update(node.names)

        def lookup(name: str) -> Kind:
            return kinds[name] if name in kinds else outer(name)

        def bind(name: str, kind: Kind) -> None:
            if name in declared_outer:
                return
            previous = kinds.get(name)
            kinds[name] = kind if previous is None or previous is kind else Kind.UNKNOWN

        if params is not None:
            for arg in [*params.posonlyargs, *params.args, *params.kwonlyargs]:
                bind(arg.arg, self.annotation_kind(arg.annotation))
            if params.vararg:
                bind(params.vararg.arg, Kind.TUPLE)
            if params.kwarg:
                bind(params.kwarg.arg, Kind.DICT)

        for node in nodes:
            if isinstance(node, ast.Assign):
                kind = self.expr_kind(node.value, lookup, attrs)
                for target in node.targets:
                    self._bind_target(target, kind, bind)
            elif isinstance(node, ast.AnnAssign):
                kind = self.annotation_kind(node.annotation)
                if kind is Kind.UNKNOWN and node.value is not None:
                    kind = self.expr_kind(node.value, lookup, attrs)
                self._bind_target(node.target, kind, bind)
            elif isinstance(node, ast.For | ast.AsyncFor):
                self._bind_target(node.target, Kind.UNKNOWN, bind)
            elif isinstance(node, ast.With | ast.AsyncWith):
                for item in node.items:
                    if item.optional_vars is not None:
                        kind = self.expr_kind(item.context_expr, lookup, attrs)
                        self._bind_target(item.optional_vars, kind, bind)
            elif isinstance(node, ast.NamedExpr):
                bind(node.target.id, self.expr_kind(node.value, lookup, attrs))
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bind(node.name, Kind.UNKNOWN)
            elif isinstance(node, ast.Import | ast.ImportFrom):
                for alias in node.names:
                    bind((alias.asname or alias.name).split(".", 1)[0], Kind.UNKNOWN)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                bind(node.name, Kind.UNKNOWN)
        return kinds, declared_outer

    @staticmethod
    def _bind_target(target: ast.expr, kind: Kind, bind: Callable[[str, Kind], None]) -> None:
        if isinstance(target, ast.Name):
            bind(target.id, kind)
        elif isinstance(target, ast.Tuple | ast.List):
            for element in target.elts:
                KindInference._bind_target(element, Kind.UNKNOWN, bind)
        elif isinstance(target, ast.Starred):
            KindInference._bind_target(target.value, Kind.LIST, bind)
