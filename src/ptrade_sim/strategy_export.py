"""Static PTrade single-file exports; conversion never imports or executes a strategy.

Source edits are AST-located imports, simulator guards and proven API attribute tokens.
Embedded modules are loaded lazily in a private namespace at deployment time, not
at conversion time. Parsing/import discovery are linear; receiver facts use a bounded
monotone fixed point over source bindings (worst case O(nodes * bindings * type families)).
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import io
import json
import os
import re
import symtable
import tempfile
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ptrade_sim.config import STRATEGY_CONFIG, _deep_merge, resolve_strategy
from ptrade_sim.exceptions import StrategyConfigError, StrategyError, StrategyImportError

__all__ = ["ExportResult", "convert_strategy", "write_export"]

# Names and deprecated arguments are taken from docs/ptrade_api.md, including its
# version-change section. No simulator API implementation is imported here.
_OFFICIAL_API = frozenset(
    [
        "set_universe",
        "set_benchmark",
        "set_commission",
        "set_fixed_slippage",
        "set_slippage",
        "set_volume_ratio",
        "set_limit_mode",
        "set_yesterday_position",
        "set_parameters",
        "set_email_info",
        "run_daily",
        "run_interval",
        "get_trading_day",
        "get_all_trades_days",
        "get_trade_days",
        "get_trading_day_by_date",
        "get_market_list",
        "get_market_detail",
        "get_history",
        "get_price",
        "get_individual_entrust",
        "get_individual_transaction",
        "get_tick_direction",
        "get_sort_msg",
        "get_gear_price",
        "get_snapshot",
        "get_trend_data",
        "get_stock_name",
        "get_stock_info",
        "get_stock_status",
        "get_underlying_code",
        "get_stock_exrights",
        "get_stock_blocks",
        "get_index_stocks",
        "get_industry_stocks",
        "get_fundamentals",
        "get_Ashares",
        "get_etf_list",
        "get_etf_info",
        "get_etf_stock_list",
        "get_etf_stock_info",
        "get_ipo_stocks",
        "get_cb_list",
        "get_cb_info",
        "get_reits_list",
        "get_position",
        "get_positions",
        "get_all_positions",
        "get_trades_file",
        "convert_position_from_csv",
        "get_user_name",
        "get_deliver",
        "get_fundjour",
        "get_research_path",
        "get_trade_name",
        "get_lucky_info",
        "order",
        "order_target",
        "order_value",
        "order_target_value",
        "order_market",
        "ipo_stocks_order",
        "after_trading_order",
        "after_trading_cancel_order",
        "etf_basket_order",
        "etf_purchase_redemption",
        "order_tick",
        "cancel_order",
        "cancel_order_ex",
        "debt_to_stock_order",
        "get_open_orders",
        "get_order",
        "get_orders",
        "get_all_orders",
        "get_trades",
        "margin_trade",
        "margincash_open",
        "margincash_close",
        "margincash_direct_refund",
        "marginsec_open",
        "marginsec_close",
        "marginsec_direct_refund",
        "get_margincash_stocks",
        "get_marginsec_stocks",
        "get_margin_contract",
        "get_margin_contractreal",
        "get_margin_asset",
        "get_assure_security_list",
        "get_margincash_open_amount",
        "get_margincash_close_amount",
        "get_marginsec_open_amount",
        "get_marginsec_close_amount",
        "get_margin_entrans_amount",
        "get_enslo_security_info",
        "get_crdt_fund",
        "buy_open",
        "sell_close",
        "sell_open",
        "buy_close",
        "get_margin_rate",
        "get_instruments",
        "get_dominant_contract",
        "set_future_commission",
        "set_margin_rate",
        "get_opt_objects",
        "get_opt_last_dates",
        "get_opt_contracts",
        "get_contract_info",
        "get_covered_lock_amount",
        "get_covered_unlock_amount",
        "option_buy_open",
        "option_sell_close",
        "option_sell_open",
        "option_buy_close",
        "open_prepared",
        "close_prepared",
        "option_exercise",
        "option_covered_lock",
        "option_covered_unlock",
        "get_hks_list",
        "get_hks_price_gap",
        "get_hks_unit_amount",
        "hks_order",
        "hks_odd_lot_order",
        "get_MACD",
        "get_KDJ",
        "get_RSI",
        "get_CCI",
        "is_trade",
        "check_limit",
        "send_email",
        "send_qywx",
        "permission_test",
        "create_dir",
        "get_frequency",
        "get_business_type",
        "get_current_kline_count",
        "filter_stock_by_status",
        "check_strategy",
        "fund_transfer",
        "market_fund_transfer",
    ]
)
_API_GLOBALS = _OFFICIAL_API | {"g", "log", "get_strategy_params"}
_CALLBACKS = {"initialize": 1, "handle_data": 2}
# The local documentation explicitly uses/records these libraries. Their installed
# versions and the client runtime are not inferred from this machine's packages.
_DOCUMENTED_LIBRARIES = frozenset({"numpy", "pandas", "talib"})
_STDLIB = frozenset(
    [
        "__future__",
        "abc",
        "aifc",
        "argparse",
        "array",
        "ast",
        "asynchat",
        "asyncio",
        "asyncore",
        "base64",
        "bdb",
        "binascii",
        "bisect",
        "builtins",
        "bz2",
        "calendar",
        "cgi",
        "cgitb",
        "chunk",
        "cmath",
        "cmd",
        "code",
        "codecs",
        "codeop",
        "collections",
        "colorsys",
        "compileall",
        "concurrent",
        "configparser",
        "contextlib",
        "contextvars",
        "copy",
        "copyreg",
        "cProfile",
        "csv",
        "ctypes",
        "curses",
        "dataclasses",
        "datetime",
        "dbm",
        "decimal",
        "difflib",
        "dis",
        "distutils",
        "doctest",
        "email",
        "encodings",
        "ensurepip",
        "enum",
        "errno",
        "faulthandler",
        "fcntl",
        "filecmp",
        "fileinput",
        "fnmatch",
        "fractions",
        "ftplib",
        "functools",
        "gc",
        "getopt",
        "getpass",
        "gettext",
        "glob",
        "graphlib",
        "gzip",
        "hashlib",
        "heapq",
        "hmac",
        "html",
        "http",
        "imaplib",
        "imghdr",
        "importlib",
        "inspect",
        "io",
        "ipaddress",
        "itertools",
        "json",
        "keyword",
        "linecache",
        "locale",
        "logging",
        "lzma",
        "mailbox",
        "mailcap",
        "marshal",
        "math",
        "mimetypes",
        "mmap",
        "modulefinder",
        "multiprocessing",
        "netrc",
        "nis",
        "nntplib",
        "numbers",
        "operator",
        "optparse",
        "os",
        "pathlib",
        "pdb",
        "pickle",
        "pickletools",
        "pipes",
        "pkgutil",
        "platform",
        "plistlib",
        "poplib",
        "posix",
        "pprint",
        "profile",
        "pstats",
        "pty",
        "pwd",
        "py_compile",
        "pyclbr",
        "pydoc",
        "queue",
        "quopri",
        "random",
        "re",
        "readline",
        "reprlib",
        "resource",
        "rlcompleter",
        "runpy",
        "sched",
        "secrets",
        "select",
        "selectors",
        "shelve",
        "shlex",
        "shutil",
        "signal",
        "site",
        "smtpd",
        "smtplib",
        "sndhdr",
        "socket",
        "socketserver",
        "sqlite3",
        "ssl",
        "stat",
        "statistics",
        "string",
        "stringprep",
        "struct",
        "subprocess",
        "sunau",
        "symtable",
        "sys",
        "sysconfig",
        "tabnanny",
        "tarfile",
        "telnetlib",
        "tempfile",
        "textwrap",
        "threading",
        "time",
        "timeit",
        "tkinter",
        "token",
        "tokenize",
        "tomllib",
        "trace",
        "traceback",
        "tracemalloc",
        "tty",
        "turtle",
        "types",
        "typing",
        "unicodedata",
        "unittest",
        "urllib",
        "uu",
        "uuid",
        "venv",
        "warnings",
        "wave",
        "weakref",
        "webbrowser",
        "wsgiref",
        "xdrlib",
        "xml",
        "xmlrpc",
        "zipapp",
        "zipfile",
        "zipimport",
        "zlib",
        "zoneinfo",
    ]
)
_MODULE_MIN_VERSION = {
    "contextvars": (3, 7),
    "dataclasses": (3, 7),
    "graphlib": (3, 9),
    "secrets": (3, 6),
    "tomllib": (3, 11),
    "zoneinfo": (3, 9),
}
_API_KEYWORDS = {
    "get_history": {
        "count",
        "frequency",
        "field",
        "security_list",
        "fq",
        "include",
        "fill",
        "is_dict",
    },
    "get_price": {
        "security",
        "start_date",
        "end_date",
        "frequency",
        "fields",
        "fq",
        "count",
        "is_dict",
    },
    "get_fundamentals": {
        "security",
        "table",
        "fields",
        "date",
        "start_year",
        "end_year",
        "report_types",
        "merge_type",
        "is_dataframe",
    },
    "get_strategy_params": {"key", "default"},
}
_DEPRECATED_KEYWORDS = {
    "get_fundamentals": {"date_type"},
    "get_hks_unit_amount": {"entrust_type"},
    "ipo_stocks_order": {"market_type"},
    "set_parameters": {"individual_data_in_dict", "tick_direction_in_dict"},
}
_MAX_SOURCE_BYTES = 16 * 1024 * 1024
_MAX_MODULES = 512
_LOG_LEVELS = frozenset({"info", "warning", "error", "debug"})
_DUNDER_GLOBALS = frozenset(
    {"__name__", "__file__", "__package__", "__builtins__", "__doc__", "__loader__", "__spec__"}
)


@dataclass(frozen=True)
class ExportResult:
    """JSON-ready report and UTF-8 source; blocked exports have an empty source."""

    source: str
    filename: str
    report: dict[str, Any]
    _protected_paths: tuple[Path, ...] = field(default=(), repr=False, compare=False)


@dataclass
class _Module:
    name: str
    path: Path | None
    relative_path: str
    package: bool
    source: str = ""
    tree: ast.Module | None = None
    sha256: str | None = None
    size: int = 0
    transformed: str = ""
    patches: list[tuple[int, int, str]] = field(default_factory=list)
    removed_guards: set[int] = field(default_factory=set)
    trimmed_aliases: set[str] = field(default_factory=set)
    bindings: dict[str, str] = field(default_factory=dict)
    bound_names: set[str] | None = None
    star_exports: set[str] = field(default_factory=set)
    star_imports: list[tuple[str, ast.ImportFrom]] = field(default_factory=list)
    source_lines: list[str] = field(default_factory=list)
    source_starts: list[int] = field(default_factory=list)


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _json_params(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StrategyConfigError(f"{label} 必须为 JSON 对象")

    # Do not silently coerce tuple/set, non-string keys, NaN or custom objects.
    def validate(item: Any) -> None:
        if item is None or isinstance(item, (str, bool, int, float)):
            return
        if isinstance(item, list):
            for child in item:
                validate(child)
            return
        if isinstance(item, dict) and all(isinstance(k, str) for k in item):
            for child in item.values():
                validate(child)
            return
        raise StrategyConfigError(f"{label} 包含非 JSON 类型或非字符串键")

    try:
        validate(value)
        return json.loads(_json(value))
    except (ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, StrategyConfigError):
            raise
        raise StrategyConfigError(f"{label} 不是有限、无循环的 JSON 对象：{exc}") from exc


def _target_version(target_python: str) -> tuple[int, int]:
    if target_python not in {"3.5", "3.11"}:
        raise StrategyConfigError("target_python 必须为官方文档列出的 '3.5' 或 '3.11'")
    return 3, int(target_python.split(".")[1])


def _span(
    source: str, node: ast.AST, lines: list[str] | None = None, starts: list[int] | None = None
) -> tuple[int, int]:
    """AST columns count UTF-8 bytes, not Unicode codepoints."""
    if lines is None or starts is None:
        lines = source.splitlines(keepends=True)
        starts = [0]
        for line in lines:
            starts.append(starts[-1] + len(line))
    first = getattr(node, "lineno", 1) - 1
    last = getattr(node, "end_lineno", first + 1) - 1
    col = getattr(node, "col_offset", 0)
    end = getattr(node, "end_col_offset", col)
    return (
        starts[first] + len(lines[first].encode("utf-8")[:col].decode("utf-8")),
        starts[last] + len(lines[last].encode("utf-8")[:end].decode("utf-8")),
    )


def _patch(module: _Module, node: ast.AST, replacement: str) -> None:
    start, end = _span(module.source, node, module.source_lines, module.source_starts)
    # Keep original line numbers for diagnostics after import/guard edits.
    missing = module.source[start:end].count("\n") - replacement.count("\n")
    if missing > 0:
        replacement += "\n" * missing
    module.patches.append((start, end, replacement))


def _apply_patches(module: _Module) -> str:
    chunks: list[str] = []
    boundary = 0
    for start, end, replacement in sorted(module.patches):
        if start < boundary:
            raise StrategyError("策略转换的 AST 源码补丁发生重叠")
        chunks.extend((module.source[boundary:start], replacement))
        boundary = end
    chunks.append(module.source[boundary:])
    return "".join(chunks)


def _walk_live(module: _Module):
    def walk(node: ast.AST):
        if id(node) in module.removed_guards:
            return
        yield node
        for child in ast.iter_child_nodes(node):
            yield from walk(child)

    if module.tree is not None:
        yield from walk(module.tree)


def _main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If) or node.orelse:
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    left, right = test.left, test.comparators[0]
    return isinstance(test.ops[0], ast.Eq) and (
        (
            isinstance(left, ast.Name)
            and left.id == "__name__"
            and isinstance(right, ast.Constant)
            and right.value == "__main__"
        )
        or (
            isinstance(right, ast.Name)
            and right.id == "__name__"
            and isinstance(left, ast.Constant)
            and left.value == "__main__"
        )
    )


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        if base:
            return base + "." + node.attr
    return None


class _GlobalReferences(ast.NodeVisitor):
    """Use the compiler's symbol tables so local shadowing is not an API error."""

    def __init__(self, tree: ast.Module, text: str, filename: str):
        self.table = symtable.symtable(text, filename, "exec")
        self.root = self.table
        self.bound = {
            s.get_name()
            for s in self.root.get_symbols()
            if s.is_assigned() or s.is_imported() or s.is_namespace()
        }
        self.used_children: set[int] = set()
        self.child_tables: dict[int, list[Any]] = {}
        self.deferred_annotations = any(
            isinstance(n, ast.ImportFrom)
            and n.module == "__future__"
            and any(a.name == "annotations" for a in n.names)
            for n in tree.body
        )
        self.local_overrides: list[set[str]] = []
        self.references: list[ast.Name] = []
        self.visit(tree)

    def visit_Name(self, node: ast.Name) -> None:
        if (
            not isinstance(node.ctx, ast.Load)
            or node.id in self.bound
            or any(node.id in names for names in self.local_overrides)
        ):
            return
        symbol = self.table.lookup(node.id)
        if symbol.is_global():
            self.references.append(node)

    def _body(self, node: ast.AST, name: str, body: list[ast.AST]) -> None:
        node_line = getattr(node, "lineno", 0)
        children = [
            child
            for child in self.child_tables.setdefault(id(self.table), self.table.get_children())
            if id(child) not in self.used_children
            and child.get_name() == name
            and child.get_lineno() == node_line
        ]
        if not children:
            if name in {"listcomp", "setcomp", "dictcomp"}:
                for statement in body:
                    self.visit(statement)
                return
            raise StrategyError(f"静态作用域定位失败：{name}:{node_line}")
        child = children[0]
        self.used_children.add(id(child))
        parent = self.table
        self.table = child
        try:
            for statement in body:
                self.visit(statement)
        finally:
            self.table = parent

    def _arguments(self, args: ast.arguments) -> None:
        for value in [*args.defaults, *args.kw_defaults]:
            if value is not None:
                self.visit(value)
        if self.deferred_annotations:
            return
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if arg.annotation is not None:
                self.visit(arg.annotation)
        for variadic in (args.vararg, args.kwarg):
            if variadic is not None and variadic.annotation is not None:
                self.visit(variadic.annotation)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        self._arguments(node.args)
        if node.returns is not None and not self.deferred_annotations:
            self.visit(node.returns)
        self._body(node, node.name, list(node.body))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)
        if not self.deferred_annotations:
            self.visit(node.annotation)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expression in [*node.decorator_list, *node.bases]:
            self.visit(expression)
        for keyword in node.keywords:
            self.visit(keyword.value)
        self._body(node, node.name, list(node.body))

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._arguments(node.args)
        self._body(node, "lambda", [node.body])

    def _comprehension(self, node: Any, name: str, values: list[ast.AST]) -> None:
        self.visit(node.generators[0].iter)
        inner: list[ast.AST] = []
        for index, generator in enumerate(node.generators):
            inner.append(generator.target)
            if index:
                inner.append(generator.iter)
            inner.extend(generator.ifs)
        names = {
            n.id
            for generator in node.generators
            for n in ast.walk(generator.target)
            if isinstance(n, ast.Name)
        }
        self.local_overrides.append(names)
        try:
            self._body(node, name, [*inner, *values])
        finally:
            self.local_overrides.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._comprehension(node, "listcomp", [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._comprehension(node, "setcomp", [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._comprehension(node, "dictcomp", [node.key, node.value])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._comprehension(node, "genexpr", [node.elt])


class _AttributeFacts:
    """Finite path-sensitive receiver facts; no expressions are evaluated."""

    def __init__(
        self,
        function: ast.FunctionDef,
        context_name: str | None,
        classes: set[str],
        unbound: set[int],
        imports: dict[str, str],
    ):
        self.classes, self.unbound, self.imports = classes, unbound, imports
        self.aliases: dict[int, tuple[ast.Attribute, set[str]]] = {}
        env = {
            a.arg: {"unknown"}
            for a in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
        }
        if context_name is not None:
            env[context_name], env["@portfolio"], env["@positions"] = (
                {"context"},
                {"portfolio"},
                {"position_map"},
            )
        if function.args.args and function.args.args[0].arg in {"self", "cls"}:
            env[function.args.args[0].arg] = {"custom"}
        self.block(function.body, env)

    @staticmethod
    def join(*states: dict[str, set[str]]) -> dict[str, set[str]]:
        keys = set().union(*(s.keys() for s in states))
        return {k: set().union(*(s.get(k, {"unknown"}) for s in states)) for k in keys}

    @staticmethod
    def copy(env: dict[str, set[str]]) -> dict[str, set[str]]:
        return {k: set(v) for k, v in env.items()}

    def kind(self, node: ast.AST, env: dict[str, set[str]]) -> set[str]:
        if isinstance(node, ast.Name):
            return env.get(node.id, {"unknown"})
        if isinstance(node, ast.Constant):
            return {"none"} if node.value is None else {"value"}
        if isinstance(node, ast.Attribute):
            base = self.kind(node.value, env)
            if base == {"context"} and node.attr == "portfolio":
                return env.get("@portfolio", {"portfolio"})
            if base == {"portfolio"} and node.attr == "positions":
                return env.get("@positions", {"position_map"})
        if isinstance(node, ast.Subscript) and self.kind(node.value, env) == {"position_map"}:
            return {"position"}
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in self.classes:
                    return {"custom"}
                if id(node.func) in self.unbound:
                    if node.func.id == "get_position":
                        return {"position"}
                    if node.func.id == "get_positions":
                        return {"position_map"}
                    if node.func.id in {"get_fundamentals", "get_price", "get_history"}:
                        return {"table"}
            if isinstance(node.func, ast.Attribute):
                if self.kind(node.func.value, env) == {"position_map"}:
                    if node.func.attr == "values":
                        return {"position_iter"}
                    if node.func.attr == "items":
                        return {"position_items"}
                    if node.func.attr == "get":
                        return {"position"} | (
                            self.kind(node.args[1], env) if len(node.args) > 1 else {"none"}
                        )
                dotted = _dotted(node.func)
                if dotted:
                    first, _, tail = dotted.partition(".")
                    if self.imports.get(first) == "pandas" and tail == "DataFrame":
                        return {"table"}
        return {"unknown"}

    def expression(self, node: ast.AST, env: dict[str, set[str]]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(node, ast.Attribute) and node.attr in {"total_value", "closeable_amount"}:
            self.aliases.setdefault(id(node), (node, set()))[1].update(self.kind(node.value, env))
        for child in ast.iter_child_nodes(node):
            self.expression(child, env)

    def bind(self, node: ast.AST, kinds: set[str], env: dict[str, set[str]]) -> None:
        if isinstance(node, ast.Name):
            env[node.id] = set(kinds)
        elif isinstance(node, (ast.List, ast.Tuple)):
            for target in node.elts:
                self.bind(target, {"unknown"}, env)
        elif isinstance(node, ast.Attribute):
            base = self.kind(node.value, env)
            if base == {"context"} and node.attr == "portfolio":
                env["@portfolio"] = set(kinds)
            elif base == {"portfolio"} and node.attr == "positions":
                env["@positions"] = {"unknown"}

    def narrowed(self, test: ast.AST, env: dict[str, set[str]], truth: bool) -> dict[str, set[str]]:
        out = self.copy(env)
        name: str | None = None
        non_none = False
        if isinstance(test, ast.Name) and truth:
            name, non_none = test.id, True
        elif (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and len(test.ops) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None
            and isinstance(test.ops[0], (ast.Is, ast.IsNot))
        ):
            name = test.left.id
            non_none = truth == isinstance(test.ops[0], ast.IsNot)
        if name is not None and name in out:
            out[name] = out[name] - {"none"} if non_none else {"none"}
        return out

    def block(
        self,
        statements: list[ast.stmt],
        env: dict[str, set[str]],
        trace: list[dict[str, set[str]]] | None = None,
    ) -> dict[str, set[str]]:
        env = self.copy(env)
        for statement in statements:
            if trace is not None:
                trace.append(self.copy(env))
            if isinstance(statement, ast.If):
                self.expression(statement.test, env)
                yes = self.block(statement.body, self.narrowed(statement.test, env, True), trace)
                no = self.block(statement.orelse, self.narrowed(statement.test, env, False), trace)
                env = self.join(yes, no)
            elif isinstance(statement, (ast.For, ast.While)):
                initial, entry = self.copy(env), self.copy(env)
                if isinstance(statement, ast.For):
                    self.expression(statement.iter, env)
                    iterator = self.kind(statement.iter, env)
                    if (
                        iterator == {"position_items"}
                        and isinstance(statement.target, (ast.List, ast.Tuple))
                        and len(statement.target.elts) == 2
                    ):
                        self.bind(statement.target.elts[0], {"value"}, entry)
                        self.bind(statement.target.elts[1], {"position"}, entry)
                    else:
                        self.bind(
                            statement.target,
                            {"position"} if iterator == {"position_iter"} else {"unknown"},
                            entry,
                        )
                else:
                    self.expression(statement.test, env)
                    entry = self.narrowed(statement.test, entry, True)
                loop = self.copy(entry)
                while True:
                    body = self.block(statement.body, loop, trace)
                    next_loop = self.join(entry, body)
                    if isinstance(statement, ast.While):
                        next_loop = self.narrowed(statement.test, next_loop, True)
                    if next_loop == loop:
                        break
                    loop = next_loop
                env = self.block(statement.orelse, self.join(initial, body), trace)
            elif isinstance(statement, ast.Try) or (
                hasattr(ast, "TryStar") and isinstance(statement, ast.TryStar)
            ):
                intermediate: list[dict[str, set[str]]] = [self.copy(env)]
                normal = self.block(statement.body, env, intermediate)
                normal = self.block(statement.orelse, normal, trace)
                handler_env = self.join(*intermediate, normal)
                exits = [normal]
                for handler in statement.handlers:
                    state = self.copy(handler_env)
                    if handler.name:
                        state[handler.name] = {"custom"}
                    exits.append(self.block(handler.body, state, trace))
                env = self.block(statement.finalbody, self.join(*exits, handler_env), trace)
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    self.expression(item.context_expr, env)
                    if item.optional_vars is not None:
                        self.bind(item.optional_vars, {"unknown"}, env)
                env = self.block(statement.body, env, trace)
            else:
                self.expression(statement, env)
                if isinstance(statement, ast.Assign):
                    kinds = self.kind(statement.value, env)
                    for target in statement.targets:
                        self.bind(target, kinds, env)
                elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                    self.bind(statement.target, self.kind(statement.value, env), env)
                elif isinstance(statement, ast.AugAssign):
                    self.bind(statement.target, {"unknown"}, env)
                elif isinstance(statement, ast.Delete):
                    for target in statement.targets:
                        self.bind(target, {"unknown"}, env)
        if trace is not None:
            trace.append(self.copy(env))
        return env


class _Converter:
    def __init__(
        self,
        target: str | Path,
        params: dict[str, Any] | None,
        target_python: str,
        params_mode: str,
    ):
        if params_mode not in {"merge", "replace"}:
            raise StrategyConfigError("params_mode 必须为 'merge' 或 'replace'")
        if params_mode == "replace" and params is None:
            raise StrategyConfigError("params_mode='replace' 必须显式提供最终 params 对象")
        self.params_mode = params_mode
        self.version = _target_version(target_python)
        self.target_python = target_python
        self.bundle = resolve_strategy(target)
        self.root = self.bundle.dir.resolve()
        self.entry_path = self.bundle.py.resolve()
        if not self.entry_path.is_relative_to(self.root):
            raise StrategyConfigError("策略入口解析到策略目录之外")
        # resolve_strategy deliberately ignores damaged backtest config files. An
        # export must not silently lose production params, so validate this file.
        config_path = self.root / STRATEGY_CONFIG
        config = self.bundle.config
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, UnicodeError) as exc:
                raise StrategyConfigError(f"策略配置读取失败：{config_path}：{exc}") from exc
            if not isinstance(config, dict):
                raise StrategyConfigError(f"策略配置必须为 JSON 对象：{config_path}")
        base = (
            _json_params(config.get("params", {}), "策略配置 params")
            if params_mode == "merge"
            else {}
        )
        override = _json_params(params, "显式 params") if params is not None else {}
        self.params = override if params_mode == "replace" else _deep_merge(base, override)
        stem = re.sub(r"[^\w.-]+", "_", self.bundle.stem).strip("._") or "strategy"
        self.filename = stem + "_ptrade.py"
        self.diagnostics: list[dict[str, Any]] = []
        self.dependencies: list[dict[str, Any]] = []
        self.api_used: set[str] = set()
        self.modules: dict[str, _Module] = {}
        self.candidates: dict[str, tuple[Path | None, str, bool] | None] = {}
        self.pending: list[_Module] = []
        self.total_bytes = 0
        self.protected: set[Path] = {self.entry_path}
        if config_path.exists():
            self.protected.add(config_path.resolve())
        relative = self.entry_path.relative_to(self.root).as_posix()
        self.entry = _Module("", self.entry_path, relative, False)
        self._read(self.entry)
        fingerprint = hashlib.sha256(
            (str(self.entry.sha256) + _json(self.params) + target_python).encode("utf-8")
        ).hexdigest()[:16]
        self.prefix = "_pt_export_" + fingerprint

    def diagnostic(
        self,
        module: _Module,
        node: ast.AST | None,
        code: str,
        message: str,
        severity: str = "error",
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        self.diagnostics.append(
            {
                "severity": severity,
                "code": code,
                "path": module.relative_path,
                "line": line if line is not None else getattr(node, "lineno", None),
                "column": column
                if column is not None
                else (getattr(node, "col_offset", -1) + 1 if node is not None else None),
                "message": message,
            }
        )

    def _read(self, module: _Module) -> None:
        if module.path is None:
            module.tree = ast.Module(body=[], type_ignores=[])
            return
        self.protected.add(module.path)
        try:
            if module.path.stat().st_size + self.total_bytes > _MAX_SOURCE_BYTES:
                self.diagnostic(module, None, "source_limit", "策略源码总大小超过静态转换预算")
                return
            raw = module.path.read_bytes()
        except OSError as exc:
            raise StrategyImportError(f"策略源码读取失败：{module.path}：{exc}") from exc
        module.sha256 = hashlib.sha256(raw).hexdigest()
        module.size = len(raw)
        self.total_bytes += module.size
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
            module.source = raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
        except (SyntaxError, UnicodeError, LookupError) as exc:
            self.diagnostic(module, None, "source_encoding", f"Python 源码编码无效：{exc}", line=1)
            return
        try:
            module.source_lines = module.source.splitlines(keepends=True)
            module.source_starts = [0]
            for line in module.source_lines:
                module.source_starts.append(module.source_starts[-1] + len(line))
            module.tree = ast.parse(
                module.source, filename=module.relative_path, feature_version=self.version
            )
            # Some host parsers accept f-strings despite feature_version=3.5.
            # Guard AST feature families explicitly instead of claiming that
            # best-effort parser flags reproduce an older interpreter.
            minimums = [
                (ast.JoinedStr, (3, 6)),
                (ast.AnnAssign, (3, 6)),
                (ast.NamedExpr, (3, 8)),
                (ast.Match, (3, 10)),
            ]
            if hasattr(ast, "TryStar"):
                minimums.append((ast.TryStar, (3, 11)))
            for node in ast.walk(module.tree):
                minimum = next((v for kind, v in minimums if isinstance(node, kind)), None)
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                    and node.args.posonlyargs
                ):
                    minimum = (3, 8)
                if isinstance(node, ast.comprehension) and node.is_async:
                    minimum = (3, 6)
                if minimum is not None and minimum > self.version:
                    self.diagnostic(
                        module,
                        node,
                        "source_syntax",
                        f"语法节点 {type(node).__name__} 至少需要 Python {minimum[0]}.{minimum[1]}",
                    )
            # Parsing alone permits compiler-only errors (e.g. return outside a
            # function). compile builds a code object but never evaluates it.
            compile(module.tree, module.relative_path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as exc:
            self.diagnostic(
                module,
                None,
                "source_syntax",
                f"目标 Python {self.target_python} 语法无效：{exc}",
                line=getattr(exc, "lineno", None),
                column=getattr(exc, "offset", None),
            )
            module.tree = None

    def _lookup(
        self, name: str, origin: _Module, node: ast.AST
    ) -> tuple[Path | None, str, bool] | None:
        if name in self.candidates:
            return self.candidates[name]
        if not name or any(not part.isidentifier() for part in name.split(".")):
            return None
        if "." in name:
            parent = self._lookup(name.rsplit(".", 1)[0], origin, node)
            if parent is None or not parent[2]:
                self.candidates[name] = None
                return None
        base = self.root.joinpath(*name.split("."))
        init = base / "__init__.py"
        candidate: Path | None = None
        package = False
        if init.is_file():
            candidate, package = init, True
        elif base.with_suffix(".py").is_file():
            candidate = base.with_suffix(".py")
        elif base.is_dir():
            # Namespace packages have no source and still own their children.
            resolved = base.resolve()
            if not resolved.is_relative_to(self.root):
                self.diagnostic(
                    origin, node, "local_import_escape", f"本地包 {name} 指向策略目录之外"
                )
                self.candidates[name] = None
                return None
            namespace_candidate = (None, base.relative_to(self.root).as_posix() + "/", True)
            self.candidates[name] = namespace_candidate
            return namespace_candidate
        if candidate is None:
            self.candidates[name] = None
            return None
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            self.diagnostic(
                origin, node, "local_import_escape", f"本地模块 {name} 指向策略目录之外"
            )
            self.candidates[name] = None
            return None
        value = (resolved, candidate.relative_to(self.root).as_posix(), package)
        self.candidates[name] = value
        return value

    def _add(self, name: str, origin: _Module, node: ast.AST) -> bool:
        candidate = self._lookup(name, origin, node)
        if candidate is None:
            return False
        if name in self.modules:
            return True
        if candidate[0] == self.entry_path:
            self.diagnostic(
                origin, node, "entry_reimport", "helper 重新导入入口文件会产生另一份策略状态"
            )
            return True
        if len(self.modules) >= _MAX_MODULES:
            self.diagnostic(origin, node, "module_limit", "本地模块数量超过静态转换预算")
            return True
        if "." in name:
            self._add(name.rsplit(".", 1)[0], origin, node)
        module = _Module(name, *candidate)
        self.modules[name] = module
        self._read(module)
        self.pending.append(module)
        return True

    def _dependency(self, name: str, origin: _Module, node: ast.AST) -> bool:
        root = name.split(".")[0]
        if root == "ptrade_sim":
            kind = "simulator"
            self.diagnostic(origin, node, "simulator_dependency", f"未剔除的模拟器依赖：{name}")
        elif root in _STDLIB:
            kind = "stdlib"
            if _MODULE_MIN_VERSION.get(root, (3, 0)) > self.version:
                self.diagnostic(
                    origin,
                    node,
                    "dependency_python_version",
                    f"{root} 不属于目标 Python {self.target_python} 的标准库",
                )
        elif root in _DOCUMENTED_LIBRARIES:
            kind = "documented_third_party"
        else:
            kind = "unknown"
            self.diagnostic(
                origin,
                node,
                "unknown_dependency",
                f"依赖 {name} 未在策略目录打包，且本地官方文档未确认其可用",
            )
        self.dependencies.append(
            {
                "name": name,
                "kind": kind,
                "path": origin.relative_path,
                "line": getattr(node, "lineno", None),
            }
        )
        return kind in {"stdlib", "documented_third_party"}

    def _strip_bootstrap(self, module: _Module) -> None:
        if module.tree is None:
            return
        sim_aliases: set[str] = set()
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Import):
                sim_aliases.update(
                    a.asname or a.name.split(".")[0]
                    for a in node.names
                    if a.name.split(".")[0] == "ptrade_sim"
                )
            elif (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").split(".")[0] == "ptrade_sim"
            ):
                sim_aliases.update(a.asname or a.name for a in node.names)
        for node in module.tree.body:
            if not _main_guard(node):
                continue
            nodes = list(ast.walk(node))
            explicit = any(
                isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in sim_aliases
                for n in nodes
            ) or any(
                (
                    isinstance(n, ast.Import)
                    and any(a.name.split(".")[0] == "ptrade_sim" for a in n.names)
                )
                or (
                    isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] == "ptrade_sim"
                )
                for n in nodes
            )
            if explicit:
                module.removed_guards.add(id(node))
                _patch(module, node, "pass")
                self.diagnostic(
                    module,
                    node,
                    "simulator_bootstrap_removed",
                    "已剔除只在 __main__ 中运行的明确模拟器启动块",
                    "info",
                )
        if module.removed_guards:
            live_names = {
                n.id
                for n in _walk_live(module)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            }
            # __all__ is also a use of an imported binding, despite being strings.
            for n in _walk_live(module):
                if (
                    isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "__all__" for t in n.targets)
                    and isinstance(n.value, (ast.Tuple, ast.List))
                ):
                    live_names.update(
                        x.value
                        for x in n.value.elts
                        if isinstance(x, ast.Constant) and isinstance(x.value, str)
                    )
            module.trimmed_aliases = sim_aliases - live_names

    def _logger_allowed(self, module: _Module, alias: str) -> bool:
        nodes = list(_walk_live(module))
        parents = {id(c): p for p in nodes for c in ast.iter_child_nodes(p)}
        for n in nodes:
            if isinstance(n, ast.Name) and n.id == alias and isinstance(n.ctx, ast.Load):
                attr = parents.get(id(n))
                call = parents.get(id(attr)) if attr is not None else None
                if not (
                    isinstance(attr, ast.Attribute)
                    and attr.attr in _LOG_LEVELS
                    and isinstance(call, ast.Call)
                    and call.func is attr
                ):
                    self.diagnostic(
                        module,
                        n,
                        "unsupported_loguru_usage",
                        "logger 仅支持直接 info/warning/error/debug 调用；动态、配置或异常调用未转换",
                    )
                    return False
        # Assignment/deletion would change which logger a later call refers to.
        for n in nodes:
            if (
                isinstance(n, ast.Name)
                and n.id == alias
                and isinstance(n.ctx, (ast.Store, ast.Del))
            ):
                self.diagnostic(module, n, "unsupported_loguru_usage", "logger 存在重新赋值或删除")
                return False
        return True

    def _absolute_from(self, module: _Module, node: ast.ImportFrom) -> str | None:
        if not node.level:
            return node.module or ""
        package = module.name if module.package else module.name.rpartition(".")[0]
        parts = package.split(".") if package else []
        if not parts or node.level > len(parts):
            self.diagnostic(
                module,
                node,
                "relative_import_outside_package",
                "相对导入超出本地包；入口按 runtime.load_strategy 的无包模块处理",
            )
            return None
        parent = ".".join(parts[: len(parts) - node.level + 1])
        return parent + ("." + node.module if node.module else "")

    def _bound_names(self, module: _Module) -> set[str]:
        if module.tree is None:
            return set()
        if module.bound_names is None:
            table = symtable.symtable(module.source, module.relative_path, "exec")
            module.bound_names = {
                s.get_name()
                for s in table.get_symbols()
                if s.is_assigned() or s.is_imported() or s.is_namespace()
            }
        return module.bound_names

    def _export_names(self, module: _Module, seen: set[str] | None = None) -> set[str]:
        if module.tree is None:
            return set()
        seen = set(seen or ())
        if module.name in seen:
            return set()
        seen.add(module.name)
        names = {name for name in self._bound_names(module) if not name.startswith("_")}
        for statement in module.tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = (
                    statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                )
                if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                    value = statement.value
                    if isinstance(value, (ast.List, ast.Tuple)) and all(
                        isinstance(x, ast.Constant) and isinstance(x.value, str) for x in value.elts
                    ):
                        return {
                            x.value
                            for x in value.elts
                            if isinstance(x, ast.Constant) and isinstance(x.value, str)
                        }
            if isinstance(statement, ast.ImportFrom) and any(
                a.name == "*" for a in statement.names
            ):
                name = self._absolute_from(module, statement)
                if name in self.modules:
                    names.update(self._export_names(self.modules[name], seen))
        return names

    def _star_children(self, name: str, module: _Module, node: ast.ImportFrom) -> None:
        child = self.modules.get(name)
        if child is None or child.tree is None:
            return
        bound = self._bound_names(child)
        for statement in child.tree.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
                continue
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                continue
            value = statement.value
            if not isinstance(value, (ast.List, ast.Tuple)) or not all(
                isinstance(x, ast.Constant) and isinstance(x.value, str) for x in value.elts
            ):
                self.diagnostic(
                    module,
                    node,
                    "dynamic_local_exports",
                    f"本地模块 {name} 的动态 __all__ 无法完整静态打包",
                )
                return
            for item in value.elts:
                if (
                    isinstance(item, ast.Constant)
                    and isinstance(item.value, str)
                    and item.value not in bound
                ):
                    self._add(name + "." + item.value, module, node)

    def _imports(self, module: _Module) -> None:
        if module.tree is None:
            return
        for node in _walk_live(module):
            if isinstance(node, ast.Import):
                replacements: list[str] = []
                changed = False
                for alias in node.names:
                    binding = alias.asname or alias.name.split(".")[0]
                    module.bindings[binding] = alias.name
                    if (
                        alias.name.split(".")[0] == "ptrade_sim"
                        and binding in module.trimmed_aliases
                    ):
                        changed = True
                    elif self._add(alias.name, module, node):
                        method = "load" if alias.asname else "import_name"
                        replacements.append(f"{binding} = {self.prefix}.{method}({alias.name!r})")
                        self.dependencies.append(
                            {
                                "name": alias.name,
                                "kind": "local",
                                "path": module.relative_path,
                                "line": getattr(node, "lineno", None),
                            }
                        )
                        changed = True
                    else:
                        self._dependency(alias.name, module, node)
                        replacements.append(ast.unparse(ast.Import(names=[alias])))
                if changed:
                    _patch(module, node, "; ".join(replacements) or "pass")
            elif isinstance(node, ast.ImportFrom):
                name = self._absolute_from(module, node)
                if name is None:
                    continue
                for alias in node.names:
                    module.bindings[alias.asname or alias.name] = name + "." + alias.name
                if name == "__future__":
                    import __future__

                    for alias in node.names:
                        feature = getattr(__future__, alias.name, None)
                        if (
                            feature is not None
                            and tuple(feature.getOptionalRelease()[:2]) > self.version
                        ):
                            self.diagnostic(
                                module,
                                node,
                                "future_python_version",
                                f"future 功能 {alias.name} 不支持 Python {self.target_python}",
                            )
                    continue
                if name.split(".")[0] == "ptrade_sim":
                    replacement = []
                    for alias in node.names:
                        binding = alias.asname or alias.name
                        if alias.name == "get_strategy_params" and name in {
                            "ptrade_sim",
                            "ptrade_sim.api",
                        }:
                            if binding != "get_strategy_params":
                                replacement.append(binding + " = get_strategy_params")
                            self.diagnostic(
                                module,
                                node,
                                "parameter_import_embedded",
                                "已用内嵌 JSON 参数接口替代模拟器参数导入",
                                "info",
                            )
                        elif binding not in module.trimmed_aliases:
                            self.diagnostic(
                                module,
                                node,
                                "simulator_dependency",
                                f"未支持的模拟器符号：{name}.{alias.name}",
                            )
                    _patch(module, node, "; ".join(replacement) or "pass")
                    continue
                if (
                    name == "loguru"
                    and all(a.name == "logger" for a in node.names)
                    and self._lookup(name, module, node) is None
                ):
                    replacements = []
                    for alias in node.names:
                        binding = alias.asname or alias.name
                        if self._logger_allowed(module, binding):
                            replacements.append(f"{binding} = {self.prefix}.logger")
                            self.diagnostic(
                                module,
                                node,
                                "loguru_logger_adapted",
                                "默认 logger 的四级直接日志调用已转为 str.format 后调用 PTrade log",
                                "info",
                            )
                    if len(replacements) == len(node.names):
                        _patch(module, node, "; ".join(replacements))
                        self.dependencies.append(
                            {
                                "name": name,
                                "kind": "adapted_logger",
                                "path": module.relative_path,
                                "line": getattr(node, "lineno", None),
                            }
                        )
                    continue
                if self._add(name, module, node):
                    replacements = []
                    for alias in node.names:
                        if alias.name == "*":
                            self._star_children(name, module, node)
                            module.star_imports.append((name, node))
                            replacements.append(f"{self.prefix}.star({name!r}, globals())")
                        else:
                            child = self.modules.get(name)
                            bound = self._bound_names(child) if child is not None else set()
                            if alias.name not in bound:
                                found_child = self._add(name + "." + alias.name, module, node)
                                if (
                                    not found_child
                                    and "__getattr__" not in bound
                                    and "*" not in bound
                                ):
                                    self.diagnostic(
                                        module,
                                        node,
                                        "missing_local_symbol",
                                        f"本地模块 {name} 没有可定位的 {alias.name} 符号或子模块",
                                    )
                            binding = alias.asname or alias.name
                            replacements.append(
                                f"{binding} = {self.prefix}.from_name({name!r}, {alias.name!r})"
                            )
                    _patch(module, node, "; ".join(replacements))
                    self.dependencies.append(
                        {
                            "name": name,
                            "kind": "local",
                            "path": module.relative_path,
                            "line": getattr(node, "lineno", None),
                        }
                    )
                else:
                    self._dependency(name, module, node)
                    if any(a.name == "*" for a in node.names):
                        self.diagnostic(
                            module,
                            node,
                            "external_wildcard_import",
                            "外部库的星号导入无法静态判定 PTrade API 是否被覆盖",
                        )

    def _inspect(self, module: _Module) -> None:
        if module.tree is None:
            return
        tree = ast.parse(
            module.transformed, filename=module.relative_path, feature_version=self.version
        )
        refs = _GlobalReferences(tree, module.transformed, module.relative_path).references
        unbound = {id(node): node for node in refs}
        builtin_names = set(vars(builtins))
        for reference in refs:
            if reference.id in module.star_exports:
                continue
            if reference.id in _API_GLOBALS:
                self.api_used.add(reference.id)
            elif (
                reference.id not in builtin_names | _DUNDER_GLOBALS
                and not reference.id.startswith(self.prefix)
            ):
                code = (
                    "unsupported_api"
                    if reference.id.startswith(
                        ("get_", "set_", "order", "run_", "cancel_", "query")
                    )
                    else "unbound_global"
                )
                self.diagnostic(
                    module,
                    reference,
                    code,
                    f"未绑定全局名称 {reference.id}，不属于本地官方 PTrade API 或内嵌参数接口",
                )
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in {"__file__", "__package__", "__loader__", "__spec__"}
            ):
                self.diagnostic(
                    module,
                    node,
                    "module_location_dependency",
                    "源码依赖原模块位置/加载元数据，单文件打包无法保留此语义",
                )
            if isinstance(node, ast.Call):
                dotted = _dotted(node.func)
                canonical = dotted
                if dotted:
                    first, _, rest = dotted.partition(".")
                    if first in module.bindings:
                        canonical = module.bindings[first] + ("." + rest if rest else "")
                if canonical in {"__import__", "builtins.__import__", "importlib.import_module"}:
                    literal = (
                        node.args[0].value
                        if node.args and isinstance(node.args[0], ast.Constant)
                        else None
                    )
                    if (
                        isinstance(literal, str)
                        and not literal.startswith(".")
                        and not self._lookup(literal, module, node)
                    ):
                        self._dependency(literal, module, node)
                    else:
                        self.diagnostic(
                            module,
                            node,
                            "dynamic_local_import",
                            "动态或本地模块导入未静态打包；使用显式 import/from 导入",
                        )
                elif canonical in {"exec", "eval", "builtins.exec", "builtins.eval"}:
                    self.diagnostic(
                        module, node, "dynamic_code", "动态执行代码使依赖与 API 无法完整静态检查"
                    )
                elif (
                    canonical
                    and canonical.startswith("importlib.")
                    and canonical.rsplit(".", 1)[-1]
                    in {"spec_from_file_location", "module_from_spec", "SourceFileLoader", "reload"}
                ):
                    self.diagnostic(
                        module,
                        node,
                        "dynamic_local_import",
                        "动态文件加载/重载不支持单文件静态打包",
                    )
                elif (
                    canonical
                    and canonical.startswith("sys.path.")
                    and canonical.rsplit(".", 1)[-1]
                    in {"append", "insert", "extend", "remove", "pop", "clear"}
                ):
                    self.diagnostic(
                        module,
                        node,
                        "import_path_mutation",
                        "策略修改 sys.path，依赖范围无法限定到策略目录",
                    )
                if (
                    isinstance(node.func, ast.Name)
                    and id(node.func) in unbound
                    and node.func.id in _API_GLOBALS
                ):
                    api = node.func.id
                    self._check_api_call(module, node, api)
                    for keyword in node.keywords:
                        if keyword.arg in _DEPRECATED_KEYWORDS.get(api, set()) or (
                            keyword.arg is not None
                            and api in _API_KEYWORDS
                            and keyword.arg not in _API_KEYWORDS[api]
                        ):
                            self.diagnostic(
                                module,
                                keyword,
                                "unsupported_api_keyword",
                                f"本地官方当前签名不支持 {api} 的 {keyword.arg} 参数",
                            )
        # Reserved top-level names must not replace the injected PTrade objects or
        # the embedded parameter interface. Function arguments remain untouched.
        for statement in tree.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = (
                    statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                )
                for target in targets:
                    for n in ast.walk(target):
                        if isinstance(n, ast.Name) and n.id in {"g", "log", "get_strategy_params"}:
                            self.diagnostic(
                                module,
                                n,
                                "api_global_shadowing",
                                f"顶层赋值覆盖保留 API 名称 {n.id}",
                            )
            if (
                module is self.entry
                and isinstance(statement, (ast.FunctionDef, ast.ClassDef))
                and statement.name == "get_strategy_params"
            ):
                self.diagnostic(
                    module,
                    statement,
                    "parameter_bridge_conflict",
                    "自定义 get_strategy_params 与冻结参数接口冲突",
                )

    def _check_api_call(self, module: _Module, node: ast.Call, api: str) -> None:
        if (
            api in _API_KEYWORDS
            and not any(isinstance(a, ast.Starred) for a in node.args)
            and len(node.args) > len(_API_KEYWORDS[api])
        ):
            self.diagnostic(
                module, node, "unsupported_api_signature", f"{api} 的位置参数超过本地官方签名"
            )
        if api != "get_history" or any(isinstance(a, ast.Starred) for a in node.args):
            return
        keywords = {k.arg: k.value for k in node.keywords if k.arg is not None}
        field_value = node.args[2] if len(node.args) > 2 else keywords.get("field")
        securities = node.args[3] if len(node.args) > 3 else keywords.get("security_list")
        fields = {
            "open",
            "high",
            "low",
            "close",
            "volume",
            "money",
            "price",
            "is_open",
            "preclose",
            "high_limit",
            "low_limit",
            "unlimited",
        }

        def strings(value: ast.AST | None) -> list[str] | None:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return [value.value]
            if isinstance(value, (ast.List, ast.Tuple)) and all(
                isinstance(x, ast.Constant) and isinstance(x.value, str) for x in value.elts
            ):
                return [
                    x.value
                    for x in value.elts
                    if isinstance(x, ast.Constant) and isinstance(x.value, str)
                ]
            return None

        field_strings, security_strings = strings(field_value), strings(securities)
        wrong_fields = (
            field_strings is not None
            and bool(field_strings)
            and all(re.fullmatch(r"\d{6}\.(?:SS|SZ)", value) for value in field_strings)
        )
        wrong_securities = (
            security_strings is not None
            and bool(security_strings)
            and all(value in fields for value in security_strings)
        )
        if wrong_fields or wrong_securities:
            self.diagnostic(
                module,
                node,
                "history_argument_order",
                "get_history 要求 field 第三、security_list 第四；模拟器也不支持证券优先重载，未改动交易调用",
            )

    def _rewrite_api_attributes(self, module: _Module) -> None:
        if module.tree is None:
            return
        refs = _GlobalReferences(module.tree, module.source, module.relative_path).references
        unbound = {id(n) for n in refs}
        callbacks = {
            "initialize",
            "handle_data",
            "before_trading_start",
            "after_trading_end",
            "tick_data",
            "on_order_response",
            "on_trade_response",
        }
        for call in _walk_live(module):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and id(call.func) in unbound
                and call.func.id in {"run_daily", "run_interval"}
                and len(call.args) > 1
                and isinstance(call.args[1], ast.Name)
            ):
                callbacks.add(call.args[1].id)
        top_functions = {id(n) for n in module.tree.body if isinstance(n, ast.FunctionDef)}
        classes = {n.name for n in _walk_live(module) if isinstance(n, ast.ClassDef)}
        for function in (n for n in _walk_live(module) if isinstance(n, ast.FunctionDef)):
            args = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
            context_name = (
                args[0].arg
                if args and id(function) in top_functions and function.name in callbacks
                else None
            )
            facts = _AttributeFacts(function, context_name, classes, unbound, module.bindings)
            for node, kinds in facts.aliases.values():
                expected, replacement = {
                    "total_value": ("portfolio", "portfolio_value"),
                    "closeable_amount": ("position", "enable_amount"),
                }[node.attr]
                if kinds == {expected} and isinstance(node.ctx, ast.Load):
                    _, end = _span(module.source, node, module.source_lines, module.source_starts)
                    module.patches.append((end - len(node.attr), end, replacement))
                    self.diagnostic(
                        module,
                        node,
                        "api_attribute_mapped",
                        f"已验证模拟属性 {node.attr} → 官方属性 {replacement}",
                        "info",
                    )
                    self.diagnostics[-1].update({"from": node.attr, "to": replacement})
                elif kinds <= {"table", "custom"}:
                    continue
                else:
                    self.diagnostic(
                        module,
                        node,
                        "ambiguous_api_attribute",
                        f"{node.attr} 的 receiver 类型无法唯一证明为 {expected}；未替换",
                    )

    def _callback(self, name: str, module: _Module, seen: set[tuple[str, str]]):
        identity = module.name, name
        if identity in seen or module.tree is None:
            return None
        seen.add(identity)
        for statement in reversed(module.tree.body):
            if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Delete)):
                targets = (
                    statement.targets
                    if isinstance(statement, (ast.Assign, ast.Delete))
                    else [statement.target]
                )
                if any(
                    isinstance(n, ast.Name) and n.id == name
                    for target in targets
                    for n in ast.walk(target)
                ):
                    return None
            if (
                isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                and statement.name == name
            ):
                return statement, module
            if isinstance(statement, ast.ImportFrom) and not statement.level:
                for alias in statement.names:
                    if statement.module in self.modules:
                        imported = self.modules[statement.module]
                        if (alias.asname or alias.name) == name:
                            return self._callback(alias.name, imported, seen)
                        if alias.name == "*" and name in self._export_names(imported):
                            return self._callback(name, imported, seen)
        return None

    def _check_callbacks(self) -> None:
        if self.entry.tree is None:
            return
        for name, arity in _CALLBACKS.items():
            found = self._callback(name, self.entry, set())
            if found is None:
                self.diagnostic(
                    self.entry, None, "missing_callback", f"缺少可静态定位的官方必选函数 {name}"
                )
                continue
            node, module = found
            args = node.args
            positional = len(args.posonlyargs) + len(args.args)
            required = positional - len(args.defaults)
            required_kwonly = any(d is None for d in args.kw_defaults)
            if (
                isinstance(node, ast.AsyncFunctionDef)
                or required > arity
                or (args.vararg is None and positional < arity)
                or required_kwonly
            ):
                self.diagnostic(
                    module,
                    node,
                    "callback_signature",
                    f"{name} 无法按官方的 {arity} 个位置参数调用",
                )

    def convert(self) -> ExportResult:
        self._strip_bootstrap(self.entry)
        self._imports(self.entry)
        index = 0
        while index < len(self.pending):
            module = self.pending[index]
            index += 1
            self._strip_bootstrap(module)
            self._imports(module)
        # A generated binding must not collide with any original identifier,
        # including attribute/string-based references. Re-render if necessary.
        originals = [self.entry, *self.modules.values()]
        if any(self.prefix in m.source for m in originals):
            raise StrategyError("策略源码与内容寻址的内部命名空间发生冲突")
        for module in originals:
            for imported, _node in module.star_imports:
                child = self.modules.get(imported)
                if child is not None:
                    module.star_exports.update(self._export_names(child))
            self._rewrite_api_attributes(module)
            module.transformed = _apply_patches(module)
            self._inspect(module)
        self._check_callbacks()
        ready = not any(d["severity"] == "error" for d in self.diagnostics)
        source = ""
        if ready:
            table = {
                name: {
                    "source": module.transformed,
                    "path": module.relative_path,
                    "package": module.package,
                }
                for name, module in sorted(self.modules.items())
            }
            prelude = _runtime(self.prefix, self.params, table)
            source = _insert_prelude(self.entry.transformed, prelude)
            try:
                export_tree = ast.parse(
                    source, filename=self.filename, feature_version=self.version
                )
                compile(export_tree, self.filename, "exec", dont_inherit=True)
            except (SyntaxError, ValueError) as exc:
                self.diagnostic(
                    self.entry,
                    None,
                    "export_syntax",
                    f"导出文件语法检查失败：{exc}",
                    line=getattr(exc, "lineno", None),
                    column=getattr(exc, "offset", None),
                )
                source, ready = "", False
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        diagnostics = sorted(
            {_json(d): d for d in self.diagnostics}.values(),
            key=lambda d: (d["path"], d["line"] or 0, d["column"] or 0, d["code"], d["severity"]),
        )
        dependencies = sorted(
            {_json(d): d for d in self.dependencies}.values(),
            key=lambda d: (d["path"], d["line"], d["name"]),
        )
        sources = [
            {
                "path": str(m.path),
                "relative_path": m.relative_path,
                "sha256": m.sha256,
                "bytes": m.size,
            }
            for m in sorted(originals, key=lambda m: m.relative_path)
            if m.path is not None
        ]
        report = {
            "format_version": 1,
            "converter": "ptrade-local-backtest.static-export",
            "ready": ready,
            "filename": self.filename,
            "params": self.params,
            "params_mode": self.params_mode,
            "source_sha256": digest,
            "export_sha256": digest,
            "sources": sources,
            "entry": str(self.entry_path),
            "root": str(self.root),
            "name": self.bundle.name,
            "target_python": self.target_python,
            "syntax_checked": ready,
            "validation": "static_only",
            "api_reference": "docs/ptrade_api.md",
            "api_used": sorted(self.api_used),
            "diagnostics": diagnostics,
            "transformations": [d for d in diagnostics if d["severity"] == "info"],
            "dependencies": dependencies,
            "modules": sorted(self.modules),
            "protected_paths": sorted(str(p) for p in self.protected),
        }
        return ExportResult(source, self.filename, report, tuple(sorted(self.protected)))


def convert_strategy(
    target: str | Path,
    params: dict[str, Any] | None = None,
    *,
    target_python: str = "3.11",
    params_mode: str = "merge",
) -> ExportResult:
    """Static export; replace embeds exact final params, merge recursively overrides config."""
    return _Converter(target, params, target_python, params_mode).convert()


def _insert_prelude(source: str, prelude: str) -> str:
    tree = ast.parse(source)
    prefix: list[ast.AST] = []
    for index, statement in enumerate(tree.body):
        if (
            index == 0
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ) or (isinstance(statement, ast.ImportFrom) and statement.module == "__future__"):
            prefix.append(statement)
        else:
            break
    if prefix:
        _, end = _span(source, prefix[-1])
        rest_of_line = source[end:].split("\n", 1)[0]
        if not rest_of_line.strip() or rest_of_line.lstrip().startswith("#"):
            newline = source.find("\n", end)
            offset = len(source) if newline < 0 else newline + 1
            before, after = source[:offset], source[offset:]
        else:
            before = source[:end] + "\n"
            after = source[end:].lstrip().removeprefix(";").lstrip(" \t")
    else:
        offset = _span(source, tree.body[0])[0] if tree.body else len(source)
        before, after = source[:offset], source[offset:]
    return (
        "# -*- coding: utf-8 -*-\n"
        + before.rstrip("\n")
        + "\n\n"
        + prelude
        + "\n"
        + after.rstrip("\n")
        + "\n"
    )


# Substitution is confined to this trusted generated template, never user source.
# Runtime syntax is intentionally Python 3.5-compatible. The strategy's requested
# target grammar is checked separately before producing a ready result.
_RUNTIME = r"""
# PTrade single-file export: embedded params and private lazy helper modules.
import builtins as $P$_builtins
import json as $P$_json
import sys as $P$_sys
import types as $P$_types

$P$_params = $P$_json.loads($PARAMS$)


def get_strategy_params(key=None, default=None):
    if key is None:
        return dict($P$_params)
    return $P$_params.get(key, default)


class $P$_LogBridge:
    def __init__(self, namespace):
        self.namespace = namespace

    def _write(self, level, message, args, kwargs):
        message = str(message)
        if args or kwargs:
            message = message.format(*args, **kwargs)
        getattr(self.namespace["log"], level)(message)

    def info(self, message, *args, **kwargs):
        self._write("info", message, args, kwargs)

    def warning(self, message, *args, **kwargs):
        self._write("warning", message, args, kwargs)

    def error(self, message, *args, **kwargs):
        self._write("error", message, args, kwargs)

    def debug(self, message, *args, **kwargs):
        self._write("debug", message, args, kwargs)


class $P$_APIBuiltins(dict):
    api_names = frozenset($APIS$)

    def __init__(self, namespace):
        dict.__init__(self, vars($P$_builtins))
        self.namespace = namespace

    def __missing__(self, name):
        if name in self.api_names:
            return self.namespace[name]
        raise KeyError(name)


class $P$_Loader:
    def __init__(self, sources, namespace):
        self.sources = sources
        self.namespace = namespace
        self.cache = {}
        self.root_name = "$P$_modules_" + str(id(self))
        self.root = $P$_types.ModuleType(self.root_name)
        self.root.__path__ = []
        self.logger = $P$_LogBridge(namespace)

    def load(self, name):
        if name in self.cache:
            return self.cache[name]
        if name not in self.sources:
            raise ImportError("unbundled local module: " + name)
        parent_name, _, short_name = name.rpartition(".")
        parent = self.load(parent_name) if parent_name else self.root
        # A package initializer may already have imported this child.
        if name in self.cache:
            return self.cache[name]
        $P$_sys.modules[self.root_name] = self.root
        item = self.sources[name]
        full_name = self.root_name + "." + name
        module = $P$_types.ModuleType(full_name)
        module.__file__ = "<ptrade-export>/" + item["path"]
        module.__package__ = full_name if item["package"] else full_name.rpartition(".")[0]
        module.__loader__ = self
        if item["package"]:
            module.__path__ = []
        # API names resolve against the current entry globals, including APIs
        # injected after top-level module imports. Ordinary builtin names and
        # helper globals keep normal Python lookup and star-import semantics.
        module.__dict__["__builtins__"] = $P$_APIBuiltins(self.namespace)
        module.__dict__["$P$"] = self
        # Cache before evaluation to retain ordinary circular-import behavior.
        self.cache[name] = module
        $P$_sys.modules[full_name] = module
        try:
            code = compile(item["source"], module.__file__, "exec", dont_inherit=True)
            exec(code, module.__dict__)
        except BaseException:
            self.cache.pop(name, None)
            if $P$_sys.modules.get(full_name) is module:
                del $P$_sys.modules[full_name]
            raise
        setattr(parent, short_name, module)
        return module

    def import_name(self, name):
        self.load(name)
        return self.cache[name.split(".")[0]]

    def from_name(self, name, attribute):
        module = self.load(name)
        try:
            return getattr(module, attribute)
        except AttributeError:
            child = name + "." + attribute
            if child in self.sources:
                return self.load(child)
            raise ImportError("cannot import name %r from %r" % (attribute, name))

    def star(self, name, namespace):
        module = self.load(name)
        names = getattr(module, "__all__", None)
        if names is None:
            names = [key for key in vars(module) if not key.startswith("_")]
        for key in names:
            if not isinstance(key, str):
                raise TypeError("local module __all__ entries must be strings")
            namespace[key] = self.from_name(name, key)


$P$ = $P$_Loader($P$_json.loads($MODULES$), globals())
"""


def _runtime(prefix: str, params: dict[str, Any], modules: dict[str, Any]) -> str:
    values = {
        "P": prefix,
        "PARAMS": repr(_json(params)),
        "MODULES": repr(_json(modules)),
        "APIS": repr(tuple(sorted(_API_GLOBALS))),
    }
    # A single pass never interprets marker text inside user strings as templates.
    return re.sub(r"\$(P|PARAMS|MODULES|APIS)\$", lambda match: values[match.group(1)], _RUNTIME)


def _is_same(path: Path, source: Path) -> bool:
    if path.resolve() == source.resolve():
        return True
    try:
        return path.exists() and source.exists() and path.samefile(source)
    except OSError:
        return False


def _publish(path: Path, content: str, overwrite: bool) -> None:
    """Stage UTF-8/LF bytes; exclusive hard-link publish prevents TOCTOU clobber."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=".ptrade-export-",
            delete=False,
        ) as fh:
            temporary = Path(fh.name)
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        if overwrite:
            temporary.replace(path)
        else:
            # Same-directory files stay on the same filesystem. Unlike rename on
            # POSIX, link refuses an existing destination atomically.
            os.link(temporary, path)
    except FileExistsError as exc:
        raise StrategyConfigError(
            f"导出文件已存在（使用 overwrite=True 显式覆盖）：{path}"
        ) from exc
    except OSError as exc:
        raise StrategyError(f"导出写入失败：{path}：{exc}") from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def write_export(
    result: ExportResult, output: str | Path, overwrite: bool = False
) -> dict[str, Path]:
    """Write a .py and same-basename .manifest.json; a directory uses result.filename.

    Original entry/helper/config paths and their symlink/hardlink aliases are
    protected even with overwrite=True. A blocked result writes only its report
    and raises StrategyError. No strategy code is executed by this function.
    """
    filename = result.filename
    if (
        Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or not filename.lower().endswith(".py")
    ):
        raise StrategyConfigError("ExportResult.filename 必须是 .py 基础文件名")
    output_path = Path(output).expanduser()
    if output_path.is_dir() or not output_path.suffix:
        output_path /= filename
    elif output_path.suffix.lower() != ".py":
        raise StrategyConfigError("导出目标必须为目录或 .py 文件")
    source_path = output_path.absolute()
    report_path = source_path.with_suffix(".manifest.json")
    protected = set(result._protected_paths)
    protected.update(Path(p) for p in result.report.get("protected_paths", []))
    for source in result.report.get("sources", []):
        if source.get("path"):
            protected.add(Path(source["path"]))
    for destination in (source_path, report_path):
        if any(_is_same(destination, original) for original in protected):
            raise StrategyConfigError(f"导出目标会覆盖原策略源码/配置：{destination}")
        if destination.exists() and not overwrite:
            raise StrategyConfigError(
                f"导出文件已存在（使用 overwrite=True 显式覆盖）：{destination}"
            )
        if destination.exists() and not destination.is_file():
            raise StrategyConfigError(f"导出目标不是普通文件：{destination}")
    digest = hashlib.sha256(result.source.encode("utf-8")).hexdigest()
    if (
        result.report.get("export_sha256") != digest
        or result.report.get("filename") != result.filename
    ):
        raise StrategyError("ExportResult 源码摘要或文件名与报告不一致")
    try:
        report_text = (
            json.dumps(result.report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            + "\n"
        )
    except (TypeError, ValueError) as exc:
        raise StrategyError(f"导出报告不是有效 JSON：{exc}") from exc
    try:
        source_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StrategyError(f"导出目录创建失败：{source_path.parent}：{exc}") from exc
    # Publish the manifest first: failure cannot leave a fresh deployable file
    # without its compatibility report. No-overwrite is also checked at publish.
    _publish(report_path, report_text, overwrite)
    if result.report.get("ready") is not True:
        raise StrategyError(f"策略尚未满足生产导出条件；已写入诊断报告：{report_path}")
    if not result.source:
        raise StrategyError("ready 策略的导出源码为空")
    _publish(source_path, result.source, overwrite)
    return {"source": source_path, "report": report_path}
