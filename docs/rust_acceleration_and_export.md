# Rust 分钟索引与 PTrade 静态转换

[返回 README](<../README.md>) · [回测使用](<user_guide.md>) · [数据链](<native_data_chain.md>) · [Windows 发布安装](<windows_release.md>) · [验收与基准](<validation.md>)

主发行 `ptrade-local-backtest 0.1.0` 与独立索引发行 `ptrade-local-backtest-native 0.1.0` 使用新项目身份；内部导入 `ptrade_sim` 与 `_ptrade_sim_native` 保持兼容。原生算法和 Python 基础回测的来源、修改及归属见 [第三方来源](<../THIRD_PARTY_NOTICES.md>)。

## 实施范围

这是渐进架构，不是全功能重写：

~~~text
DuckDB 1.5.6 物理表 → Polars / NumPy 行情列 → 可选 Rust MinuteIndex
                                                   ↓
                         Python PTrade API / 策略 → 调度、撮合、账户、指标与产物
~~~

分钟索引独立于新的 [原生下载/入库链](<native_data_chain.md>)。它不改数据库格式、价格精度、费用、T+1、涨跌停、复权或策略回调语义。Python 策略不需要改写成 Rust；DuckDB 查询是 C++、Polars 内部是 Rust，瓶颈应逐层测量。

| 模块 | 职责 |
|---|---|
| [native/Cargo.toml](<../native/Cargo.toml>) / [native/pyproject.toml](<../native/pyproject.toml>) | 独立可选 PyO3 wheel，不改变主包 Hatch 构建 |
| [core.rs](<../native/src/core.rs>) | 不可变索引，精确/范围/批量定位，自有内存统计 |
| [lib.rs](<../native/src/lib.rs>) | PyO3/NumPy 边界、整数校验、只读复制、批量释放 GIL |
| [accelerator.py](<../src/ptrade_sim/accelerator.py>) | 后端选择、ABI/API 核验、缺依赖/不兼容状态 |
| [runtime.py](<../src/ptrade_sim/runtime.py>) | 日行情索引、批量 BarData 定位、统一 LRU、冻结导出 |
| [strategy_export.py](<../src/ptrade_sim/strategy_export.py>) | AST 静态转换、局部补丁、本地模块、参数与定位诊断 |
| [cli.py](<../src/ptrade_sim/cli.py>) / [server.py](<../src/ptrade_sim/server.py>) | CLI 导出、冻结快照校验、JSON/单文件/报告下载 |
| [ExportDialog.vue](<../web/src/components/ExportDialog.vue>) | 目标版本、诊断、参数、代码预览与下载 |
| [benchmark_acceleration.py](<../scripts/benchmark_acceleration.py>) | 原始耗时、中位数、工作量与结果散列 |

## 依赖与安装

主包 Python ≥3.10，DuckDB 可选 extra 固定 1.5.6。原生索引自身最低 Rust 1.74，PyO3 0.23.5、numpy crate 0.23.0、maturin ≥1.7,<2；三个原生 crate 一起编译时使用 Rust stable ≥1.85.1。Windows 使用 MSVC C++ 工具链，发布 wheel 使用 `cp310-abi3-win_amd64`。

在仓库根目录，已建立主虚拟环境后安装：

~~~powershell
& ./.venv/Scripts/python.exe -m pip install -e ".[duckdb,dashboard,perf]"
& ./.venv/Scripts/python.exe -m pip install ./native
& ./.venv/Scripts/python.exe -c "from ptrade_sim.accelerator import backend_status; print(backend_status('rust'))"
~~~

主包安装不会自动要求 Rust。Windows x64 发布索引 wheel 可独立安装，Linux/macOS 从 [native 源码](<../native/README.md>) 构建对应平台 wheel，不能把 Windows wheel 装到其它平台。

### locked 测试与 release wheel

`extension-module` 特征只在构建扩展时开启；普通单测使用默认特征真实链接 Python，不为“跑过测试”禁用其 Python 边界。

~~~powershell
& ./.venv/Scripts/python.exe -m pip install "maturin>=1.7,<2"
$env:PYO3_PYTHON = (Resolve-Path ./.venv/Scripts/python.exe).Path
$env:Path = (& ./.venv/Scripts/python.exe -c "import sys; print(sys.base_prefix)") + ";" + $env:Path
cargo fmt --manifest-path native/Cargo.toml --all -- --check
cargo test --release --locked --manifest-path native/Cargo.toml --lib
cargo clippy --release --locked --manifest-path native/Cargo.toml --all-targets -- -D warnings
& ./.venv/Scripts/python.exe -m maturin build --release --locked --manifest-path native/Cargo.toml --interpreter ./.venv/Scripts/python.exe --out ./native/dist
& ./.venv/Scripts/python.exe -m pip install --force-reinstall --no-deps ./native/dist/ptrade_local_backtest_native-0.1.0-cp310-abi3-win_amd64.whl
~~~

## 后端选择与核验

| 后端 | 语义 |
|---|---|
| `auto` | 扩展确实未安装才回到 Python；可用即用 Rust；不兼容不是“缺扩展” |
| `python` | 明确参照路径，不导入/探测扩展；status=disabled |
| `rust` | 必须是可导入且 API 兼容的扩展，否则回测返回依赖退出码 6 |

配置 → 环境变量 → CLI，后者优先：

~~~json
{
  "acceleration": {"backend": "auto"},
  "export": {"target_python": "3.11"}
}
~~~

~~~powershell
$env:PT_SIM_ACCELERATOR = "rust"
& ./.venv/Scripts/ptrade-local-backtest.exe env --json
& ./.venv/Scripts/ptrade-local-backtest.exe backtest --strategy ./examples/demo_rotation --db-path ./data/quant.duckdb --start 2025-06-03 --end 2025-06-12 --frequency minute --accelerator rust --no-dashboard
~~~

`backend_status('rust')` 的预期字段是 requested=rust、effective=rust、available=true、state=available、version=0.1.0。实际 run 进度/资源报告也记录后端，不能只凭安装过 wheel 判断某次运行已启用 Rust。日级回测不执行分钟索引热路径。

### 索引契约与复杂度

- `row_of(code, slot)` 返回首个精确行，缺代码/槽为 -1。
- `rows_upto(code, slot, include)` 保持 NumPy searchsorted 的 left/right 半开范围语义，不跨过当前时刻。
- `rows_of(codes, slot)` 保留输入顺序与重复代码；一次 FFI 调用批量查询。
- 按证券范围定位平均 O(1)，每范围二分 O(log R)，批量 O(K log R)，构建/持有空间 O(N+C)。
- 构建复制 int32 槽位与证券范围；不依赖 NumPy 可变输入，支持只读/非连续数组。
- 内存统计含行情 NumPy 列、Python 范围对象、原生载荷及 PyO3 包装，受分钟 LRU 预算管理。
- 数据库读取、策略 Python 回调、DataFrame 返回值、BarData 构造、日志与磁盘 IO 仍是独立成本。

## PTrade 单文件静态转换

`export` 输入目录或单文件，不连接行情库，不执行输入策略。它是静态转换器，不是任意 Python 转译器、策略行为证明或券商客户端自动部署。

~~~powershell
# 默认输出路径也可由 --output 明确指定。
& ./.venv/Scripts/ptrade-local-backtest.exe export --strategy ./examples/demo_rotation --config ./config.example.json --output ./production/demo_rotation_ptrade.py --target-python 3.11 --json

# 只检查，不写源/报告文件。
& ./.venv/Scripts/ptrade-local-backtest.exe export --strategy ./examples/demo_rotation --target-python 3.11 --check --json

# Windows PowerShell 5.1 的原生程序参数须保留 JSON 双引号。
& ./.venv/Scripts/ptrade-local-backtest.exe export --strategy ./examples/demo_rotation --output ./production/custom_ptrade.py --params '{\"top_n\":2,\"watch_days\":10}' --target-python 3.11 --json

# 示例含 f-string；旧客户端目标应返回定位诊断并退出 4，不是成功降级。
& ./.venv/Scripts/ptrade-local-backtest.exe export --strategy ./examples/demo_rotation --target-python 3.5 --check --json
~~~

参数优先级是已生效回测/策略配置 → `--params-file` → `--params`。不同 PowerShell 版本的原生参数双引号规则不同，跨版本使用 UTF-8 参数文件可避免 shell 翻译；未提及的已生效参数继续保留。参数文件必须是 UTF-8 JSON 对象，CLI 也接受 UTF-8 BOM；示例用 Python 写无 BOM 文件。例如完整 `production/params.json`：

~~~json
{"top_n": 2, "watch_days": 10, "rebalance_days": 5}
~~~

~~~powershell
# 生成同一参数文件；Python 写 UTF-8 无 BOM，兼容 Windows PowerShell 5.1。
& ./.venv/Scripts/python.exe -c "import json,pathlib; p=pathlib.Path('production'); p.mkdir(exist_ok=True); (p/'params.json').write_text(json.dumps({'top_n':2,'watch_days':10,'rebalance_days':5}),encoding='utf-8')"
& ./.venv/Scripts/ptrade-local-backtest.exe export --strategy ./examples/demo_rotation --params-file ./production/params.json --output ./production/params_ptrade.py --target-python 3.11 --json
~~~

目标版本也可用 `export.target_python` 或 `PT_SIM_EXPORT_PYTHON`。它选择的是检查目标，不是运行主包的解释器版本。

### 3.5 / 3.11 诊断

报告使用目标版本 AST 解析，同时显式检查 f-string、变量注解、海象、match、位置专用参数、异步推导、except* 等语法家族及目标标准库/future 支持。宿主的 best-effort 解析标志不被冒称为真正的旧解释器执行。

- 3.5 兼容源码可通过，生成的取参/模块包装也兼容 3.5。
- f-string 至少 3.6、`__future__.annotations` 与 dataclasses 不属于 3.5 目标；会出现 `source_syntax/future_python_version/dependency_python_version` 等诊断。
- 不认识的目标版本是配置错误；CLI 可选值只有 3.5/3.11。
- `ready=true` 的报告仍为 `validation=static_only`，没有实际券商客户端执行证据。

### 转换规则与阻塞条件

1. 保留 PTrade 回调、`g/log/context` 和官方 API 调用。
2. 内嵌最终参数与 `get_strategy_params(key=None, default=None)`，保留浅副本/按键语义。CLI、runtime 和历史快照使用 `params_mode=replace`，不复活旧默认键；程序化 API 默认 merge 适用于局部覆盖。
3. 同目录 helper/package 打包到独立命名空间；不简单拼接源文件，避免名字冲突。
4. 只对 AST 定位且能够证明的对象属性/导入/本地启动引导做局部补丁。已知 Portfolio.total_value → portfolio_value、Position.closeable_amount → enable_amount；未知 receiver 或分支重绑不猜测。
5. 窄范围桥接常规 Loguru logger.info/warning/error/debug；`bind/add/opt/catch` 等扩展用法会诊断，不伪装兼容。
6. 动态本地导入、缺 helper、未知第三方依赖、未定义/非 PTrade API、无效回调、目标语法等使 ready=false。
7. 不做证券代码/金额/任意字符串的全局替换；源策略、helper、策略配置不被覆盖。

通过生成一个 UTF-8/LF `.py` 与同名 `.manifest.json`；报告包含原输入 SHA-256、输出 SHA-256、参数、依赖/API、局部模块与带路径/行列的 diagnostics。阻塞默认只写报告并返回 4；`--check` 两种情况都不写文件；已有导出须明确 `--overwrite`，源文件仍不能作为输出目标。

## 看板冻结快照

新 run 开始时冻结源码与静态导出，不在点击下载时去读取已改变的原策略：

~~~text
<run>/strategy_source.py
<run>/strategy_config.json
<run>/run_config.json
<run>/strategy_export.py                  仅 ready=true
<run>/strategy_export_report.json         通过/阻塞/错误均留档
~~~

- 优先冻结代码和最终有效 params；导出阻塞不妨碍本地回测本身继续。
- 旧 run 从留档入口源码转换；未留档的 helper 会阻塞，不到外部原路径寻找。
- 用真实 UTF-8 文件字节验 SHA-256，固定 LF 写入；不使用自动换行转换后的文本散列。
- 内容损坏、pair 半成品或散列不符返回 422，不悄悄回退到旧/外部源码。
- 合法 report-only 阻塞快照仍可取 JSON/manifest；生产代码下载返回 422。

~~~text
GET /api/run/{name}/export                   JSON：source / filename / report
GET /api/run/{name}/export?format=python     下载单文件
GET /api/run/{name}/export?format=manifest   下载报告
~~~

缺 run/入口副本为 404。导出响应禁用缓存，UTF-8 文件名使用 Content-Disposition。列表/详情的转换对话框提供参数、诊断、预览与下载；静态未通过时复制/下载代码不可用。

构建并启动：

~~~powershell
pnpm --dir web install --frozen-lockfile
pnpm --dir web run lint
pnpm --dir web run build
$env:PTRADE_SIM_WEB_DIST = (Resolve-Path ./web/dist).Path
& ./.venv/Scripts/ptrade-local-backtest.exe dashboard --root ./backtest_results --host 127.0.0.1 --port 8765
~~~

服务须使用新构建，浏览器刷新加载新资源。这里是 ptrade 本地回测框架自己的看板，不是其它宿主 GUI。

## 回归与基准

~~~powershell
& ./.venv/Scripts/python.exe -m pytest tests/test_accelerator.py tests/test_acceleration_integration.py tests/test_strategy_export.py tests/test_export_cli.py tests/test_server_export.py -q
& ./.venv/Scripts/python.exe -m pytest tests/test_long_horizon_smoke.py -q
& ./.venv/Scripts/python.exe scripts/benchmark_acceleration.py --codes 1000 --queries 100000 --repeats 3 --engine-codes 200 --engine-days 20 --threads 2 --output ../work/ptrade-benchmark/benchmark.json
~~~

默认基准只创建临时合成库；比较缺槽/重复/范围/批量定位及两个引擎的成交、费用、净值、最终资产与输出散列，验证非零 Bar/成交。按交替顺序运行两后端，保存原始每次耗时与 median。

已有真实库的只读对照必须明确库、策略、窗口：

~~~powershell
& ./.venv/Scripts/python.exe scripts/benchmark_acceleration.py --db-path ./data/quant.duckdb --strategy ./examples/demo_rotation --start 2025-06-03 --end 2025-06-12 --repeats 3 --threads 2 --output ../work/ptrade-benchmark/real-benchmark.json
~~~

计时包含引擎构造、读取、策略、进度/CSV/快照写入；不含数据库生成和最终流水线指标汇总。没有下载→入库→回测端到端倍数声明，也没有对 PTrade 云端的速度对照。历史 1.85× / 1.83× 仅为两组同工作量 Python/Rust 引擎阶段样本，不混合计算。历史合成实测、原始样本与最新回归分开保存在 [验收与基准](<validation.md>)。
