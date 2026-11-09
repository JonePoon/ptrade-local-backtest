"""策略导出 API 的独立夹具：防 live 源码漂移、半成品冻结和生产下载绕过。

不使用真实行情库，不复用旧 server 测试夹具。关键完整性测试另做内存变异验证。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

import pytest

pytest.importorskip("fastapi", reason="看板导出测试需要 fastapi")
pytest.importorskip("httpx", reason="TestClient 需要 httpx")

from fastapi.testclient import TestClient

from ptrade_sim import server
from ptrade_sim.exceptions import StrategyConfigError, StrategyImportError, StrategyPathError

pytestmark = pytest.mark.integration

RUN_NAME = "snapshot-20250101_120000"
SOURCE = "# 中文策略\r\nlabel = '冻结内容'\r\n"


def _json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _report(source: str = SOURCE, *, ready: bool = True, filename: str = "中文轮动.py") -> dict:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return {
        "ready": ready,
        "diagnostics": [],
        "params": {"top_n": 5, "label": "冻结参数"},
        "filename": filename,
        "source_sha256": digest,
        "export_sha256": digest,
    }


def _freeze(
    run: Path, source: str = SOURCE, *, ready: bool = True, filename: str = "中文轮动.py"
) -> dict:
    report = _report(source, ready=ready, filename=filename)
    if ready:
        (run / "strategy_export.py").write_bytes(source.encode("utf-8"))
    _json(run / "strategy_export_report.json", report)
    return report


def _no_convert(*args, **kwargs):
    raise AssertionError("冻结/缺失副本的 run 不得调用转换器或读取 live strategy")


@pytest.fixture
def export_results(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "backtest_results"
    run = root / RUN_NAME
    run.mkdir(parents=True)
    return root, run


@pytest.fixture
def export_client(export_results, monkeypatch):
    root, _ = export_results
    monkeypatch.setattr(server, "resolve_web_dist", lambda: None)
    with TestClient(server.create_app(root)) as client:
        yield client


@pytest.fixture
def fake_converter(monkeypatch):
    calls = []

    def convert(target: Path, params=None, *, params_mode="merge"):
        assert params_mode == "replace", "legacy API必须按冻结的完整参数字典转换"
        calls.append((target, params))
        source = target.read_text(encoding="utf-8")
        report = _report(source, filename="历史副本.py")
        report["params"] = params or {}
        return SimpleNamespace(source=source, filename=report["filename"], report=report)

    monkeypatch.setattr(server, "convert_strategy", convert)
    return calls


@pytest.mark.parametrize("format", ["json", "python", "manifest"])
def test_frozen_pair_overrides_changed_source_and_config(
    export_client, export_results, monkeypatch, format
):
    _, run = export_results
    report = _freeze(run)
    (run / "strategy_source.py").write_text(
        "raise RuntimeError('changed live source')\n", encoding="utf-8"
    )
    _json(run / "run_config.json", {"params": {"top_n": 99}})
    monkeypatch.setattr(server, "convert_strategy", _no_convert)
    monkeypatch.setattr(server.runstore, "_strategy_source", _no_convert)

    response = export_client.get(f"/api/run/{RUN_NAME}/export?format={format}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    if format == "json":
        assert response.json() == {"source": SOURCE, "filename": "中文轮动.py", "report": report}
    elif format == "python":
        assert response.content == SOURCE.encode("utf-8"), (
            "CRLF 与中文必须按冻结字节下载，不能正规化后破坏散列"
        )
        assert response.headers["content-type"] == "text/x-python; charset=utf-8"
        assert response.headers["x-content-type-options"] == "nosniff"
    else:
        assert response.json() == report
        assert response.headers["content-type"] == "application/json"
        assert (
            unquote(response.headers["content-disposition"].split("filename*=UTF-8''")[1])
            == "中文轮动.py.manifest.json"
        )


def test_json_is_default_and_frozen_filename_falls_back_to_run(export_client, export_results):
    _, run = export_results
    report = _freeze(run)
    report.pop("filename")
    _json(run / "strategy_export_report.json", report)
    response = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert response.status_code == 200
    assert response.json()["filename"] == RUN_NAME + ".py"


def test_blocked_report_preview_and_manifest_but_no_production_download(
    export_client, export_results, monkeypatch
):
    _, run = export_results
    report = _freeze(run, "", ready=False)
    report["diagnostics"] = [
        {
            "severity": "error",
            "code": "unsupported_sim_api",
            "path": "strategy.py",
            "line": 7,
            "column": 1,
            "message": "仅模拟运行时提供该 API",
        }
    ]
    _json(run / "strategy_export_report.json", report)
    monkeypatch.setattr(server, "convert_strategy", _no_convert)
    preview = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert preview.status_code == 200
    assert preview.json()["report"] == report
    assert preview.json()["source"] == ""
    download = export_client.get(f"/api/run/{RUN_NAME}/export?format=python")
    assert download.status_code == 422, "不能把 blocked 转换候选当作生产策略下载"
    assert download.json()["detail"]["report"] == report
    assert "content-disposition" not in download.headers
    manifest = export_client.get(f"/api/run/{RUN_NAME}/export?format=manifest")
    assert manifest.status_code == 200
    assert manifest.json() == report


def test_tampered_frozen_pair_never_falls_back(export_client, export_results, monkeypatch):
    _, run = export_results
    _freeze(run)
    (run / "strategy_export.py").write_text("# half-written newer source\n", encoding="utf-8")
    (run / "strategy_source.py").write_text("# old copy is available\n", encoding="utf-8")
    monkeypatch.setattr(server, "convert_strategy", _no_convert)
    for format in ("json", "python", "manifest"):
        response = export_client.get(f"/api/run/{RUN_NAME}/export?format={format}")
        assert response.status_code == 422, "缺少散列校验会把新源码与旧报告拼成貌似可部署的结果"
        report = response.json()["detail"]["report"]
        assert report["ready"] is False
        assert report["diagnostics"][-1]["code"] == "snapshot_digest_mismatch"


@pytest.mark.parametrize("remaining", ["strategy_export.py", "strategy_export_report.json"])
def test_incomplete_pair_is_not_a_legacy_run(export_client, export_results, monkeypatch, remaining):
    _, run = export_results
    if remaining.endswith(".py"):
        (run / remaining).write_text(SOURCE, encoding="utf-8")
    else:
        _json(run / remaining, _report())
    (run / "strategy_source.py").write_text("# old source\n", encoding="utf-8")
    monkeypatch.setattr(server, "convert_strategy", _no_convert)
    response = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert response.status_code == 422
    assert (
        response.json()["detail"]["report"]["diagnostics"][-1]["code"]
        == "incomplete_export_snapshot"
    )


@pytest.mark.parametrize(
    "change",
    [{"ready": 1}, {"params": []}, {"diagnostics": None}, {"diagnostics": [{"severity": []}]}],
)
def test_malformed_report_is_422_not_500(export_client, export_results, change):
    _, run = export_results
    report = _freeze(run)
    report.update(change)
    _json(run / "strategy_export_report.json", report)
    response = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert response.status_code == 422
    assert response.json()["detail"]["report"]["ready"] is False


def test_inconsistent_ready_report_is_not_deployable(export_client, export_results):
    _, run = export_results
    report = _freeze(run)
    report["diagnostics"] = [
        {"severity": "error", "code": "blocked", "message": "错误不可标记 ready"}
    ]
    _json(run / "strategy_export_report.json", report)
    response = export_client.get(f"/api/run/{RUN_NAME}/export?format=python")
    assert response.status_code == 422


@pytest.mark.parametrize("report_text", ["{", "[]", '{"ready": true, "params": {"x": NaN}}'])
def test_bad_frozen_json_does_not_use_old_source(
    export_client, export_results, monkeypatch, report_text
):
    _, run = export_results
    _freeze(run)
    (run / "strategy_export_report.json").write_text(report_text, encoding="utf-8")
    monkeypatch.setattr(server, "convert_strategy", _no_convert)
    response = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert response.status_code == 422
    assert response.json()["detail"]["report"]["diagnostics"][-1]["code"] == "invalid_snapshot_json"


def test_frozen_utf8_decoding_is_strict(export_client, export_results):
    _, run = export_results
    report = _freeze(run)
    (run / "strategy_export.py").write_bytes(b"\xff")
    report["export_sha256"] = hashlib.sha256(b"\xff").hexdigest()
    _json(run / "strategy_export_report.json", report)
    response = export_client.get(f"/api/run/{RUN_NAME}/export?format=python")
    assert response.status_code == 422
    assert response.json()["detail"]["report"]["diagnostics"][-1]["code"] == "invalid_export_source"


def test_frozen_digest_is_required(export_client, export_results):
    _, run = export_results
    report = _freeze(run)
    report.pop("export_sha256")
    report.pop("source_sha256")
    _json(run / "strategy_export_report.json", report)
    response = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert response.status_code == 422
    assert (
        response.json()["detail"]["report"]["diagnostics"][-1]["code"] == "snapshot_digest_mismatch"
    )


def test_source_sha256_alias_supports_earlier_frozen_report(export_client, export_results):
    _, run = export_results
    report = _freeze(run)
    report.pop("export_sha256")
    _json(run / "strategy_export_report.json", report)
    response = export_client.get(f"/api/run/{RUN_NAME}/export?format=python")
    assert response.status_code == 200
    assert response.content == SOURCE.encode("utf-8")


def test_blocked_only_report_requires_no_source_or_hash(export_client, export_results, monkeypatch):
    _, run = export_results
    report = _report("", ready=False)
    report.pop("export_sha256")
    report.pop("source_sha256")
    report["diagnostics"] = [
        {"severity": "error", "code": "snapshot_export_failed", "message": "转换阻塞"}
    ]
    _json(run / "strategy_export_report.json", report)
    monkeypatch.setattr(server, "convert_strategy", _no_convert)
    preview = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert preview.status_code == 200
    assert preview.json() == {"source": "", "filename": report["filename"], "report": report}
    assert export_client.get(f"/api/run/{RUN_NAME}/export?format=manifest").status_code == 200
    assert export_client.get(f"/api/run/{RUN_NAME}/export?format=python").status_code == 422


def test_blocked_report_does_not_expose_stale_source(export_client, export_results, monkeypatch):
    _, run = export_results
    report = _freeze(run, "", ready=False)
    (run / "strategy_export.py").write_text("# STALE-CANDIDATE\n", encoding="utf-8")
    monkeypatch.setattr(server, "convert_strategy", _no_convert)
    response = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert response.status_code == 200
    assert response.json()["source"] == ""
    assert response.json()["report"] == report
    assert "STALE-CANDIDATE" not in response.text


def test_legacy_uses_run_copy_and_final_run_params(export_client, export_results, fake_converter):
    root, run = export_results
    live = root.parent / "original.py"
    live.write_text("# 修改后的原始策略\n", encoding="utf-8")
    source = "# 旧 run 的中文副本\n"
    (run / "strategy_source.py").write_text(source, encoding="utf-8")
    _json(
        run / "run_config.json",
        {"strategy": str(live), "params": {"top_n": 8, "label": "运行覆盖"}},
    )
    _json(run / "strategy_config.json", {"params": {"top_n": 2}})
    _json(run / "progress.json", {"strategy": str(live)})
    response = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert response.status_code == 200
    assert fake_converter == [(run / "strategy_source.py", {"top_n": 8, "label": "运行覆盖"})]
    assert response.json()["source"] == source
    assert response.json()["report"]["params"] == {"top_n": 8, "label": "运行覆盖"}
    assert live.read_text(encoding="utf-8") == "# 修改后的原始策略\n"
    assert not (run / "strategy_export.py").exists(), "GET 不应在历史结果目录写转换产物"


@pytest.mark.parametrize("config", [None, {"params": {"top_n": 2}}])
def test_legacy_missing_run_config_uses_snapshot_defaults(
    export_client, export_results, fake_converter, config
):
    _, run = export_results
    (run / "strategy_source.py").write_text(SOURCE, encoding="utf-8")
    if config is not None:
        _json(run / "strategy_config.json", config)
    response = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert response.status_code == 200
    assert fake_converter == [(run / "strategy_source.py", config["params"] if config else {})]


@pytest.mark.parametrize("config", ["{", "[]", '{"params": null}', '{"params": []}'])
def test_bad_final_run_params_are_not_silently_replaced(
    export_client, export_results, monkeypatch, config
):
    _, run = export_results
    (run / "strategy_source.py").write_text(SOURCE, encoding="utf-8")
    (run / "run_config.json").write_text(config, encoding="utf-8")
    _json(run / "strategy_config.json", {"params": {"top_n": 2}})
    monkeypatch.setattr(server, "convert_strategy", _no_convert)
    response = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert response.status_code == 422, "配置损坏不可换成默认参数，改变当次回测交易逻辑"


def test_no_snapshot_does_not_read_recorded_live_path(export_client, export_results, monkeypatch):
    root, run = export_results
    live = root.parent / "live_secret.py"
    live.write_text("# NEVER-RETURN-LIVE-CONTENT\n", encoding="utf-8")
    for name in ("progress.json", "run_config.json"):
        _json(run / name, {"strategy": str(live), "source_path": str(live)})
    _json(run / "summary.json", {"config": {"strategy": str(live)}})
    monkeypatch.setattr(server, "convert_strategy", _no_convert)
    monkeypatch.setattr(server.runstore, "_strategy_source", _no_convert)
    response = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert response.status_code == 404
    assert "NEVER-RETURN-LIVE-CONTENT" not in response.text


@pytest.mark.parametrize(
    "name", ["..%2Foutside", "..%5Coutside", "%2E", "%2E%2E", "bad:name", "%00", "missing-run"]
)
def test_illegal_or_missing_run_is_404_even_with_spa(export_results, monkeypatch, tmp_path, name):
    root, _ = export_results
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>not an export</html>", encoding="utf-8")
    monkeypatch.setattr(server, "resolve_web_dist", lambda: dist)
    with TestClient(server.create_app(root)) as client:
        response = client.get(f"/api/run/{name}/export")
    assert response.status_code == 404, "API 穿越路径不能被 SPA index.html 伪装为 200"


def test_canonical_root_cannot_be_used_as_a_run(export_client, export_results, monkeypatch):
    root, _ = export_results
    _freeze(root)
    monkeypatch.setattr(server.runstore, "_safe_run_dir", lambda _root, _name: root)
    response = export_client.get("/api/run/root-alias/export")
    assert response.status_code == 404, (
        "Windows 点号目录别名或返回 root 的解析结果不能导出根目录文件"
    )


def test_utf8_download_filename_is_sanitized(export_client, export_results):
    _, run = export_results
    filename = '../坏名\\嵌套\r\n";<>?.py'
    _freeze(run, filename=filename)
    preview = export_client.get(f"/api/run/{RUN_NAME}/export").json()
    response = export_client.get(f"/api/run/{RUN_NAME}/export?format=python")
    assert response.status_code == 200
    header = response.headers["content-disposition"]
    assert "\r" not in header and "\n" not in header
    decoded = unquote(header.split("filename*=UTF-8''")[1])
    assert decoded == preview["filename"]
    assert "坏名" in decoded
    assert decoded.endswith(".py")
    assert not any(char in decoded for char in '/\\\r\n";<>?:*|')
    assert not decoded.startswith(".")


def test_windows_reserved_download_name_is_escaped(export_client, export_results):
    _, run = export_results
    _freeze(run, filename="CON.py")
    response = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert response.json()["filename"] == "_CON.py"


@pytest.mark.parametrize("format", ["zip", "Python", ""])
def test_invalid_format_is_rejected(export_client, format):
    response = export_client.get(f"/api/run/{RUN_NAME}/export?format={format}")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "format"]


@pytest.mark.parametrize(
    "error",
    [
        StrategyPathError("missing helper"),
        StrategyConfigError("bad params"),
        StrategyImportError("bad encoding"),
    ],
)
def test_expected_conversion_errors_are_reported(export_client, export_results, monkeypatch, error):
    _, run = export_results
    (run / "strategy_source.py").write_text(SOURCE, encoding="utf-8")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(server, "convert_strategy", fail)
    response = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert response.status_code == 422
    report = response.json()["detail"]["report"]
    assert report["ready"] is False
    assert report["diagnostics"][-1]["message"] == str(error)


def test_real_converter_exports_legacy_snapshot_and_final_params(export_client, export_results):
    _, run = export_results
    source = (
        "# 中文策略副本\n"
        "def initialize(context):\n"
        "    g.label = get_strategy_params('label')\n"
        "def handle_data(context, data):\n"
        "    log.info(g.label)\n"
    )
    (run / "strategy_source.py").write_text(source, encoding="utf-8")
    params = {"top_n": 8, "label": "最终嵌入"}
    _json(run / "run_config.json", {"params": params})
    preview = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert preview.status_code == 200
    body = preview.json()
    assert body["report"]["ready"] is True, body["report"]["diagnostics"]
    assert body["report"]["params"] == params
    assert body["report"]["validation"] == "static_only"
    assert body["report"]["target_python"] == "3.11"
    assert (
        body["report"]["export_sha256"]
        == hashlib.sha256(body["source"].encode("utf-8")).hexdigest()
    )
    # 只执行这里自建、只有函数定义的输出夹具，独立核验参数桥；不调用交易回调。
    namespace = {}
    exec(compile(body["source"], body["filename"], "exec"), namespace)
    assert namespace["get_strategy_params"]() == params
    download = export_client.get(f"/api/run/{RUN_NAME}/export?format=python")
    assert download.status_code == 200
    assert download.content == body["source"].encode("utf-8")
    assert (run / "strategy_source.py").read_text(encoding="utf-8") == source
    assert not (run / "strategy_export.py").exists(), "旧 run 的 GET 必须保持只读"


@pytest.mark.parametrize(
    "run_config",
    [
        {"params": {}},
        {"params": {"top_n": 8, "nested": {"selected": 1}}},
        {},
    ],
    ids=["empty-final", "nested-final", "missing-final-field"],
)
def test_legacy_effective_params_replace_strategy_defaults(
    export_client, export_results, run_config
):
    _, run = export_results
    source = (
        "def initialize(context):\n"
        "    g.params = get_strategy_params()\n"
        "def handle_data(context, data):\n"
        "    log.info('tick')\n"
    )
    (run / "strategy_source.py").write_text(source, encoding="utf-8")
    _json(
        run / "strategy_config.json",
        {
            "params": {
                "top_n": 2,
                "nested": {"default_only": 99, "selected": -1},
                "stale_default": "不可复活",
            }
        },
    )
    _json(run / "run_config.json", run_config)
    expected = run_config.get("params", {})
    preview = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert preview.status_code == 200
    body = preview.json()
    assert body["report"]["ready"] is True, body["report"]["diagnostics"]
    assert body["report"]["params"] == expected, (
        "最终参数必须整体替换默认参数，空字典和嵌套字段也不能被补回"
    )
    # 在自建的纯函数定义夹具上核验嵌入参数，不能只有报告看起来正确。
    namespace = {}
    exec(compile(body["source"], body["filename"], "exec"), namespace)
    assert namespace["get_strategy_params"]() == expected
    production = export_client.get(f"/api/run/{RUN_NAME}/export?format=python")
    assert production.status_code == 200
    assert production.content == body["source"].encode("utf-8")
    assert not (run / "strategy_export.py").exists(), "legacy GET 仍不得写快照"


def test_real_converter_blocks_sim_dependency_in_legacy_run(export_client, export_results):
    _, run = export_results
    source = (
        "from ptrade_sim.runtime import Engine\n"
        "def initialize(context):\n"
        "    g.engine = Engine()\n"
        "def handle_data(context, data):\n"
        "    log.info(g.engine)\n"
    )
    (run / "strategy_source.py").write_text(source, encoding="utf-8")
    preview = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert preview.status_code == 200
    body = preview.json()
    assert body["source"] == ""
    assert body["report"]["ready"] is False
    assert any(d["severity"] == "error" and d["line"] == 1 for d in body["report"]["diagnostics"])
    download = export_client.get(f"/api/run/{RUN_NAME}/export?format=python")
    assert download.status_code == 422
    assert download.json()["detail"]["report"]["ready"] is False
    assert export_client.get(f"/api/run/{RUN_NAME}/export?format=manifest").status_code == 200


@pytest.mark.parametrize(
    "artifact",
    ["strategy_source.py", "strategy_export.py", "strategy_export_report.json", "run_config.json"],
)
@pytest.mark.parametrize("unsafe_kind", ["symlink", "resolved_outside"])
def test_snapshot_boundary_predicates_without_os_link_privilege(
    export_client, export_results, monkeypatch, tmp_path, artifact, unsafe_kind
):
    # Windows 无 symlink 权限时仍独立验证两条边界判定及读取前拒绝；真实 OS 链接另测。
    _, run = export_results
    snapshot = run / artifact
    snapshot.write_text("SHOULD-NOT-BE-READ", encoding="utf-8")
    if artifact == "run_config.json":
        (run / "strategy_source.py").write_text(SOURCE, encoding="utf-8")
    if unsafe_kind == "symlink":
        original = Path.is_symlink

        def is_symlink(path):
            return path == snapshot or original(path)

        monkeypatch.setattr(Path, "is_symlink", is_symlink)
    else:
        original = Path.resolve

        def resolve(path, *args, **kwargs):
            return tmp_path / "outside.txt" if path == snapshot else original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(server, "convert_strategy", _no_convert)
    response = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert response.status_code == 422
    assert "SHOULD-NOT-BE-READ" not in response.text
    assert response.json()["detail"]["report"]["diagnostics"][-1]["code"] == "unsafe_snapshot"


@pytest.mark.parametrize(
    "artifact",
    ["strategy_source.py", "strategy_export.py", "strategy_export_report.json", "run_config.json"],
)
def test_artifact_symlink_cannot_escape_run(
    export_client, export_results, monkeypatch, tmp_path, artifact
):
    _, run = export_results
    external = tmp_path / "private.txt"
    external.write_text("PRIVATE-OUTSIDE-RUN", encoding="utf-8")
    if artifact == "run_config.json":
        (run / "strategy_source.py").write_text(SOURCE, encoding="utf-8")
    try:
        (run / artifact).symlink_to(external)
    except OSError as exc:
        pytest.skip(f"本机不支持创建测试符号链接：{exc}")
    monkeypatch.setattr(server, "convert_strategy", _no_convert)
    response = export_client.get(f"/api/run/{RUN_NAME}/export")
    assert response.status_code == 422
    assert "PRIVATE-OUTSIDE-RUN" not in response.text
    assert response.json()["detail"]["report"]["diagnostics"][-1]["code"] == "unsafe_snapshot"


def test_run_directory_symlink_cannot_escape_results(export_results, monkeypatch, tmp_path):
    root, _ = export_results
    outside = tmp_path / "outside_run"
    outside.mkdir()
    _freeze(outside)
    try:
        (root / "outside-link").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"本机不支持创建测试符号链接：{exc}")
    monkeypatch.setattr(server, "resolve_web_dist", lambda: None)
    monkeypatch.setattr(server, "convert_strategy", _no_convert)
    with TestClient(server.create_app(root)) as client:
        response = client.get("/api/run/outside-link/export")
    assert response.status_code == 404
