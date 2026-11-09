"""PTrade 模拟回测看板 —— FastAPI 后端。

用法：
    ptrade-local-backtest dashboard                      # 默认根目录 ./backtest_results，端口 8765
    ptrade-local-backtest dashboard --port 9000 --root /path/to/backtest_results
    python -m ptrade_sim.server --port 9000   # 等价（不经 CLI）

功能：
    - GET /                看板页面（Vite 构建产物 web/dist，未构建则返回提示）
    - GET /api/ping        健康检查 / 端口复用探测
    - GET /api/runs        run 列表（状态/进度/时间/源码元信息/实时或最终指标）
    - GET /api/run/<name>/detail   详情（资金曲线/回撤/日志尾/指标/Beta-Alpha）
    - GET /api/run/<name>/trades   交易明细（服务端分页）
    - GET /api/run/<name>/log?lines=N&offset=M  日志尾部 N 行（可向前翻页）
    - GET /api/run/<name>/source   策略源码
    - GET /api/run/<name>/export?format=json|python|manifest  PTrade 单文件/报告

前端：仓库 ``web/``（Vite + Vue3 + TS），构建产物 ``web/dist`` 由本服务静态托管；
开发时可 ``cd web && pnpm dev``（vite dev server 代理 /api 到本服务）。

数据流：引擎逐日落盘 ``backtest_results/<run>/progress.json`` 与 ``daily_stats.csv``，
本服务读取并实时呈现。``ptrade-local-backtest backtest`` 通过 ``ensure_running()`` 自动拉起本服务。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ptrade_sim import derived, runstore
from ptrade_sim.config import DEFAULT_PORT, DEFAULT_RESULTS_DIR
from ptrade_sim.data_api import create_data_router
from ptrade_sim.exceptions import ConfigError, StrategyError
from ptrade_sim.runstore import TAIL_MAX_BYTES
from ptrade_sim.strategy_export import convert_strategy


def _web_dist_candidates() -> list[Path]:
    """前端构建产物候选目录（按优先级）：

    1. 环境变量 ``PTRADE_SIM_WEB_DIST``（显式覆盖）
    2. 包目录 ``<包>/web/dist``（随包分发前端的场景）
    3. 仓库根 ``web/dist``（开发态：src/ptrade_sim/server.py -> 上溯两级）
    4. 工作目录 ``./web/dist``
    """
    here = Path(__file__).resolve().parent
    out: list[Path] = []
    env = os.environ.get("PTRADE_SIM_WEB_DIST")
    if env:
        out.append(Path(env).expanduser())
    out.append(here / "web" / "dist")
    out.append(here.parent.parent / "web" / "dist")
    out.append(Path.cwd() / "web" / "dist")
    return out


def resolve_web_dist() -> Path | None:
    """返回首个存在的 dist 目录；均不存在返回 None。"""
    return next((p for p in _web_dist_candidates() if p.is_dir()), None)


# ============================================================
# 工具
# ============================================================


# ============================================================
# run 状态与指标
# ============================================================


# ============================================================
# 快照策略转换
# ============================================================


def _export_problem(
    code: str, message: str, *, path: str | None = None, report: dict | None = None
) -> HTTPException:
    """结构化导出失败；损坏的新快照不可悄悄退回旧源码。"""
    blocked = dict(report or {})
    diagnostics = blocked.get("diagnostics")
    blocked["ready"] = False
    blocked["diagnostics"] = [
        *(diagnostics if isinstance(diagnostics, list) else []),
        {
            "severity": "error",
            "code": code,
            "path": path,
            "line": None,
            "column": None,
            "message": message,
        },
    ]
    blocked.setdefault("params", {})
    return HTTPException(422, detail={"message": message, "report": blocked})


def _export_snapshot_file(d: Path, filename: str) -> Path | None:
    """仅允许该 run 内的普通文件；不追踪外部源码/快照符号链接。"""
    p = d / filename
    if not p.exists() and not p.is_symlink():
        return None
    if p.is_symlink() or not p.is_file() or not p.resolve().is_relative_to(d):
        raise _export_problem("unsafe_snapshot", "策略快照不是 run 内的普通文件", path=filename)
    return p


def _export_read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Python 的 JSON 解码器默认接受 NaN/Infinity；JSON API 不接受这些值。
        json.dumps(data, allow_nan=False)
    except (OSError, ValueError, UnicodeError) as exc:
        raise _export_problem(
            "invalid_snapshot_json", "策略快照 JSON 无法读取或解析", path=path.name
        ) from exc
    if not isinstance(data, dict):
        raise _export_problem("invalid_snapshot_json", "策略快照 JSON 必须是对象", path=path.name)
    return data


def _export_validate_report(report: dict) -> None:
    diagnostics = report.get("diagnostics")
    if not isinstance(diagnostics, list):
        raise _export_problem("invalid_export_report", "策略转换报告诊断必须是数组")
    valid = (
        isinstance(report.get("ready"), bool)
        and isinstance(report.get("params"), dict)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("severity"), str)
            and item["severity"] in {"error", "warning", "info"}
            and isinstance(item.get("code"), str)
            and isinstance(item.get("message"), str)
            for item in diagnostics
        )
    )
    if not valid or (
        report.get("ready") and any(item["severity"] == "error" for item in diagnostics)
    ):
        raise _export_problem("invalid_export_report", "策略转换报告结构不完整或就绪状态矛盾")


def _export_filename(filename: str | None, run_name: str) -> str:
    """保留中文，移除控制字符、路径分隔符及文件名特殊字符。"""
    raw = filename if isinstance(filename, str) and filename.strip() else f"{run_name}.py"
    stem = raw[:-3] if raw.lower().endswith(".py") else raw
    stem = re.sub(r"[^\w .-]", "_", stem).strip(" .")[:120].rstrip(" .")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if stem.split(".", 1)[0].upper() in reserved:
        stem = "_" + stem
    return f"{stem or 'strategy'}.py"


def _export_headers(filename: str) -> dict[str, str]:
    # ASCII fallback 供旧客户端使用；RFC 5987 保留合法 UTF-8 下载名。
    fallback = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
    return {
        "Content-Disposition": f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }


def _run_export(d: Path, run_name: str) -> dict:
    """读取冻结 pair，或静态转换历史 run 副本。O(源码字节数) 校验散列。"""
    report_path = _export_snapshot_file(d, "strategy_export_report.json")
    if report_path is not None:
        report = _export_read_json(report_path)
        _export_validate_report(report)
        filename = _export_filename(report.get("filename"), run_name)
        # blocked 冻结只写报告，无源码/散列也完整；不读取潜在旧候选代码。
        if not report["ready"]:
            return {"source": "", "filename": filename, "report": report}
        source_path = _export_snapshot_file(d, "strategy_export.py")
        if source_path is None:
            raise _export_problem(
                "incomplete_export_snapshot", "策略转换快照尚未完整保存", report=report
            )
        try:
            source_bytes = source_path.read_bytes()
            source = source_bytes.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise _export_problem(
                "invalid_export_source", "策略转换源码快照无法按 UTF-8 读取", report=report
            ) from exc
        digest = report.get("export_sha256") or report.get("source_sha256")
        if digest != hashlib.sha256(source_bytes).hexdigest():
            raise _export_problem(
                "snapshot_digest_mismatch",
                "策略转换快照校验失败，请等待完整快照或重新生成回测",
                report=report,
            )
        return {"source": source, "filename": filename, "report": report}

    if _export_snapshot_file(d, "strategy_export.py") is not None:
        raise _export_problem("incomplete_export_snapshot", "策略转换快照尚未完整保存")
    source_path = _export_snapshot_file(d, "strategy_source.py")
    if source_path is None:
        raise HTTPException(404, "run 中没有策略源码副本")
    params = {}
    # 最终运行参数已冻结（空字典也有效），不得重新合并策略默认参数。
    for filename in ("run_config.json", "strategy_config.json"):
        config_path = _export_snapshot_file(d, filename)
        if config_path is None:
            continue
        config = _export_read_json(config_path)
        params = config.get("params", {})
        if not isinstance(params, dict):
            raise _export_problem(
                "invalid_snapshot_params", "快照 params 必须是对象", path=filename
            )
        break
    try:
        result = convert_strategy(source_path, params=params, params_mode="replace")
    except (ConfigError, StrategyError, OSError, UnicodeError) as exc:
        raise _export_problem(
            "snapshot_conversion_failed", str(exc), path=source_path.name
        ) from exc
    _export_validate_report(result.report)
    filename = _export_filename(result.report.get("filename") or result.filename, run_name)
    return {"source": result.source, "filename": filename, "report": result.report}


# ============================================================
# FastAPI 应用
# ============================================================


def create_app(root: Path) -> FastAPI:
    """构造 FastAPI 应用：API 路由 + 前端静态托管。"""
    root = root.resolve()
    web_dist = resolve_web_dist()
    app = FastAPI(title="ptrade 本地回测框架", docs_url=None, redoc_url=None)
    app.include_router(create_data_router(root))

    @app.get("/api/ping")
    def ping() -> dict:
        return {"ok": True, "root": str(root)}

    @app.get("/api/runs")
    def api_runs() -> dict:
        return {"runs": runstore._json_clean(runstore.scan_runs(root))}

    @app.get("/api/run/{name}/detail")
    def api_detail(name: str) -> dict:
        d = runstore._safe_run_dir(root, name)
        if d is None:
            raise HTTPException(404, "run not found")
        status = runstore._run_status(d)
        return runstore._json_clean(
            {
                "name": name,
                "status": status["status"],
                "progress": status["progress"],
                "summary": runstore._run_metrics(d, status),
                "series": derived._series(d),
                "monthly_ext": derived._monthly_extended(d),
                "alpha_beta": derived._overall_alpha_beta(d),
                "logs": runstore._tail_lines(d / "output.log", 60),
            }
        )

    @app.get("/api/run/{name}/trades")
    def api_trades(
        name: str,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=500),
    ) -> dict:
        d = runstore._safe_run_dir(root, name)
        if d is None:
            raise HTTPException(404, "run not found")
        t = d / "trades.csv"
        if not t.exists():
            return {"rows": [], "total": 0}
        try:
            df = runstore._read_csv_retry(t)
        except (OSError, ValueError):
            return {"rows": [], "total": 0, "error": "trades.csv 读取失败"}
        total = df.height
        # 服务端分页：大 run（上千行）不全量下发，仅取当前页
        start = (page - 1) * page_size
        chunk = df.slice(start, page_size)
        return {
            "rows": runstore._json_clean(chunk.to_dicts()),
            "total": total,
        }

    @app.get("/api/run/{name}/log")
    def api_log(
        name: str,
        lines: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict:
        d = runstore._safe_run_dir(root, name)
        if d is None:
            raise HTTPException(404, "run not found")
        log = d / "output.log"
        if not log.exists():
            return {"lines": [], "total": 0}
        # 向前翻页需要更大的读窗：offset 越大越要读全文件，避免 64KB 截断
        max_bytes = TAIL_MAX_BYTES
        if offset > 0:
            # 粗略按需扩容：offset 行 × 200B 估窗，封顶 8MB（避免特大日志全读）
            max_bytes = max(TAIL_MAX_BYTES, min(offset * 200, 8 * 1024 * 1024))
        total = runstore._count_lines(log)
        return {
            "lines": runstore._tail_lines(log, lines, max_bytes=max_bytes, offset=offset),
            "total": total,
        }

    @app.get("/api/run/{name}/source")
    def api_source(name: str) -> dict:
        d = runstore._safe_run_dir(root, name)
        if d is None:
            raise HTTPException(404, "run not found")
        src = runstore._strategy_source(d)
        return {"path": src["path"], "exists": src["exists"], "source": src["source"]}

    @app.get("/api/run/{name}/export")
    def api_export(name: str, format: Literal["json", "python", "manifest"] = "json") -> Response:
        # '.' 可被旧 _safe_run_dir 解析为 root；导出必须绑定单个 run。
        d = None if name in {".", ".."} else runstore._safe_run_dir(root, name)
        if d is None or d == root:
            raise HTTPException(404, "run not found")
        result = _run_export(d, name)
        report = result["report"]
        if format == "python":
            if not report["ready"]:
                raise HTTPException(422, detail={"message": "策略转换存在阻塞项", "report": report})
            return Response(
                result["source"].encode("utf-8"),
                media_type="text/x-python; charset=utf-8",
                headers=_export_headers(result["filename"]),
            )
        if format == "manifest":
            filename = result["filename"] + ".manifest.json"
            return JSONResponse(report, headers=_export_headers(filename))
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/api/{path:path}", include_in_schema=False)
    def unknown_api(path: str) -> None:
        # 非法 API 路径不能被 SPA fallback 伪装成一次成功的导出。
        raise HTTPException(404, "API endpoint not found")

    # ---------- 前端静态托管（Vite 产物） ----------
    if web_dist is not None:
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(web_dist / "index.html")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            p = (web_dist / path).resolve()
            # 防穿越：只服务 dist 内文件；否则回退 index.html（SPA 路由）
            if p.is_file() and p.is_relative_to(web_dist.resolve()):
                return FileResponse(p)
            return FileResponse(web_dist / "index.html")

    else:

        @app.get("/", include_in_schema=False)
        def no_frontend() -> JSONResponse:
            return JSONResponse(
                {
                    "error": "前端未构建",
                    "hint": "在仓库 web/ 目录执行：pnpm install && pnpm build",
                    "api": "/api/runs",
                },
                status_code=503,
            )

    return app


# ============================================================
# 端口探测与常驻拉起（供 cli.py 调用）
# ============================================================


def _ping(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/ping", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_running(root: Path, port: int, timeout: float = 5.0) -> str | None:
    """确保看板服务在跑：已跑则复用（校验 root 一致），否则脱离终端常驻拉起。

    返回服务 URL；拉起失败返回 None（不阻断回测）。
    """
    url = f"http://127.0.0.1:{port}"
    if _ping(url):
        try:
            with urllib.request.urlopen(f"{url}/api/ping", timeout=1) as r:
                data = json.loads(r.read().decode("utf-8"))
            if data.get("root") == str(root.resolve()):
                return url
        except Exception:
            pass
        return None  # 端口被其他程序占用（或 root 不一致）

    cmd = [
        sys.executable,
        "-m",
        "ptrade_sim.server",
        "--root",
        str(root.resolve()),
        "--port",
        str(port),
    ]
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    # 让子进程继承当前包的导入路径（editable/源码运行场景）
    env = os.environ.copy()
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    if pkg_parent not in env.get("PYTHONPATH", "").split(os.pathsep):
        env["PYTHONPATH"] = os.pathsep.join(
            [pkg_parent] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )
    try:
        subprocess.Popen(cmd, close_fds=True, env=env, **kwargs)
    except Exception:
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _ping(url):
            return url
        time.sleep(0.2)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ptrade-local-backtest dashboard", description="PTrade 回测看板（FastAPI）"
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("PT_SIM_PORT", DEFAULT_PORT))
    )
    parser.add_argument("--root", type=Path, default=Path.cwd() / DEFAULT_RESULTS_DIR)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        print(f"根目录不存在：{root}")
        print("请先运行一次回测：ptrade-local-backtest backtest")
        return 1

    try:
        import uvicorn
    except ImportError:
        print(
            "缺少看板依赖。请安装：\n"
            "  uv tool install 'ptrade-local-backtest[dashboard]'   # 或\n"
            "  pip install 'ptrade-local-backtest[dashboard]'"
        )
        return 1

    dist = resolve_web_dist()
    if dist is None:
        print(
            "提示：前端尚未构建，页面将返回 503。在仓库 web/ 目录执行：\n"
            "  pnpm install && pnpm build"
        )
    print(f"PTrade 回测看板：http://{args.host}:{args.port}（根目录 {root}）")
    uvicorn.run(create_app(root), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
