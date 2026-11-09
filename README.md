# ptrade 本地回测框架

**本地行情 · PTrade Python 策略 · 独立回测/复盘 · 原生数据链 · 可视化工作台**

第一开发者与维护者：[JonePoon](https://github.com/JonePoon)。独立公开仓库：[JonePoon/ptrade-local-backtest](https://github.com/JonePoon/ptrade-local-backtest)，起始版本 **0.1.0**。

本仓库不是 ptrade-sim 的 GitHub fork 仓库，但参考并实际复用了 [ceke233/ptrade-sim](https://github.com/ceke233/ptrade-sim) 的策略 API、调度、撮合/账户、基础回测与看板等实现；上游基线为 **1d0094783ad3daac482e7fafc4d241e4969a2d4e**。不是券商、恒生/PTrade 或上游官方发行。作者和范围见 [AUTHORS](<AUTHORS.md>)、[第三方来源及许可](<THIRD_PARTY_NOTICES.md>)；[根 MIT](<LICENSE>) 保留 JonePoon 与 ceke233 版权行，[上游 MIT 全文](<LICENSES/ptrade-sim-MIT.txt>) 和 [tdxpy NOTICE](<native-fetch/NOTICE.tdxpy>) 原样保留。

[使用指南](<docs/user_guide.md>) · [原生数据链](<docs/native_data_chain.md>) · [Rust 索引与策略转换](<docs/rust_acceleration_and_export.md>) · [Windows 安装与发布](<docs/windows_release.md>) · [验收与基准](<docs/validation.md>) · [更新日志](<CHANGELOG.md>)

## 为何在本地跑

周末 PTrade 云端维护、云端回测服务暂不可用时，**事先备齐本地行情、日历、证券信息与所需参考数据**，即可在本机独立完成回测、历史复盘、看板查看与策略静态导出；这些操作不需要连接 PTrade 云端。行情更新或外部 SDK 参考刷新仍按各自数据源执行。

本机 CPU/内存、DuckDB 物理表、Polars/NumPy 列式数据与可选 Rust 分钟索引把计算和结果留在本地，减少对云端排队和服务窗口的依赖。项目没有 PTrade 云端速度对照实测，不给出“比云端快几倍”；本地资源队列、策略回调、IO 与索引成本仍可分别测量。

| 名称 | 0.1.0 约定 |
|---|---|
| 中文产品 | ptrade 本地回测框架 |
| Python 发行包 | `ptrade-local-backtest` |
| 主 CLI / 短别名 | `ptrade-local-backtest` / `ptrade-local` |
| 兼容 CLI | `ptrade-sim`，同一主入口 |
| 公共 Python 门面 | `ptrade_local_backtest`，提供回测/转换入口与产品常量 |
| 内部兼容命名空间 | `ptrade_sim` 保持不变，旧策略无需修改导入 |
| 可选索引发行 | `ptrade-local-backtest-native 0.1.0`；扩展模块 `_ptrade_sim_native` 保持不变 |
| 原生可执行文件 | `ptrade-fetch`、`ptrade-store` 保留名称，各 0.1.0 |

## 能做什么

- **本地回测**：分钟/日级调度，T+1、涨跌停、复权、佣金、印花税与滑点；读取既有 DuckDB 物理表。
- **PTrade API 边界**：55 个注册项，保留策略需要的 pandas 返回值；完整实现、受限实现和占位项分开说明。
- **可选 Rust 分钟索引**：精确、范围、批量定位；`auto`/`python`/`rust` 三后端，原生内存纳入统一 LRU 预算。
- **原生行情下载**：独立 `ptrade-fetch`，TDX TCP、解码、并发限流、重试、不可变 Parquet shard、SHA-256 清单和成功水位。
- **原生增量入库**：独立 `ptrade-store`，DuckDB **1.5.6**，按实际业务主键更新/插入，校验与来源检查点在同一事务提交。
- **策略静态转换**：CLI/看板生成 PTrade 单文件，冻结参数、局部模块与诊断；检查 Python **3.11 / 3.5** 目标，不执行输入策略。
- **可视化工作台**：运行列表、指标/曲线、成交、日志、源码/转换；数据页查看库/湖、预览计划、显式启动任务、进度和取消。

## 渐进 Rust 架构

~~~text
TDX TCP ── Rust ptrade-fetch ── 不可变 Parquet + 清单/水位 ─┐
                                                         ├─ Rust ptrade-store ── DuckDB 1.5.6 物理库
独立 CNE SDK ── 参考数据 / active revision / hfq 适配器 ────┘                           │
                                                                                 ↓
                                                                  Polars / NumPy 列式数据
                                                                                 ↓
                                                     Rust MinuteIndex（可选）+ Python 策略 / API
                                                                                 ↓
                                                        Python 调度 / 撮合 / 账户 / 指标 / run 产物
                                                                                 ↓
                                                             FastAPI + Vue 3 可视化工作台
~~~

Rust 负责新增的索引、下载和入库算法。Python 继续负责控制层、策略执行、PTrade API、撮合/账户和显式 SDK 适配器。DuckDB 查询引擎本来是 C++，Polars 本来是 Rust；数据库格式和既有交易语义不因增加原生模块而改变。

| 组件 | 运行形态 | 构建/安装 |
|---|---|---|
| 主包 | Python ≥3.10；开发命令使用 3.12 | [pyproject.toml](<pyproject.toml>)，Hatch wheel |
| 分钟索引 | 独立 PyO3 扩展，可不安装 | [native](<native/Cargo.toml>)，maturin / abi3 wheel |
| 行情下载 | Rust 独立可执行文件 | [native-fetch](<native-fetch/Cargo.toml>)，Cargo locked |
| DuckDB 入库 | Rust 独立可执行文件，嵌入 1.5.6 引擎 | [native-store](<native-store/Cargo.toml>)，Cargo locked |
| 参考数据 | 独立已配置的 CNE SDK 环境 | [SDK 适配边界](<docs/native_data_chain.md#cne-sdk-与版本边界>) |
| 前端 | Vue 3 / TypeScript / Vite | [web/package.json](<web/package.json>)；Node 24、pnpm 11.8.0 |

## 界面预览

以下图像由新品牌 0.1.0 页面重新采集，使用独立合成 run/data 夹具，不代表真实策略业绩。数据页经真实 FastAPI 读取合成库/湖；最终截图使用已核对来源的冻结原生组件，真实 doctor 返回 fetch 0.1.0 / store DuckDB v1.5.6，控制层只读计划 ready=true。三份留存任务的 partial/succeeded/failed 仍是合成状态，不是本次原生同步结果；显式 MOCK 的 start/cancel 交互另列于 [浏览器验收记录](<docs/validation.md>)。

### 运行列表与详情

![本地回测运行列表](<docs/images/local-dashboard.png>)

![净值、回撤、成交与日志详情](<docs/images/local-detail.png>)

### PTrade 静态转换

![目标解释器、冻结参数、转换代码与诊断](<docs/images/local-export.png>)

静态通过时可复制/下载单文件及报告；阻塞时仍可查看和下载诊断报告，生产代码下载/复制不可用。导出通过不等于在券商客户端实测通过。

![Python 3.5 的 source_syntax 阻塞与禁用代码下载](<docs/images/local-export-blocked.png>)

### 数据工作台与移动端

![数据状态、计划与任务工作台](<docs/images/local-data.png>)

这三张合成库存表（40 行日线、2 标的、20 日历）和一个 Parquet 只用于工作台展示，不是完整回测契约库。doctor/计划只证明对应探针和只读预览，不证明该演示数据已满足真实回测/同步覆盖。

![移动端本地回测工作台](<docs/images/local-mobile.png>)

[移动数据页完整截图：390px 视口](<docs/images/local-data-mobile.png>)

回测结果页只读取 run 产物；**数据页不是全部只读**。状态查询与计划预览只读，下载/入库/同步由明确的按钮或 CLI 命令启动。关闭看板不影响独立回测进程；看板自己拥有的数据任务会在服务退出时取消并清理。

## 安装与构建

主发行包为 **ptrade-local-backtest 0.1.0**，可选索引发行包为 **ptrade-local-backtest-native 0.1.0**。以下为本仓库源码安装和对应 [独立 GitHub Releases](https://github.com/JonePoon/ptrade-local-backtest/releases) 的构建/安装命令；附件是否已上传以实际 Release 为准。上游 PyPI `ptrade-sim` 不是这个独立项目的发行渠道。

### Windows 源码安装

在准备存放项目的目录运行；不需要激活虚拟环境。

~~~powershell
git clone https://github.com/JonePoon/ptrade-local-backtest.git
Set-Location ptrade-local-backtest
py -3.12 -m venv .venv
& ./.venv/Scripts/python.exe -m pip install --upgrade pip
& ./.venv/Scripts/python.exe -m pip install -e ".[duckdb,dashboard,perf]"
& ./.venv/Scripts/ptrade-local-backtest.exe --version
~~~

预期最后输出 `ptrade-local-backtest 0.1.0`。主包固定 `duckdb==1.5.6`；CNE SDK 使用另一环境，不让它的 DuckDB 依赖覆盖回测环境。

公共导入门面与兼容导入并存：

~~~python
from ptrade_local_backtest import BacktestEngine, convert_strategy, write_export

# 原策略已有的内部导入保持兼容，例如分钟索引工具仍在原命名空间。
from ptrade_sim.accelerator import backend_status
~~~

[公共门面](<src/ptrade_local_backtest/__init__.py>) 同时导出 `main`、`PROJECT_NAME`、`DISTRIBUTION_NAME`、`PRIMARY_DEVELOPER` 和 `__version__`；已有策略不需要因项目改名重写导入。

源码不携带构建好的前端。安装 Node 24 后构建：

~~~powershell
npm install --global pnpm@11.8.0
pnpm --dir web install --frozen-lockfile
pnpm --dir web run build
$env:PTRADE_SIM_WEB_DIST = (Resolve-Path ./web/dist).Path
~~~

只使用回测/导出 CLI 时不需要 Node。安装发布 wheel、原生 ZIP、独立索引 wheel 和 Windows 完整包的命令见 [Windows 安装与发布](<docs/windows_release.md>)。

Linux/macOS 的主包源码入口使用对应虚拟环境路径；其它构建命令选择本机工具链/平台，Windows 原生 wheel/EXE 不跨平台安装：

~~~bash
git clone https://github.com/JonePoon/ptrade-local-backtest.git
cd ptrade-local-backtest
python3.12 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -e ".[duckdb,dashboard,perf]"
./.venv/bin/ptrade-local-backtest --version
npm install --global pnpm@11.8.0
pnpm --dir web install --frozen-lockfile
pnpm --dir web run build
export PTRADE_SIM_WEB_DIST="$PWD/web/dist"
~~~

该源码命令适配运行下限；本项目此次实际验收的平台/解释器范围仍以 [validation](<docs/validation.md>) 为准。

### 可选原生组件

编译全部原生组件使用 Rust stable ≥1.85.1 与 Windows MSVC C++ 构建工具；仅使用发布二进制无需 Rust。

~~~powershell
# 分钟索引：独立安装，主包本身不触发 Rust 构建。
& ./.venv/Scripts/python.exe -m pip install ./native

# 下载与入库：直接运行 release locked 测试与构建。
foreach ($crate in @('native-fetch', 'native-store')) {
    cargo test --release --locked --manifest-path "$crate/Cargo.toml"
    if ($LASTEXITCODE -ne 0) { throw "原生测试失败：$crate" }
    cargo build --release --locked --manifest-path "$crate/Cargo.toml"
    if ($LASTEXITCODE -ne 0) { throw "原生构建失败：$crate" }
}

& ./native-fetch/target/release/ptrade-fetch.exe doctor
& ./native-store/target/release/ptrade-store.exe doctor
& ./.venv/Scripts/python.exe -c "from ptrade_sim.accelerator import backend_status; print(backend_status('rust'))"
~~~

下载器应报告原生能力；入库工具应报告 `engine_version=v1.5.6`；索引状态应为 `effective=rust`、`available=True`。构建、ABI 与错误条件见 [原生组件文档](<docs/rust_acceleration_and_export.md#依赖与安装>)。

## 快速开始：回测

### 1. 准备物理库

引擎只读 DuckDB，不在回测时扫描 Parquet 视图。表/字段以 [数据契约](<src/ptrade_sim/data_contract.py>) 为准。

| 必需表 | 用途 |
|---|---|
| `ashare_calendar` | 交易日历 |
| `ashare_stock_basic` | 证券基础、上市/退市日期 |
| `ashare_1d_stock` | 个股日线、前收、复权因子、历史状态 |
| `ashare_1d_index` | 基准指数日线 |
| `ashare_1m_stock` | 分钟回测行情；只跑 daily 可保留空表 |

另外五张可选契约表为 `ashare_1m_index`、`ashare_1d_feature`、`ashare_index_weight`、`ashare_index_info_basic`、`ashare_l2_auction`。缺可选数据会限制对应 API，不等于拥有完整财务/逐笔/盘口数据。

已有符合旧表布局的 Parquet 源可用 `db build`；CNE lake 使用新的 `data ingest/sync`，两种输入布局不混用。下面的旧布局命令假设源已放在本机 `data/source`：

~~~powershell
& ./.venv/Scripts/ptrade-local-backtest.exe db build --db ./data/quant.duckdb --data-dir ./data/source --start-year 2025 --end-year 2025
& ./.venv/Scripts/ptrade-local-backtest.exe db verify --db ./data/quant.duckdb --contract-only --json
& ./.venv/Scripts/ptrade-local-backtest.exe db verify --list-contract --json
~~~

日线/股票量单位为**股**，成交额为**元**，价格是未复权值。TDX 日线从手转换为股，TDX 分钟已经是股；指数未证实的量单位保留 unknown。详见 [数据单位、频率与 partial](<docs/native_data_chain.md#数据不变量>)。

### 2. 运行完整示例

仓库提供可直接运行的 [示例策略](<examples/demo_rotation/strategy.py>) 与 [策略配置](<examples/demo_rotation/strategy_config.json>)，包含完整动量排序、目标持仓和 T+1 检查，无需补充伪代码。示例使用 20 个交易日的回看窗口，库中应包含起始日期之前的相应历史。

~~~powershell
& ./.venv/Scripts/ptrade-local-backtest.exe backtest --strategy ./examples/demo_rotation --db-path ./data/quant.duckdb --start 2025-06-03 --end 2025-06-12 --frequency daily --no-dashboard

# 改为分钟周期，明确要求已安装的 Rust 索引。
& ./.venv/Scripts/ptrade-local-backtest.exe backtest --strategy ./examples/demo_rotation --db-path ./data/quant.duckdb --start 2025-06-03 --end 2025-06-12 --frequency minute --accelerator rust --no-dashboard
~~~

默认输出为 `backtest_results/<策略目录名>-<时间戳>/`。配置可放在策略目录；机器级路径与缓存放在私有配置。分层、全部环境变量、撮合与输出字段见 [使用指南](<docs/user_guide.md>)。

### 3. 打开看板

~~~powershell
& ./.venv/Scripts/ptrade-local-backtest.exe dashboard --root ./backtest_results --host 127.0.0.1 --port 8765
~~~

浏览器打开 <http://127.0.0.1:8765>。健康检查：

~~~powershell
Invoke-RestMethod http://127.0.0.1:8765/api/ping
~~~

源码安装须完成前端构建；发布 wheel 应带有前端。查看结果不执行策略，列表/详情从结果目录读取。新增 run 在开始时冻结源码、参数、转换代码与报告，历史导出不依赖后来改动的原策略。

## 快速开始：数据计划与任务

数据配置和回测配置分离。公开 [数据模板](<data_config.example.json>) 中的相邻 `cne_lake` 路径只是兼容布局，独立安装可用 CLI 或自己的数据配置选择目标。

~~~powershell
# 仅预览计划，不下载、不写库、不创建数据任务。
& ./.venv/Scripts/ptrade-local-backtest.exe data plan --config ./data_config.example.json --lake ./data/demo-lake --db ./data/demo.duckdb --start 2025-06-03 --end 2025-06-03 --symbols "000001.SZ,600000.SH" --datasets "daily_bars,minute_bars,minute_bars_5m" --json

& ./.venv/Scripts/ptrade-local-backtest.exe data status --config ./data_config.example.json --json
& ./.venv/Scripts/ptrade-local-backtest.exe data jobs --output-dir ./backtest_results --json
~~~

计划中的 `ready` 只反映所需原生工具可启动，不承诺源覆盖完整或参考数据齐全。完整配置、`fetch/ingest/sync/verify`、HTTP 任务、取消与 SDK 参考刷新见 [原生数据链](<docs/native_data_chain.md>)。

- 1m 和 5m 保持独立数据集、保留期、水位和覆盖报告；5m 不填入 1m 物理表。
- 回测调度有 241 个槽位，含 09:30；TDX 1m 源是 **240 根结束标签**，从 09:31 开始。下载器不会为了凑 241 根虚造竞价。
- `partial` 与 `succeeded` 分开；默认 `allow_partial=false`，部分完成以非零退出，不推进完整窗口成功水位。
- 已有正式库/数据湖不因安装、计划预览或打开页面而重建；入库仅按实际输入业务键变更，不按缺失日期删除旧行情。

## PTrade 单文件转换

不需要行情库，不执行策略。

~~~powershell
& ./.venv/Scripts/ptrade-local-backtest.exe export --strategy ./examples/demo_rotation --target-python 3.11 --output ./production/demo_rotation_ptrade.py --json
& ./.venv/Scripts/ptrade-local-backtest.exe export --strategy ./examples/demo_rotation --target-python 3.11 --check --json

# 示例含 f-string，3.5 目标应给出 source_syntax 诊断并退出 4。
& ./.venv/Scripts/ptrade-local-backtest.exe export --strategy ./examples/demo_rotation --target-python 3.5 --check --json
~~~

通过会生成 UTF-8/LF 单文件及同名 `.manifest.json`；阻塞时只输出报告。报告 `validation=static_only`：它证明已声明的静态检查通过，不等于已经在券商客户端执行。3.5 是目标语法检查，不会自动把所有现代 Python 特性降级。

## API 支持边界

注册清单与 [API 语义测试](<tests/test_api_surface.py>) 对齐。55 个注册项中包括受限项与占位项，不能解读为 55 个完整官方功能。

| 分组 | 数量 | 主要接口 |
|---|---:|---|
| 环境与调度 | 11 | `g/log/context/run_daily/get_frequency/get_strategy_params` |
| 设置 | 9 | `set_universe/set_benchmark/set_commission/set_slippage/set_limit_mode` |
| 行情 | 5 | `get_history/get_price/get_trend_data/get_snapshot/get_current_kline_count` |
| 证券信息 | 18 | `get_Ashares/get_stock_info/get_stock_status/get_trade_days/get_fundamentals/get_index_stocks` |
| 交易 | 9 | `order/order_value/order_target/order_target_value/cancel_order/get_orders/get_trades` |
| 持仓 | 3 | `get_position/get_positions/get_all_positions` |

`get_Ashares` 是按历史日期判断的沪深 A 股（含创业板/科创板，不含北交所）；`get_fundamentals` 仅 valuation；指数成分依赖对应表；除权/板块/行业/REITs 部分接口是首次调用留日志的占位；部分设置是 no-op；无实盘推送、逐笔委托队列与完整财务报表。完整列表见 [使用指南](<docs/user_guide.md#api-支持清单>)；[PTrade 参考文档](<docs/ptrade_api.md>) 描述官方接口，不是本地全部支持的承诺。

## 验收与性能

新独立版本的实测结果与预期命令分开记入 [验收记录](<docs/validation.md>)，Lead 按完成并收集的结果更新 [逐项验收 JSON](<docs/acceptance.json>)；阅读时以 release/date 区分版本，来源计数不自动成为 0.1.0 的新验收。来源 [Windows 基准 JSON](<docs/benchmarks/windows-x64-20261108.json>) 和本页关联的历史记录仍按当时工作量解释，不据此声称整版已验完。

新 [离线回归](<tests/test_local_offline.py>) 在 socket.connect 被阻断的条件下，用已准备的合成本地库分别跑 Python/Rust 两后端，各完成 5 日并实际产生交易和 Bar，连接尝试为 0；这是本地独立运行的定向证据，不是与云端的速度比较。

历史合成同工作量测试中，200 股票 × 20 日的**Python/Rust 引擎阶段**中位数分别为 **4.787479 s / 2.581132 s**，比值 **1.854798×（约 1.85×）**。两个后端处理 964,000 个 Bar、100 笔成交并验证结果散列一致。计时包括引擎构造、数据读、策略与 run IO，**不含数据库生成和最终流水线指标汇总**。

这个 1.85× 不是 PTrade 云端对照，也不是全部策略或下载→入库→回测端到端速度。更早的 1.83× 样本仍按原值保留在 [历史基准](<docs/validation.md#历史有效工作量基准>)；不同历史样本不混合计算。

## 开发与仓库结构

~~~text
ptrade-local-backtest/
├── src/ptrade_local_backtest/ 公共导入门面与主 CLI
├── src/ptrade_sim/       Python 控制、API、回测、看板与 SDK 适配
├── native/              可选 Rust/PyO3 分钟索引
├── native-fetch/        Rust TDX 下载与不可变 Parquet 发布
├── native-store/        Rust DuckDB 1.5.6 事务入库/校验
├── web/                 Vue 3 + TypeScript 前端源码
├── examples/            完整目录式示例策略与参数
├── scripts/             基准、原生构建、安装、发布打包、覆盖率门槛
├── docs/                使用、数据、架构、发布、验收与公开截图
├── AUTHORS.md           独立项目与基础代码作者
├── THIRD_PARTY_NOTICES.md 来源、许可范围与分发要求
├── LICENSE              JonePoon / ceke233 MIT 版权行
├── LICENSES/            ptrade-sim 原始 MIT 全文
├── config.example.json  回测公开模板
└── data_config.example.json  数据链公开模板
~~~

开发安装和检查：

~~~powershell
& ./.venv/Scripts/python.exe -m pip install -e ".[dev,duckdb,dashboard,perf]"
& ./.venv/Scripts/python.exe -m ruff check src tests scripts
& ./.venv/Scripts/python.exe -m ruff format --check src tests scripts
& ./.venv/Scripts/python.exe -m mypy
& ./.venv/Scripts/python.exe -m pytest --cov=ptrade_sim --cov-report=json:coverage.json
& ./.venv/Scripts/python.exe scripts/check_coverage.py --json coverage.json
~~~

[贡献指南](<CONTRIBUTING.md>) 说明测试、数据契约、单向依赖、原生验证和发布约定。GitHub 使用独立仓库的 [Issues](https://github.com/JonePoon/ptrade-local-backtest/issues)；上游历史与作者归属保留在 [原项目](https://github.com/ceke233/ptrade-sim) 和 [LICENSE](<LICENSE>)。
