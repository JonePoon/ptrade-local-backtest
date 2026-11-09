"""Static export contracts; only test-generated fixtures are executed for import semantics."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from ptrade_sim import strategy_export as E
from ptrade_sim.exceptions import (
    StrategyConfigError,
    StrategyError,
    StrategyImportError,
    StrategyPathError,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _private_modules_only():
    before = set(sys.modules)
    yield
    for key in set(sys.modules) - before:
        if key.startswith("_pt_export_"):
            sys.modules.pop(key, None)


def _bundle(tmp_path, source=None, files=None, params=None, name="case"):
    root = tmp_path / name
    root.mkdir()
    if source is None:
        source = "def initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n"
    (root / "strategy.py").write_text(source, encoding="utf-8")
    for filename, content in (files or {}).items():
        path = root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    if params is not None:
        (root / "strategy_config.json").write_text(json.dumps({"params": params}), encoding="utf-8")
    return root


def _execute(result, namespace=None):
    # This harness runs only source generated in this file. Conversion of any
    # user strategy is independently verified not to execute it below.
    assert result.report["ready"], result.report["diagnostics"]
    namespace = {"__name__": "export_fixture", **(namespace or {})}
    exec(compile(result.source, result.filename, "exec"), namespace)
    return namespace


def _errors(result):
    assert result.report["ready"] is False
    assert result.source == "", "被阻止的转换不得保留貌似可部署的源码"
    return {d["code"] for d in result.report["diagnostics"] if d["severity"] == "error"}


def test_default_result_is_json_ready_and_hashed(tmp_path):
    root = _bundle(tmp_path)
    result = E.convert_strategy(root)
    assert result.filename == "case_ptrade.py"
    assert result.report["filename"] == result.filename
    assert result.report["ready"] is True
    assert result.report["validation"] == "static_only"
    assert result.report["target_python"] == "3.11"
    assert result.report["syntax_checked"] is True
    digest = hashlib.sha256(result.source.encode("utf-8")).hexdigest()
    assert result.report["export_sha256"] == result.report["source_sha256"] == digest
    assert result.source.startswith("# -*- coding: utf-8 -*-\n")
    json.dumps(result.report, allow_nan=False)


def test_input_shapes_follow_resolve_strategy(tmp_path):
    root = _bundle(tmp_path)
    file_result = E.convert_strategy(root / "strategy.py")
    directory_result = E.convert_strategy(root)
    assert file_result.filename == "strategy_ptrade.py"
    assert directory_result.filename == "case_ptrade.py"
    assert file_result.source == directory_result.source
    (root / "strategy.py").rename(root / "named.py")
    assert E.convert_strategy(root).report["ready"]
    (root / "other.py").write_text("x = 1", encoding="utf-8")
    with pytest.raises(StrategyConfigError):
        E.convert_strategy(root)


def test_missing_and_non_python_targets_use_project_errors(tmp_path):
    with pytest.raises(StrategyPathError):
        E.convert_strategy(tmp_path / "not-there.py")
    bad = tmp_path / "text.txt"
    bad.write_text("x", encoding="utf-8")
    with pytest.raises(StrategyConfigError):
        E.convert_strategy(bad)


def test_conversion_never_executes_entry_or_helper(tmp_path, monkeypatch):
    root = _bundle(
        tmp_path,
        "import helper\nraise RuntimeError('must not execute entry')\n"
        "def initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n",
        {"helper.py": "raise RuntimeError('must not execute helper')\n"},
    )

    def forbidden_exec(*args, **kwargs):
        pytest.fail("转换器调用了 exec")

    monkeypatch.setattr("builtins.exec", forbidden_exec)
    result = E.convert_strategy(root)
    assert result.report["ready"] is True
    assert "helper" not in sys.modules
    assert "must not execute entry" in result.source


def test_params_are_json_embedded_deep_merged_and_detached(tmp_path):
    root = _bundle(tmp_path, params={"nested": {"a": 1, "b": 2}, "enabled": True, "empty": None})
    override = {"nested": {"b": 3}, "text": "中文 ' \" \\ \n\u2028", "values": [True, None, 2.5]}
    result = E.convert_strategy(root, params=override)
    assert result.report["params"] == {
        "nested": {"a": 1, "b": 3},
        "enabled": True,
        "empty": None,
        "text": "中文 ' \" \\ \n\u2028",
        "values": [True, None, 2.5],
    }
    namespace = _execute(result)
    getter = namespace["get_strategy_params"]
    assert getter() == result.report["params"]
    assert getter("enabled") is True
    assert getter("empty", "fallback") is None
    assert getter("missing", 17) == 17
    assert getter(key="missing", default=18) == 18
    copy = getter()
    copy["enabled"] = False
    assert getter("enabled") is True, "全量读取必须返回顶层副本"
    copy["nested"]["a"] = 10
    assert getter("nested")["a"] == 10, "按现有 API 维持浅副本而非偷偷改成深副本"
    override["nested"]["b"] = 99
    assert result.report["params"]["nested"]["b"] == 3


@pytest.mark.parametrize(
    "params",
    [[1], {1: "bad"}, {"x": float("nan")}, {"x": float("inf")}, {"x": (1, 2)}, {"x": object()}],
)
def test_invalid_params_use_config_error(tmp_path, params):
    with pytest.raises(StrategyConfigError):
        E.convert_strategy(_bundle(tmp_path), params=params)


def test_cyclic_params_use_config_error(tmp_path):
    params = {}
    params["cycle"] = params
    with pytest.raises(StrategyConfigError):
        E.convert_strategy(_bundle(tmp_path), params=params)


@pytest.mark.parametrize("config", ["{broken", "[]", '{"params": [1]}'])
def test_damaged_strategy_config_is_not_silently_ignored(tmp_path, config):
    root = _bundle(tmp_path)
    (root / "strategy_config.json").write_text(config, encoding="utf-8")
    with pytest.raises(StrategyConfigError):
        E.convert_strategy(root)


def test_parameters_import_is_removed_without_simulator_dependency(tmp_path):
    root = _bundle(
        tmp_path,
        "from ptrade_sim.api import get_strategy_params as params\n"
        "def initialize(context):\n    context.value = params('n', 2)\n"
        "def handle_data(context, data):\n    pass\n",
        params={"n": 7},
    )
    result = E.convert_strategy(root)
    namespace = _execute(result)
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.value == 7
    assert not any(d["severity"] == "error" for d in result.report["diagnostics"])


@pytest.mark.parametrize(
    "source",
    [
        "def initialize(:\n    pass\n",
        "return 1\n",
        "def initialize(context):\n    from helper import *\n",
    ],
)
def test_python_syntax_and_compiler_errors_are_located_reports(tmp_path, source):
    result = E.convert_strategy(_bundle(tmp_path, source))
    assert "source_syntax" in _errors(result)
    diag = next(d for d in result.report["diagnostics"] if d["code"] == "source_syntax")
    assert diag["path"] == "strategy.py"
    assert isinstance(diag["line"], int) and diag["line"] >= 1
    assert isinstance(diag["message"], str) and diag["message"]


def test_invalid_source_encoding_is_blocked_not_replaced(tmp_path):
    root = _bundle(tmp_path)
    raw = b"# coding: utf-8\n\xff\n"
    (root / "strategy.py").write_bytes(raw)
    result = E.convert_strategy(root)
    assert "source_encoding" in _errors(result)
    assert result.report["sources"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert (root / "strategy.py").read_bytes() == raw


def test_pep263_encoding_is_decoded_and_reexported_utf8(tmp_path):
    root = _bundle(tmp_path)
    source = "# coding: latin-1\nTEXT = 'café'\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n"
    raw = source.encode("latin-1")
    (root / "strategy.py").write_bytes(raw)
    result = E.convert_strategy(root)
    assert _execute(result)["TEXT"] == "café"
    assert result.report["sources"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert (root / "strategy.py").read_bytes() == raw


def test_deterministic_and_no_source_overwrites(tmp_path):
    root = _bundle(
        tmp_path, files={"unused.py": "raise RuntimeError('unused')\n"}, params={"b": 2, "a": 1}
    )
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    first = E.convert_strategy(root, params={"z": None, "x": 3})
    second = E.convert_strategy(root, params={"x": 3, "z": None})
    assert first.source == second.source
    assert first.report == second.report
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}
    assert first.report["modules"] == []


def test_local_helper_state_is_isolated_and_alias_imports_work(tmp_path, monkeypatch):
    sentinel = ModuleType("helper")
    sentinel.VALUE = 900
    monkeypatch.setitem(sys.modules, "helper", sentinel)
    root = _bundle(
        tmp_path,
        "import helper as h\nfrom helper import score as sc\nVALUE = 99\n"
        "def initialize(context):\n    context.values = (h.score(), sc(), VALUE)\n"
        "def handle_data(context, data):\n    pass\n",
        {"helper.py": "VALUE = 3\ndef score():\n    return VALUE\n"},
    )
    result = E.convert_strategy(root)
    namespace = _execute(result)
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.values == (3, 3, 99), "平铺 helper 全局变量会改变交易计算"
    namespace["h"].VALUE = 8
    assert namespace["sc"]() == 8
    assert namespace["VALUE"] == 99
    assert sys.modules["helper"] is sentinel


def test_two_exports_have_independent_local_module_caches(tmp_path):
    root = _bundle(
        tmp_path,
        "import helper\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n",
        {"helper.py": "VALUE = []\n"},
    )
    result = E.convert_strategy(root)
    a, b = _execute(result), _execute(result)
    a["helper"].VALUE.append(1)
    assert b["helper"].VALUE == [], "相同源码在两个运行实例中不得共享 helper 状态"
    assert a["helper"].__name__ != b["helper"].__name__


def test_nested_package_and_relative_helper_imports(tmp_path):
    root = _bundle(
        tmp_path,
        "import package.child\nimport package.child as alias\nfrom package import child\n"
        "def initialize(context):\n    context.value = (package.child.value(), alias.value(), child.value())\n"
        "def handle_data(context, data):\n    pass\n",
        {
            "package/__init__.py": "from . import child\n",
            "package/child.py": "from .other import VALUE\ndef value():\n    return VALUE\n",
            "package/other.py": "VALUE = 42\n",
        },
    )
    result = E.convert_strategy(root)
    namespace = _execute(result)
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.value == (42, 42, 42)
    assert namespace["alias"] is namespace["child"] is namespace["package"].child
    assert result.report["modules"] == ["package", "package.child", "package.other"]


def test_package_initializer_does_not_execute_child_twice(tmp_path):
    root = _bundle(
        tmp_path,
        "import package.child\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n",
        {
            "package/__init__.py": "from . import child\n",
            "package/child.py": "from collections import Counter\nVALUE = Counter()\nVALUE['loads'] += 1\n",
        },
    )
    namespace = _execute(E.convert_strategy(root))
    assert namespace["package"].child.VALUE == {"loads": 1}
    assert namespace["package"].child is next(
        v for k, v in sys.modules.items() if k == namespace["package"].child.__name__
    )


def test_namespace_packages_are_bundled(tmp_path):
    root = _bundle(
        tmp_path,
        "from namespace.nested import helper\ndef initialize(context):\n    context.value = helper.VALUE\n"
        "def handle_data(context, data):\n    pass\n",
        {"namespace/nested/helper.py": "VALUE = 12\n"},
    )
    namespace = _execute(E.convert_strategy(root))
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.value == 12


def test_package_wins_over_same_named_file(tmp_path):
    root = _bundle(
        tmp_path,
        "import helper\ndef initialize(context):\n    context.value = helper.VALUE\n"
        "def handle_data(context, data):\n    pass\n",
        {"helper.py": "VALUE = 'file'\n", "helper/__init__.py": "VALUE = 'package'\n"},
    )
    namespace = _execute(E.convert_strategy(root))
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.value == "package"


def test_from_import_preserves_existing_package_attribute(tmp_path):
    root = _bundle(
        tmp_path,
        "from package import value\ndef initialize(context):\n    context.value = value\n"
        "def handle_data(context, data):\n    pass\n",
        {"package/__init__.py": "value = 15\n", "package/value.py": "value = 99\n"},
    )
    namespace = _execute(E.convert_strategy(root))
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.value == 15


def test_local_star_import_respects_all_and_hides_internal_names(tmp_path):
    root = _bundle(
        tmp_path,
        "from helper import *\ndef initialize(context):\n    context.value = VALUE\n"
        "def handle_data(context, data):\n    pass\n",
        {"helper.py": "__all__ = ['VALUE']\nVALUE = 4\nEXTRA = 99\n"},
    )
    namespace = _execute(E.convert_strategy(root))
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.value == 4
    assert "EXTRA" not in namespace
    assert "logger" not in namespace


def test_package_star_all_loads_declared_child(tmp_path):
    root = _bundle(
        tmp_path,
        "from package import *\ndef initialize(context):\n    context.value = child.VALUE\n"
        "def handle_data(context, data):\n    pass\n",
        {"package/__init__.py": "__all__ = ['child']\n", "package/child.py": "VALUE = 9\n"},
    )
    namespace = _execute(E.convert_strategy(root))
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.value == 9


def test_dynamic_local_all_is_blocked(tmp_path):
    root = _bundle(
        tmp_path,
        "from helper import *\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n",
        {"helper.py": "__all__ = ['V' + 'ALUE']\nVALUE = 1\n"},
    )
    assert "dynamic_local_exports" in _errors(E.convert_strategy(root))


def test_circular_plain_imports_keep_partial_module_identity(tmp_path):
    root = _bundle(
        tmp_path,
        "import a\ndef initialize(context):\n    context.value = a.value()\n"
        "def handle_data(context, data):\n    pass\n",
        {"a.py": "import b\nVALUE = 7\ndef value():\n    return b.a.VALUE\n", "b.py": "import a\n"},
    )
    namespace = _execute(E.convert_strategy(root))
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.value == 7
    assert namespace["a"].b.a is namespace["a"]


def test_lazy_function_import_is_not_eagerly_evaluated(tmp_path):
    root = _bundle(
        tmp_path,
        "def initialize(context):\n    pass\ndef handle_data(context, data):\n    import helper\n",
        {"helper.py": "raise RuntimeError('lazy')\n"},
    )
    namespace = _execute(E.convert_strategy(root))
    namespace["initialize"](SimpleNamespace())
    with pytest.raises(RuntimeError, match="lazy"):
        namespace["handle_data"](None, None)


def test_dataclass_helper_has_private_sys_modules_identity(tmp_path):
    root = _bundle(
        tmp_path,
        "from helper import Thing\ndef initialize(context):\n    context.thing = Thing(5)\n"
        "def handle_data(context, data):\n    pass\n",
        {
            "helper.py": "from __future__ import annotations\nfrom dataclasses import dataclass\n"
            "@dataclass\nclass Thing:\n    value: int\n"
        },
    )
    namespace = _execute(E.convert_strategy(root))
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.thing.value == 5


def test_helper_can_read_embedded_params_and_preinjected_api(tmp_path):
    root = _bundle(
        tmp_path,
        "import helper\ndef initialize(context):\n    context.value = helper.value()\n"
        "def handle_data(context, data):\n    pass\n",
        {"helper.py": "def value():\n    return g.multiplier * get_strategy_params('n', 1)\n"},
        params={"n": 3},
    )
    namespace = _execute(E.convert_strategy(root), {"g": SimpleNamespace(multiplier=4)})
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.value == 12


def test_no_arbitrary_literal_replacement_and_unicode_import_spans(tmp_path):
    source = "标题 = 'import helper; BacktestEngine' ; import helper as h\n"
    source += "# BacktestEngine is an ordinary comment\ndef initialize(context):\n    context.value = h.VALUE\n"
    source += "def handle_data(context, data):\n    pass\n"
    root = _bundle(tmp_path, source, {"helper.py": "VALUE = 11\n"})
    result = E.convert_strategy(root)
    namespace = _execute(result)
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.value == 11
    assert namespace["标题"] == "import helper; BacktestEngine"
    assert "# BacktestEngine is an ordinary comment" in result.source


def test_docstring_future_and_same_line_header_preserved(tmp_path):
    root = _bundle(
        tmp_path,
        '"""策略模块文档"""; from __future__ import annotations; import helper\n'
        "def initialize(context: MissingButDeferred):\n    context.value = helper.VALUE\n"
        "def handle_data(context, data):\n    pass\n",
        {"helper.py": "VALUE = 6\n"},
    )
    namespace = _execute(E.convert_strategy(root))
    assert namespace["__doc__"] == "策略模块文档"
    assert namespace["initialize"].__annotations__["context"] == "MissingButDeferred"


def test_simulator_main_guard_and_bootstrap_only_import_removed(tmp_path):
    source = "from ptrade_sim.runtime import BacktestEngine\n"
    source += "def initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n"
    source += "if __name__ == '__main__':\n    engine = BacktestEngine('fake')\n    engine.run()\n"
    root = _bundle(tmp_path, source)
    result = E.convert_strategy(root)
    assert result.report["ready"]
    assert "BacktestEngine" not in result.source
    assert "simulator_bootstrap_removed" in {d["code"] for d in result.report["diagnostics"]}
    _execute(result, {"__name__": "__main__"})
    assert (root / "strategy.py").read_text(encoding="utf-8") == source


def test_unrelated_main_guard_is_not_stripped(tmp_path):
    source = "def initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n"
    source += "if __name__ == '__main__':\n    VALUE = 12\n"
    result = E.convert_strategy(_bundle(tmp_path, source))
    namespace = _execute(result, {"__name__": "__main__"})
    assert namespace["VALUE"] == 12
    assert "if __name__ == '__main__':" in result.source


def test_simulator_used_in_trading_logic_is_blocked(tmp_path):
    source = "from ptrade_sim.runtime import BacktestEngine\ndef initialize(context):\n    BacktestEngine('fake')\n"
    source += "def handle_data(context, data):\n    pass\n"
    assert "simulator_dependency" in _errors(E.convert_strategy(_bundle(tmp_path, source)))


@pytest.mark.parametrize("dependency", ["duckdb", "polars", "unknown_helper", "jqdata", "loguru"])
def test_unknown_and_unportable_dependencies_are_not_imported(tmp_path, dependency):
    source = f"import {dependency}\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n"
    result = E.convert_strategy(_bundle(tmp_path, source))
    assert "unknown_dependency" in _errors(result)
    diagnostic = next(d for d in result.report["diagnostics"] if d["code"] == "unknown_dependency")
    assert diagnostic["path"] == "strategy.py" and diagnostic["line"] == 1
    assert diagnostic["severity"] == "error"


@pytest.mark.parametrize(
    "header,body",
    [
        ("import importlib", "importlib.import_module('helper')"),
        ("from importlib import import_module as load", "load('helper')"),
        ("", "__import__('helper')"),
        ("import importlib as il", "il.import_module(context.module)"),
    ],
)
def test_dynamic_local_imports_are_blocked(tmp_path, header, body):
    source = (
        header
        + "\ndef initialize(context):\n    "
        + body
        + "\ndef handle_data(context, data):\n    pass\n"
    )
    result = E.convert_strategy(_bundle(tmp_path, source, {"helper.py": "VALUE = 1\n"}))
    assert "dynamic_local_import" in _errors(result)


def test_known_dynamic_stdlib_import_can_remain_unchanged(tmp_path):
    root = _bundle(
        tmp_path,
        "import importlib\ndef initialize(context):\n    context.value = importlib.import_module('math').sqrt(9)\n"
        "def handle_data(context, data):\n    pass\n",
    )
    namespace = _execute(E.convert_strategy(root))
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.value == 3


@pytest.mark.parametrize(
    "expression,code",
    [
        ("get_current_data()", "unsupported_api"),
        ("get_history(3, field='close', adjustment='hfq')", "unsupported_api_keyword"),
        ("get_fundamentals('x', 'eps', date_type=1)", "unsupported_api_keyword"),
        ("set_parameters(individual_data_in_dict='1')", "unsupported_api_keyword"),
        ("eval(context.code)", "dynamic_code"),
    ],
)
def test_nonofficial_api_and_hidden_code_are_blocked(tmp_path, expression, code):
    source = (
        "def initialize(context):\n    pass\ndef handle_data(context, data):\n    "
        + expression
        + "\n"
    )
    result = E.convert_strategy(_bundle(tmp_path, source))
    assert code in _errors(result)
    assert any(d["line"] == 4 for d in result.report["diagnostics"] if d["code"] == code)


def test_local_and_comprehension_shadowing_is_not_misdiagnosed_as_api(tmp_path):
    source = "def initialize(get_current_data):\n    values = [get_current_data for get_current_data in range(2)]\n"
    source += "    nested = lambda get_current_data: get_current_data + 1\n"
    source += "    return (values, nested(2))\ndef handle_data(context, data):\n    pass\n"
    result = E.convert_strategy(_bundle(tmp_path, source))
    assert _execute(result)["initialize"](None) == ([0, 1], 3)


@pytest.mark.parametrize(
    "statement",
    [
        "g = object()",
        "log = object()",
        "get_strategy_params = lambda: {}",
        "def get_strategy_params():\n    return {}",
    ],
)
def test_reserved_global_rebindings_are_blocked(tmp_path, statement):
    result = E.convert_strategy(
        _bundle(
            tmp_path,
            statement
            + "\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n",
        )
    )
    assert _errors(result) & {"api_global_shadowing", "parameter_bridge_conflict"}


@pytest.mark.parametrize(
    "source",
    [
        "def initialize():\n    pass\ndef handle_data(context, data):\n    pass\n",
        "async def initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n",
        "def initialize(context):\n    pass\ndef handle_data(context, data, extra):\n    pass\n",
    ],
)
def test_incompatible_callback_signatures_are_blocked(tmp_path, source):
    assert "callback_signature" in _errors(E.convert_strategy(_bundle(tmp_path, source)))


def test_missing_mandatory_callback_is_not_silently_fabricated(tmp_path):
    source = "def initialize(context):\n    pass\n"
    assert "missing_callback" in _errors(E.convert_strategy(_bundle(tmp_path, source)))


def test_callbacks_can_be_reexported_from_helper(tmp_path):
    root = _bundle(
        tmp_path,
        "from helper import initialize, handle_data\n",
        {
            "helper.py": "def initialize(context):\n    context.value = 4\ndef handle_data(context, data):\n    pass\n"
        },
    )
    namespace = _execute(E.convert_strategy(root))
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.value == 4


def test_documented_target_python_35_has_compatible_generated_syntax(tmp_path):
    result = E.convert_strategy(_bundle(tmp_path), target_python="3.5")
    assert result.report["target_python"] == "3.5"
    assert result.report["ready"]
    ast.parse(result.source, feature_version=(3, 5))


@pytest.mark.parametrize(
    "header,expression",
    [
        ("", "f'{context}'"),
        ("from __future__ import annotations", "1"),
        ("import dataclasses", "1"),
    ],
)
def test_older_python_target_is_not_falsely_ready(tmp_path, header, expression):
    source = (
        header
        + "\ndef initialize(context):\n    "
        + expression
        + "\ndef handle_data(context, data):\n    pass\n"
    )
    assert _errors(E.convert_strategy(_bundle(tmp_path, source), target_python="3.5")) & {
        "source_syntax",
        "future_python_version",
        "dependency_python_version",
    }


def test_unknown_python_target_uses_config_error(tmp_path):
    with pytest.raises(StrategyConfigError):
        E.convert_strategy(_bundle(tmp_path), target_python="3.12")


def test_loguru_narrow_bridge_preserves_format_arguments(tmp_path):
    source = "from loguru import logger\ndef initialize(context):\n    logger.info('value {} {name}', 3, name='甲')\n"
    source += "    logger.warning('literal {braces}')\n    logger.error('bad')\n    logger.debug('debug')\n"
    source += "def handle_data(context, data):\n    pass\n"
    records = []
    log = SimpleNamespace(
        **{
            level: (lambda msg, level=level: records.append((level, msg)))
            for level in ("info", "warning", "error", "debug")
        }
    )
    result = E.convert_strategy(_bundle(tmp_path, source))
    namespace = _execute(result, {"log": log})
    namespace["initialize"](None)
    assert records == [
        ("info", "value 3 甲"),
        ("warning", "literal {braces}"),
        ("error", "bad"),
        ("debug", "debug"),
    ]
    assert "loguru_logger_adapted" in {d["code"] for d in result.report["diagnostics"]}


@pytest.mark.parametrize(
    "use",
    [
        "logger.bind(x=1)",
        "logger.add('x')",
        "logger.remove()",
        "logger.opt()",
        "logger.catch()",
        "logger.exception('x')",
        "context.logger = logger",
        "context.cb = logger.info",
    ],
)
def test_other_loguru_uses_are_explicitly_blocked(tmp_path, use):
    source = (
        "from loguru import logger\ndef initialize(context):\n    "
        + use
        + "\ndef handle_data(context, data):\n    pass\n"
    )
    assert "unsupported_loguru_usage" in _errors(E.convert_strategy(_bundle(tmp_path, source)))


def test_real_example_is_statically_exported_without_running_it():
    target = Path(__file__).resolve().parents[1] / "examples" / "demo_rotation"
    result = E.convert_strategy(target, params={"top_n": 2})
    assert result.report["ready"]
    assert result.report["params"]["top_n"] == 2
    assert "loguru_logger_adapted" in {d["code"] for d in result.report["diagnostics"]}


def test_write_export_paths_utf8_and_no_overwrite(tmp_path):
    root = _bundle(tmp_path, params={"name": "中文"})
    result = E.convert_strategy(root)
    paths = E.write_export(result, tmp_path / "exports")
    assert paths == {
        "source": tmp_path / "exports" / result.filename,
        "report": tmp_path / "exports" / "case_ptrade.manifest.json",
    }
    assert paths["source"].read_bytes() == result.source.encode("utf-8")
    assert json.loads(paths["report"].read_text(encoding="utf-8")) == result.report
    with pytest.raises(StrategyConfigError):
        E.write_export(result, tmp_path / "exports")
    assert E.write_export(result, tmp_path / "exports", overwrite=True) == paths
    assert list(paths["source"].parent.glob(".ptrade-export-*")) == []


def test_explicit_output_file_name_and_directory_with_py_suffix(tmp_path):
    result = E.convert_strategy(_bundle(tmp_path))
    paths = E.write_export(result, tmp_path / "renamed.py")
    assert paths["source"].name == "renamed.py"
    assert paths["report"].name == "renamed.manifest.json"
    directory = tmp_path / "directory.py"
    directory.mkdir()
    assert E.write_export(result, directory)["source"] == directory / result.filename


@pytest.mark.parametrize("output_name", ["strategy.py", "helper.py", "package/__init__.py"])
@pytest.mark.parametrize("overwrite", [False, True])
def test_writer_protects_all_original_sources_even_on_overwrite(tmp_path, output_name, overwrite):
    root = _bundle(
        tmp_path,
        "import helper, package\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n",
        {"helper.py": "VALUE=1\n", "package/__init__.py": "VALUE=2\n"},
    )
    result = E.convert_strategy(root)
    destination = root / output_name
    before = destination.read_bytes()
    with pytest.raises(StrategyConfigError):
        E.write_export(result, destination, overwrite=overwrite)
    assert destination.read_bytes() == before
    assert not destination.with_suffix(".manifest.json").exists()


def test_writer_protects_hardlink_alias_of_source(tmp_path):
    root = _bundle(tmp_path)
    result = E.convert_strategy(root)
    alias = tmp_path / "alias.py"
    os.link(root / "strategy.py", alias)
    with pytest.raises(StrategyConfigError):
        E.write_export(result, alias, overwrite=True)
    assert alias.read_bytes() == (root / "strategy.py").read_bytes()


def test_writer_protects_symlink_alias_of_source(tmp_path):
    root = _bundle(tmp_path)
    result = E.convert_strategy(root)
    alias = tmp_path / "alias.py"
    try:
        alias.symlink_to(root / "strategy.py")
    except OSError as exc:
        pytest.skip(f"当前文件系统没有创建符号链接能力：{exc}")
    with pytest.raises(StrategyConfigError):
        E.write_export(result, alias, overwrite=True)


def test_writer_checks_both_existing_destinations_before_creating_source(tmp_path):
    result = E.convert_strategy(_bundle(tmp_path))
    report = tmp_path / "out.manifest.json"
    report.write_text("original", encoding="utf-8")
    with pytest.raises(StrategyConfigError):
        E.write_export(result, tmp_path / "out.py")
    assert not (tmp_path / "out.py").exists()
    assert report.read_text(encoding="utf-8") == "original"


def test_blocked_export_writes_report_only_and_raises_strategy_error(tmp_path):
    source = "import missing_helper\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n"
    result = E.convert_strategy(_bundle(tmp_path, source))
    output = tmp_path / "blocked.py"
    with pytest.raises(StrategyError, match="报告"):
        E.write_export(result, output)
    assert not output.exists(), "blocked输出不得形成.py交付物"
    report = json.loads(output.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert report["ready"] is False
    assert report["diagnostics"] == result.report["diagnostics"]


def test_writer_rejects_path_traversal_filename_wrong_suffix_and_tamper(tmp_path):
    result = E.convert_strategy(_bundle(tmp_path))
    with pytest.raises(StrategyConfigError):
        E.write_export(replace(result, filename="../escape.py"), tmp_path / "exports")
    with pytest.raises(StrategyConfigError):
        E.write_export(result, tmp_path / "out.txt")
    with pytest.raises(StrategyError):
        E.write_export(replace(result, source=result.source + "# corrupt\n"), tmp_path / "out.py")
    assert not (tmp_path / "out.py").exists()


def test_source_read_failure_uses_strategy_import_error(tmp_path, monkeypatch):
    root = _bundle(tmp_path)
    original_read = Path.read_bytes

    def fail_entry(path):
        if path == root / "strategy.py":
            raise PermissionError("read denied")
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", fail_entry)
    with pytest.raises(StrategyImportError):
        E.convert_strategy(root)


def test_local_symlink_cannot_escape_bundle_root(tmp_path):
    root = _bundle(
        tmp_path,
        "import helper\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n",
    )
    outside = tmp_path / "outside.py"
    outside.write_text("raise RuntimeError('outside')\n", encoding="utf-8")
    try:
        (root / "helper.py").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"当前文件系统没有创建符号链接能力：{exc}")
    result = E.convert_strategy(root)
    assert "local_import_escape" in _errors(result)
    assert all(Path(s["path"]) != outside for s in result.report["sources"])


def test_legacy_frozen_source_does_not_search_original_live_helper(tmp_path):
    live = _bundle(
        tmp_path,
        "import helper\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n",
        {"helper.py": "VALUE = 3\n"},
    )
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    entry = frozen / "strategy_source.py"
    entry.write_bytes((live / "strategy.py").read_bytes())
    result = E.convert_strategy(entry, params={"frozen": 7})
    assert "unknown_dependency" in _errors(result)
    assert result.report["params"] == {"frozen": 7}
    assert result.report["modules"] == []


def test_bounded_source_and_module_graph_fail_closed(tmp_path, monkeypatch):
    root = _bundle(tmp_path)
    monkeypatch.setattr(E, "_MAX_SOURCE_BYTES", 10)
    assert "source_limit" in _errors(E.convert_strategy(root))


def test_final_params_replace_never_revives_config_defaults(tmp_path):
    root = _bundle(tmp_path, params={"old": 1, "nested": {"old": 2, "kept": 3}})
    empty = E.convert_strategy(root, params={}, params_mode="replace")
    assert empty.report["params"] == {}
    assert _execute(empty)["get_strategy_params"]() == {}
    final = {"nested": {"kept": 8}, "new": True}
    result = E.convert_strategy(root, params=final, params_mode="replace")
    assert result.report["params"] == final
    assert _execute(result)["get_strategy_params"]() == final
    assert E.convert_strategy(root, params=final).report["params"]["nested"] == {
        "old": 2,
        "kept": 8,
    }


@pytest.mark.parametrize("mode,params", [("unknown", {}), ("replace", None)])
def test_invalid_final_params_mode_uses_config_error(tmp_path, mode, params):
    with pytest.raises(StrategyConfigError):
        E.convert_strategy(_bundle(tmp_path), params=params, params_mode=mode)


def test_template_markers_in_user_params_and_helper_strings_are_not_reinterpreted(tmp_path):
    marker = "$P$ $PARAMS$ $MODULES$ $APIS$"
    root = _bundle(
        tmp_path,
        "import helper\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n",
        {"helper.py": "VALUE = '$P$ $PARAMS$ $MODULES$ $APIS$'\n"},
    )
    result = E.convert_strategy(root, params={"text": marker})
    namespace = _execute(result)
    assert marker == namespace["helper"].VALUE
    assert namespace["get_strategy_params"]("text") == marker


def test_helper_api_globals_can_be_injected_after_module_import(tmp_path):
    root = _bundle(
        tmp_path,
        "import helper\ndef initialize(context):\n    context.value = helper.value()\n"
        "def handle_data(context, data):\n    pass\n",
        {"helper.py": "def value():\n    return g.multiplier * get_strategy_params('n')\n"},
        params={"n": 3},
    )
    namespace = _execute(E.convert_strategy(root))
    namespace["g"] = SimpleNamespace(multiplier=4)
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.value == 12
    namespace["g"] = SimpleNamespace(multiplier=5)
    namespace["initialize"](context)
    assert context.value == 15, "helper必须读取当前API对象，而非首次import的过期副本"


@pytest.mark.parametrize(
    "call",
    [
        "get_history(20, '1d', ['600000.SS'], ['close'])",
        "get_history(20, '1d', context.pool, ['close'])",
        "get_history(20, field=['600000.SS'], security_list=['close'])",
    ],
)
def test_wrong_history_argument_order_is_blocked_not_silently_reordered(tmp_path, call):
    source = (
        "def initialize(context):\n    pass\ndef handle_data(context, data):\n    " + call + "\n"
    )
    result = E.convert_strategy(_bundle(tmp_path, source))
    assert "history_argument_order" in _errors(result)


@pytest.mark.parametrize(
    "call",
    [
        "get_history(20, '1d', ['close'], context.pool)",
        "get_history(20, field='close', security_list=context.pool)",
        "get_history(20, '1d', context.fields, context.pool)",
    ],
)
def test_official_history_order_and_dynamic_official_arguments_remain_unchanged(tmp_path, call):
    source = (
        "def initialize(context):\n    pass\ndef handle_data(context, data):\n    " + call + "\n"
    )
    result = E.convert_strategy(_bundle(tmp_path, source))
    assert result.report["ready"], result.report["diagnostics"]
    assert call in result.source


def test_proven_api_attributes_map_only_tokens_and_preserve_orders(tmp_path):
    source = "TEXT = 'total_value closeable_amount'\n"
    source += "def initialize(ctx):\n    pass\n"
    source += "def handle_data(ctx, data):\n    pf = ctx.portfolio\n    pos = pf.positions['X']\n"
    source += "    order_target('X', pos.closeable_amount * 2)\n"
    source += "    order_target_value('X', pf.total_value * 0.3)\n"
    root = _bundle(tmp_path, source)
    result = E.convert_strategy(root)
    records = []
    stubs = {
        "order_target": lambda c, n: records.append((c, n)),
        "order_target_value": lambda c, n: records.append((c, n)),
    }
    original = {"__name__": "original_fixture", **stubs}
    exec(compile(source, "fixture.py", "exec"), original)
    old = SimpleNamespace(
        portfolio=SimpleNamespace(
            total_value=1000, positions={"X": SimpleNamespace(closeable_amount=7)}
        )
    )
    original["handle_data"](old, None)
    expected = list(records)
    records.clear()
    exported = _execute(result, stubs)
    native = SimpleNamespace(
        portfolio=SimpleNamespace(
            portfolio_value=1000, positions={"X": SimpleNamespace(enable_amount=7)}
        )
    )
    exported["handle_data"](native, None)
    assert records == expected == [("X", 14), ("X", 300)]
    assert "pos.enable_amount * 2" in result.source
    assert "pf.portfolio_value * 0.3" in result.source
    assert "TEXT = 'total_value closeable_amount'" in result.source
    mapped = [d for d in result.report["diagnostics"] if d["code"] == "api_attribute_mapped"]
    assert {(d["from"], d["to"]) for d in mapped} == {
        ("total_value", "portfolio_value"),
        ("closeable_amount", "enable_amount"),
    }
    assert all(d["severity"] == "info" and d["line"] for d in mapped)
    assert (root / "strategy.py").read_text(encoding="utf-8") == source


def test_unicode_context_name_and_api_get_position_receiver_mapping(tmp_path):
    source = "def initialize(上下文):\n    上下文.value = 上下文.portfolio.total_value + get_position('X').closeable_amount\n"
    source += "def handle_data(context, data):\n    pass\n"
    result = E.convert_strategy(_bundle(tmp_path, source))
    namespace = _execute(result, {"get_position": lambda code: SimpleNamespace(enable_amount=3)})
    context = SimpleNamespace(portfolio=SimpleNamespace(portfolio_value=9))
    namespace["initialize"](context)
    assert context.value == 12


@pytest.mark.parametrize(
    "body",
    [
        "pf = context.portfolio\n    pf = context.other\n    context.value = pf.total_value",
        "pf = context.portfolio\n    if context.flag:\n        pf = context.other\n    context.value = pf.total_value",
        "context.value = context.other.total_value",
        "pf = context.portfolio\n    for item in data:\n        context.value = pf.total_value\n        pf = context.other",
        "pf = context.portfolio\n    try:\n        pf = context.other\n        raise RuntimeError()\n        pf = context.portfolio\n    except Exception:\n        context.value = pf.total_value",
    ],
)
def test_ambiguous_or_rebound_api_attribute_receivers_are_not_blindly_mapped(tmp_path, body):
    source = (
        "def initialize(context):\n    pass\ndef handle_data(context, data):\n    " + body + "\n"
    )
    assert "ambiguous_api_attribute" in _errors(E.convert_strategy(_bundle(tmp_path, source)))


def test_position_get_none_guard_and_values_loop_are_proven(tmp_path):
    source = "def initialize(context):\n    context.value = 0\n"
    source += "    pos = context.portfolio.positions.get('X')\n    if pos is not None:\n        context.value += pos.closeable_amount\n"
    source += "    for pos in context.portfolio.positions.values():\n        context.value += pos.closeable_amount\n"
    source += "def handle_data(context, data):\n    pass\n"
    result = E.convert_strategy(_bundle(tmp_path, source))
    namespace = _execute(result)
    context = SimpleNamespace(
        portfolio=SimpleNamespace(positions={"X": SimpleNamespace(enable_amount=5)})
    )
    namespace["initialize"](context)
    assert context.value == 10


def test_owned_custom_class_attribute_is_not_a_portfolio_alias(tmp_path):
    source = "class Quote:\n    def __init__(self):\n        self.total_value = 9\n"
    source += (
        "def initialize(context):\n    quote = Quote()\n    context.value = quote.total_value\n"
    )
    source += "def handle_data(context, data):\n    pass\n"
    result = E.convert_strategy(_bundle(tmp_path, source))
    namespace = _execute(result)
    context = SimpleNamespace()
    namespace["initialize"](context)
    assert context.value == 9
    assert "quote.total_value" in result.source
    assert not any(d["code"] == "api_attribute_mapped" for d in result.report["diagnostics"])


def test_documented_financial_table_total_value_column_is_not_rewritten(tmp_path):
    source = "def initialize(context):\n    pass\ndef handle_data(context, data):\n"
    source += (
        "    table = get_fundamentals('X', 'valuation')\n    context.value = table.total_value\n"
    )
    result = E.convert_strategy(_bundle(tmp_path, source))
    assert result.report["ready"], result.report["diagnostics"]
    assert "table.total_value" in result.source


def test_crlf_source_exports_exact_lf_utf8_bytes_matching_both_digests(tmp_path):
    root = _bundle(tmp_path)
    source = "TEXT = '中文'\r\ndef initialize(context):\r\n    pass\r\ndef handle_data(context, data):\r\n    pass\r\n"
    raw = source.encode("utf-8")
    (root / "strategy.py").write_bytes(raw)
    result = E.convert_strategy(root)
    paths = E.write_export(result, tmp_path / "production.py")
    written = paths["source"].read_bytes()
    assert written == result.source.encode("utf-8")
    assert b"\r\n" not in written
    assert (
        hashlib.sha256(written).hexdigest()
        == result.report["export_sha256"]
        == result.report["source_sha256"]
    )
    assert result.report["sources"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert (root / "strategy.py").read_bytes() == raw


@pytest.mark.parametrize("dependency", ["numpy", "pandas", "talib"])
def test_documented_third_party_dependencies_are_classified_without_importing_them(
    tmp_path, monkeypatch, dependency
):
    # Installed-package probes would execute foreign code during conversion.
    source = (
        "import "
        + dependency
        + "\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n"
    )
    root = _bundle(tmp_path, source)
    original_import = __import__

    def guard(name, *args, **kwargs):
        if name.split(".")[0] == dependency:
            pytest.fail("静态转换不应 import 策略的三方依赖")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guard)
    result = E.convert_strategy(root)
    assert result.report["ready"], result.report["diagnostics"]
    assert result.report["dependencies"] == [
        {"name": dependency, "kind": "documented_third_party", "path": "strategy.py", "line": 1}
    ]
    assert "import " + dependency in result.source


def test_relative_entry_import_is_blocked_at_the_original_line(tmp_path):
    source = "from .helper import VALUE\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n"
    result = E.convert_strategy(_bundle(tmp_path, source, {"helper.py": "VALUE=1\n"}))
    assert "relative_import_outside_package" in _errors(result)
    diag = next(
        d for d in result.report["diagnostics"] if d["code"] == "relative_import_outside_package"
    )
    assert (diag["path"], diag["line"]) == ("strategy.py", 1)


def test_local_file_cannot_masquerade_as_a_parent_package(tmp_path):
    source = "import helper.child\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n"
    root = _bundle(tmp_path, source, {"helper.py": "VALUE=1\n", "helper/child.py": "VALUE=2\n"})
    result = E.convert_strategy(root)
    assert "unknown_dependency" in _errors(result)
    assert result.report["modules"] == []
    assert {s["relative_path"] for s in result.report["sources"]} == {"strategy.py"}, (
        "helper.py 是模块而不是包，不能读入其旁边同名目录里的源码"
    )


def test_entry_reimport_is_blocked_instead_of_creating_duplicate_strategy_state(tmp_path):
    source = "import strategy\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n"
    root = _bundle(tmp_path, source)
    result = E.convert_strategy(root)
    assert "entry_reimport" in _errors(result)
    assert result.report["modules"] == []
    assert (root / "strategy.py").read_text(encoding="utf-8") == source


def test_module_budget_stops_before_reading_a_partial_helper_bundle(tmp_path, monkeypatch):
    source = "import helper\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n"
    root = _bundle(tmp_path, source, {"helper.py": "raise RuntimeError('not read')\n"})
    reads = []
    original_read = Path.read_bytes

    def counted(path):
        reads.append(path)
        return original_read(path)

    monkeypatch.setattr(E, "_MAX_MODULES", 0)
    monkeypatch.setattr(Path, "read_bytes", counted)
    result = E.convert_strategy(root)
    assert "module_limit" in _errors(result)
    assert root / "helper.py" not in reads
    assert result.report["modules"] == []


def test_plain_simulator_import_is_blocked_not_classified_as_an_external_library(tmp_path):
    source = "import ptrade_sim\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n"
    result = E.convert_strategy(_bundle(tmp_path, source))
    assert "simulator_dependency" in _errors(result)
    assert result.report["dependencies"][0]["kind"] == "simulator"


def test_logger_reassignment_is_not_silently_replaced_with_the_production_logger(tmp_path):
    source = "from loguru import logger\nlogger = None\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n"
    result = E.convert_strategy(_bundle(tmp_path, source))
    assert "unsupported_loguru_usage" in _errors(result)
    assert any(
        d["line"] == 2
        for d in result.report["diagnostics"]
        if d["code"] == "unsupported_loguru_usage"
    )


def test_missing_local_from_symbol_is_located_and_blocks_deployment(tmp_path):
    source = "from helper import MISSING\ndef initialize(context):\n    pass\ndef handle_data(context, data):\n    pass\n"
    result = E.convert_strategy(_bundle(tmp_path, source, {"helper.py": "VALUE=1\n"}))
    assert "missing_local_symbol" in _errors(result)
    assert any(
        d["line"] == 1 and d["path"] == "strategy.py"
        for d in result.report["diagnostics"]
        if d["code"] == "missing_local_symbol"
    )


def test_writer_refuses_an_existing_manifest_directory_even_when_overwrite_is_enabled(tmp_path):
    result = E.convert_strategy(_bundle(tmp_path))
    output = tmp_path / "production.py"
    report_dir = output.with_suffix(".manifest.json")
    report_dir.mkdir()
    marker = report_dir / "keep.txt"
    marker.write_text("existing", encoding="utf-8")
    with pytest.raises(StrategyConfigError, match="普通文件"):
        E.write_export(result, output, overwrite=True)
    assert not output.exists()
    assert marker.read_text(encoding="utf-8") == "existing"


def test_writer_rejects_non_json_report_before_creating_output_files(tmp_path):
    result = E.convert_strategy(_bundle(tmp_path))
    invalid = replace(result, report={**result.report, "unexpected": object()})
    output = tmp_path / "new_dir" / "production.py"
    with pytest.raises(StrategyError, match="有效 JSON") as caught:
        E.write_export(invalid, output)
    assert isinstance(caught.value.__cause__, TypeError)
    assert not output.parent.exists(), "报告序列化失败不得先留下部署目录或源码"


def test_writer_directory_creation_failure_keeps_source_and_output_untouched(tmp_path, monkeypatch):
    root = _bundle(tmp_path)
    before = (root / "strategy.py").read_bytes()
    result = E.convert_strategy(root)
    output = tmp_path / "denied" / "production.py"
    original_mkdir = Path.mkdir

    def denied(path, *args, **kwargs):
        if path == output.parent:
            raise PermissionError("directory denied")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(StrategyError, match="目录创建失败") as caught:
        E.write_export(result, output)
    assert isinstance(caught.value.__cause__, PermissionError)
    assert not output.exists()
    assert not output.with_suffix(".manifest.json").exists()
    assert (root / "strategy.py").read_bytes() == before


def test_no_overwrite_publish_handles_a_destination_created_after_preflight(tmp_path, monkeypatch):
    result = E.convert_strategy(_bundle(tmp_path))
    output = tmp_path / "production.py"
    report = output.with_suffix(".manifest.json")

    def raced_link(temporary, destination):
        assert Path(destination) == report
        Path(destination).write_text("concurrent owner", encoding="utf-8")
        raise FileExistsError("created after preflight")

    monkeypatch.setattr(E.os, "link", raced_link)
    with pytest.raises(StrategyConfigError, match="已存在") as caught:
        E.write_export(result, output)
    assert isinstance(caught.value.__cause__, FileExistsError)
    assert report.read_text(encoding="utf-8") == "concurrent owner"
    assert not output.exists(), "原子独占发布失败不得继续写部署源码"
    assert list(tmp_path.glob(".ptrade-export-*")) == []


def test_publish_io_failure_releases_the_staged_file_and_does_not_write_source(
    tmp_path, monkeypatch
):
    result = E.convert_strategy(_bundle(tmp_path))
    output = tmp_path / "production.py"

    def denied_link(*args, **kwargs):
        raise PermissionError("publish denied")

    monkeypatch.setattr(E.os, "link", denied_link)
    with pytest.raises(StrategyError, match="写入失败") as caught:
        E.write_export(result, output)
    assert isinstance(caught.value.__cause__, PermissionError)
    assert not output.exists()
    assert not output.with_suffix(".manifest.json").exists()
    assert list(tmp_path.glob(".ptrade-export-*")) == [], "发布失败必须清理自己创建的临时文件"


def test_ready_flag_cannot_make_an_empty_export_source_deployable(tmp_path):
    result = E.convert_strategy(_bundle(tmp_path))
    digest = hashlib.sha256(b"").hexdigest()
    invalid = replace(
        result,
        source="",
        report={**result.report, "source_sha256": digest, "export_sha256": digest},
    )
    output = tmp_path / "production.py"
    with pytest.raises(StrategyError, match="源码为空"):
        E.write_export(invalid, output)
    assert not output.exists()
    assert output.with_suffix(".manifest.json").is_file()
