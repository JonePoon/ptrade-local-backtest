# 贡献指南

**ptrade 本地回测框架**是 [JonePoon/ptrade-local-backtest](https://github.com/JonePoon/ptrade-local-backtest) 的独立公开项目，第一开发者/维护者为 JonePoon，主发行版本 **0.1.0**。不是 ptrade-sim 的 GitHub fork 仓库，但参考并实际复用了 [ceke233/ptrade-sim](https://github.com/ceke233/ptrade-sim) 的基础实现；基线 **1d0094783ad3daac482e7fafc4d241e4969a2d4e**。作者、复用范围与许可证见 [AUTHORS](<AUTHORS.md>)、[第三方来源](<THIRD_PARTY_NOTICES.md>)、[根 MIT](<LICENSE>)、[上游原始 MIT](<LICENSES/ptrade-sim-MIT.txt>) 和保持原样的 [tdxpy NOTICE](<native-fetch/NOTICE.tdxpy>)。

[使用指南](<docs/user_guide.md>) · [数据不变量](<docs/native_data_chain.md#数据不变量>) · [Rust/导出](<docs/rust_acceleration_and_export.md>) · [Windows 发布](<docs/windows_release.md>) · [实际验收](<docs/validation.md>)

## 开发环境

主包 Python ≥3.10，下方开发命令使用 3.12；新版本的实际执行结果单独记录，不继承来源版本的测试计数。CI 配置为 Python 3.10–3.13；这是配置矩阵，不代表某次本地执行了全部版本。前端 Node 24 / pnpm 11.8.0；三个原生组件一起构建用 Rust stable ≥1.85.1、rustfmt/clippy 和 MSVC C++（Windows）。

在准备存放源码的目录运行：

~~~powershell
git clone https://github.com/JonePoon/ptrade-local-backtest.git
Set-Location ptrade-local-backtest
py -3.12 -m venv .venv
& ./.venv/Scripts/python.exe -m pip install --upgrade pip
& ./.venv/Scripts/python.exe -m pip install -e ".[dev,duckdb,dashboard,perf]" build "maturin>=1.7,<2"
& ./.venv/Scripts/python.exe -m pre_commit install
npm install --global pnpm@11.8.0
pnpm --dir web install --frozen-lockfile
~~~

DuckDB extra 固定 1.5.6。CNE SDK 使用独立已配置环境，不把它的 DuckDB 2.0 依赖混入主环境；没有 SDK 的纯 Python/原生/legacy 合成测试不靠正式库兜底。

发行名改为 `ptrade-local-backtest`，主 CLI 同名，短别名 `ptrade-local`，主/短 CLI 经 `ptrade_local_backtest.main` 转入同一 CLI 实现；旧 CLI `ptrade-sim` 保留 `ptrade_sim.cli:main` 兼容入口。公共 Python 门面导出回测、静态转换/写出与产品常量，不移动已有内部实现。内部 `ptrade_sim` / `_ptrade_sim_native`、`PT_SIM_*` / `PTRADE_SIM_WEB_DIST` 和共享 `~/.ptrade-sim/queue` 是明确保留的兼容接口，不作全局品牌字符串替换。

## 检查命令

### Python

~~~powershell
& ./.venv/Scripts/python.exe -m ruff check src tests scripts
& ./.venv/Scripts/python.exe -m ruff format --check src tests scripts
& ./.venv/Scripts/python.exe -m mypy
& ./.venv/Scripts/python.exe -m pytest --cov=ptrade_sim --cov-report=term --cov-report=json:coverage.json
& ./.venv/Scripts/python.exe scripts/check_coverage.py --json coverage.json
~~~

需要主动格式化时运行 `python -m ruff format src tests scripts`，然后重新做 --check。Mypy 检查目标 3.12 与主包运行下限不同；配置在 [pyproject.toml](<pyproject.toml>)，不因此宣称主包最低运行版本升到 3.12。

常用定向回归：

~~~powershell
& ./.venv/Scripts/python.exe -m pytest -m unit -q
& ./.venv/Scripts/python.exe -m pytest -m integration -q
& ./.venv/Scripts/python.exe -m pytest tests/test_long_horizon_smoke.py tests/test_multimonth_metrics.py -q
& ./.venv/Scripts/python.exe -m pytest tests/test_accelerator.py tests/test_acceleration_integration.py -q
& ./.venv/Scripts/python.exe -m pytest tests/test_strategy_export.py tests/test_export_cli.py tests/test_server_export.py -q
& ./.venv/Scripts/python.exe -m pytest tests/test_data_tools.py tests/test_data_jobs.py tests/test_data_reference.py tests/test_data_api.py -q
& ./.venv/Scripts/python.exe -m pytest tests/test_release_packaging.py -q
~~~

最新实际计数与 skip 原因只在 [验收记录](<docs/validation.md>) 更新，避免文档多处固定一个即将过时的“总用例数/秒数”。

### 前端

~~~powershell
pnpm --dir web run test
pnpm --dir web run lint
pnpm --dir web run build
~~~

[build 脚本](<web/package.json>) 已包含 `vue-tsc -b && vite build`，不存在另一个 `pnpm run typecheck` 脚本。修改后用新 dist 运行 FastAPI 看板并刷新页面；结果页、数据页、导出通过/阻塞、桌面/手机分别验证。截图使用合成目录，不夹带真实账户、策略研究、私有路径或数据。

开发代理仅用于方便前端开发；数据写接口必须同源。不要把开发代理上的只读请求成功当成生产同源写/取消验证。

### 三个原生 crate

PyO3 单测需能找到主 Python DLL；只有实际构建扩展才开启 extension-module 特征。

~~~powershell
$env:PYO3_PYTHON = (Resolve-Path ./.venv/Scripts/python.exe).Path
$env:Path = (& ./.venv/Scripts/python.exe -c "import sys; print(sys.base_prefix)") + ";" + $env:Path
foreach ($crate in @("native", "native-fetch", "native-store")) {
    cargo fmt --manifest-path "$crate/Cargo.toml" --all -- --check
    if ($LASTEXITCODE -ne 0) { throw "fmt failed: $crate" }
    cargo test --release --locked --all-targets --manifest-path "$crate/Cargo.toml"
    if ($LASTEXITCODE -ne 0) { throw "tests failed: $crate" }
    cargo clippy --release --locked --all-targets --manifest-path "$crate/Cargo.toml" -- -D warnings
    if ($LASTEXITCODE -ne 0) { throw "clippy failed: $crate" }
}
& ./.venv/Scripts/python.exe -m maturin build --release --locked --manifest-path native/Cargo.toml --interpreter ./.venv/Scripts/python.exe --out ./native/dist
& ./.venv/Scripts/python.exe -m pip install --force-reinstall --no-deps ./native/dist/ptrade_local_backtest_native-0.1.0-cp310-abi3-win_amd64.whl
& ./scripts/build_native_data.ps1
~~~

然后再次执行实际安装扩展后的 Python 对照测试，检查 status 的有效 Rust 后端；缺扩展 skip 不可作为原生成功证据。release doctor 必须真实读取 native store v1.5.6。离线 TCP/Parquet/事务夹具和实际微窗网络是不同验证层。

## 测试约定

- 合成库从 [data_contract.py](<src/ptrade_sim/data_contract.py>) 构造；常规回归不依赖本机正式行情。
- 指标、缓存、资源或任何 `len/count > N` 分支变更后必须跑长区间合成回归。短区间可能绕过月度统计与大证券池缓存路径。
- [长区间测试](<tests/test_long_horizon_smoke.py>) 跨 14 个月；[月度指标测试](<tests/test_multimonth_metrics.py>) 用 pandas 独立对照偏度/峰度定义，不只检查“没有抛异常”。
- 关键机制写有效负例：破坏日期筛选、单位、散列、成功水位、事务或快照完整性时，断言应失败且指出后果。
- 缓存机制用计数器/事件证明命中、失效和淘汰；两次返回相等不是缓存命中证明。
- 避免恒真断言、`or True`、只看状态键存在、不判断真实工作量的空回测基准。
- 真实库索引对照只读，网络微窗/入库/取消用独立工作目标；测试不触碰正式湖、正式库或用户计划任务。
- 原生 fixtures 记录真实 TCP 分片/重试/并发/RPS，入库 fixtures 验证文件 SHA、键级修订、旧列、anchor、复权、状态、取消和回滚。

### 覆盖率 ratchet

[check_coverage.py](<scripts/check_coverage.py>) 除全局 70% 外还检查每模块下限；缺登记模块与水位由 [元测试](<tests/test_coverage_floors.py>) 约束。新增模块同步登记 FLOORS/ratchet 水位，覆盖率升高后上调，不降低旧门槛换取通过。

测量结果中的模块缺失或 coverage JSON 缺失必须失败，不通过 omit 隐去模块。实际分母、分支覆盖与 skip 需记录，不能把全局百分比当成所有分支已测。

## 架构约定

~~~text
cli → pipeline → runtime → {api, history, conventions, data_source, cache, accelerator}
server → {runstore, derived, strategy_export, data_api}
data_api / cli → data_jobs → data_tools → 原生进程 / SDK connector
~~~

[架构测试](<tests/test_architecture.py>) 强制核心依赖边界。底层契约、缓存、API、约定、异常不反向 import CLI/runtime；看板不为读取结果启动引擎；数据任务持有自己的原生进程，不接管用户其他进程。

- 内部数据通路使用 Polars/NumPy；pandas 只在 PTrade API 返回值/历史组装边界。
- Rust 索引、fetch、store 是可单独构建/测试/替换的模块，不把三种功能揉成一个隐式安装副作用。
- 主 wheel 不附带本机平台 EXE/DLL；原生工具 ZIP 与 ABI 索引 wheel 独立分发。
- 默认 auto 只在扩展确实未安装时回退；不兼容、导入依赖坏掉、ABI/API 变化必须明确报告。

## 数据与契约

[物理契约](<src/ptrade_sim/data_contract.py>) 是表/列口径唯一权威；字段变化先改契约和入库映射，再改取数/夹具/校验。列名/类型变动要在 PR 与 [CHANGELOG](<CHANGELOG.md>) 说明既有库兼容性，不能在取数处各自临时 `SELECT a AS b` 隐藏差异。

必须保持：

1. 股票 volume=股、money=元，TDX 日线手→股只转一次，分钟不 ×100；未知指数单位显式 unknown。
2. 原始 OHLC 与独立 hfq；历史 preclose 取可靠源/窗口之前 anchor，缺因子/前收不能默认补 1/当日 close 成功。
3. trade_date 用文件内真实日期，不用 Hive 月目录伪造；1m/5m 分开，240 个源结束标签不虚构成 241 根。
4. 历史名称/ST/退市按时点，不用当前 list_status/name 判历史；snapshot 成分不伪装时点拉链。
5. 增量按实际业务键，保留未输入旧数据/自定义列；失败/取消回滚数据与来源检查点。
6. CNE active revision + SHA verified native overlay；有 current pointer 时不能退回废弃 mutable。
7. SDK 与主 DuckDB 环境隔离，after 只在独立 merged view 派生并核验；不动原 generation/current pointer。
8. partial 永远可见，控制层默认非零，不把进度 100% 或 native return 0 当成全覆盖。

更完整的阶段/单位/事务语义见 [数据链](<docs/native_data_chain.md>)。

## 错误与退出码

可预期业务错误使用 [exceptions.py](<src/ptrade_sim/exceptions.py>)，不是全部裸 `ValueError`。

| 场景 | 类型/CLI 码 |
|---|---|
| 配置/策略定位 | ConfigError 系列，2 |
| 数据/数据库/查询失败 | DataError 系列，3 |
| 策略导入/约定 | StrategyError 系列，4 |
| 资源/队列 | QueueTimeoutError，5 |
| 可选依赖/原生接口 | DependencyError，6 |
| 编程缺陷 | TypeError/AttributeError 等，1 |

原有异常兼容靠多重继承（例如 `StrategyPathError(ConfigError, FileNotFoundError)`），同步更新 [异常测试](<tests/test_exceptions.py>)。具体子类映射排在父类前；编程缺陷不能吞成业务成功。

DuckDB 查询失败须累计计数/受限样本，写入 summary 的 data_errors 并非零退出；空查询结果、回看数据缺口、查询失败三个概念分开。数据 worker 也须保留 errors 与最终状态，不能只打印日志后返回 succeeded。

## 静态导出与快照

- 只做明确有依据的 AST 局部补丁，不进行证券/金额/任意字符串全局替换。
- 静态 target 3.5/3.11 检查与实际券商执行分开；ready 的 validation 固定 static_only。
- CLI/runtime 冻结最终 params 使用 replace，避免空参数/嵌套覆盖复活旧默认键。
- 入口和本地 helper 保留命名空间；未知依赖/动态导入/对象重绑需要诊断，不猜订单/API 语义。
- run 保存冻结 pair 或合法 report-only 阻塞；内容 SHA-256 验真实 UTF-8 字节，LF 原子写入。
- 半成品/损坏报告/散列不符不能回退到旧源码或外部路径；响应、下载、复制禁用逻辑需有效负例。

## 性能报告

~~~powershell
& ./.venv/Scripts/python.exe scripts/benchmark_acceleration.py --codes 1000 --queries 100000 --repeats 3 --engine-codes 200 --engine-days 20 --threads 2 --output ../work/ptrade-benchmark/benchmark.json
~~~

报告原始样本、中位数、机器/版本/线程、工作量、散列与计时范围。索引内核、引擎阶段、数据下载、入库、SDK、最终 pipeline 汇总分别计量。历史合成 1.85× 与更早 1.83× 各为同工作量 Python/Rust 引擎阶段的单独样本，不是对 PTrade 云端的测量，也不是全部策略或完整链路的倍数。没有端到端测量就不写“Rust 让所有回测/整个项目快多少倍”。

## 来源与许可审查

- 第一开发者 JonePoon 是本独立项目的创建/维护责任人，不覆盖 ceke233 和第三方的原始贡献。
- 复用/移植代码在 PR 中记录来源仓库、准确 commit、原作者、许可证及修改范围，不仅写“参考思路”。
- 不删除或改写上游版权与许可正文；新增第三方部分先更新 [来源通知](<THIRD_PARTY_NOTICES.md>) 及相应许可文件。
- 界面新图来自新品牌实际页面；历史原始基准、来源验收与 API 参考文档分别保留其身份，不重新包装为新版本的运行成果。

## PR 与发布

- Issue/PR 使用独立仓库的 [Issues](https://github.com/JonePoon/ptrade-local-backtest/issues)；上游修复说明保留来源与作者。
- 提交说明写原因、触发条件、兼容变化，尤其是时间、数量、单位和默认值。
- 每项改动附复现命令、退出码/状态、实际测试数、skip 原因、基准/错误报告与必要的合成夹具。
- 同步 [CHANGELOG](<CHANGELOG.md>)、相关 docs 与真实截图；没有新运行证据不改旧基准数字。
- 提交只包含本次源码/公开图文。正式库、Parquet、私有配置、研究目录、绝对私有路径和原始 work 日志不发布。
- [CI](<.github/workflows/ci.yml>) 与 [原生 CI](<.github/workflows/native-data.yml>) 配置存在不等于本次远端运行通过；推送后检查实际 workflow 结果。
- 发布须验证 wheel 内首页/assets、原生 manifest/doctor/安装、独立 ABI wheel、sdist 必需文件，并检查每种分发中的根 MIT、上游原 MIT、AUTHORS、THIRD_PARTY_NOTICES 和 tdxpy NOTICE；完整命令见 [发布文档](<docs/windows_release.md>)。

[每日入口模板](<integrations/cne_lake/README.md>) 是显式部署资料，不是安装 hook。数据任务/开发/发布都不会自动注册或修改用户计划任务。
