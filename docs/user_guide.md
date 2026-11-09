# ptrade 本地回测框架使用指南：配置、回测、看板与 API

[返回 README](<../README.md>) · [数据链](<native_data_chain.md>) · [Rust 与策略导出](<rust_acceleration_and_export.md>) · [Windows 安装](<windows_release.md>)

本文命令从仓库根目录运行。Windows 示例显式调用虚拟环境，不依赖激活状态；主包 Python ≥3.10，完整开发环境建议 Python 3.12。源码安装和前端构建见 [README](<../README.md#安装与构建>)。

本指南对应 `ptrade-local-backtest 0.1.0`；主 CLI 同名，`ptrade-local` 为短别名，旧 `ptrade-sim` 为兼容入口。公共导入门面 `ptrade_local_backtest` 提供 `BacktestEngine`、`convert_strategy`、`write_export`、`main` 与产品常量；内部 `ptrade_sim`、`PT_SIM_*`、`PTRADE_SIM_WEB_DIST` 与共享 `~/.ptrade-sim/queue` 保持兼容，发行名变化不要求重写策略导入。第一开发者/维护者为 JonePoon；参考与代码复用范围见 [作者与来源](<../THIRD_PARTY_NOTICES.md>)。

## 本地独立工作流

已安装依赖并准备好满足契约的本地数据后，回测、结果复盘与静态导出不连接 PTrade 云端。周末云端维护时可以继续这些本地操作；新的行情下载和 SDK 参考刷新按实际网络源另行执行。

1. 用 `db verify --db ./data/quant.duckdb --contract-only --json` 检查已有物理表契约。策略需要的区间、回看历史、因子和可选表仍要按自己的数据覆盖核对。
2. 用 `backtest --no-dashboard` 独立执行，再从结果目录启动看板查看净值、成交、日志与冻结配置。
3. 用 `export --target-python 3.11 --check --json` 检查策略，不需要行情库；静态报告与目标券商实际执行是不同验证层。

完整参数与命令见下文。依靠本机算力、列式数据和可选 Rust 索引减少云端排队依赖，不代表已测量对云端的速度倍数。

## 两套配置，不混用

| 配置 | 用途 | 路径基准 |
|---|---|---|
| [config.example.json](<../config.example.json>) | 回测公开结构与默认值 | 回测相对路径按启动工作目录解析 |
| `ptrade_config.json` / `.local_config.json` | 本机库路径、缓存、资源与默认策略 | 启动工作目录 |
| [示例策略配置](<../examples/demo_rotation/strategy_config.json>) | 策略名、参数、回测周期/区间 | 策略文件所在目录自动发现 |
| [data_config.example.json](<../data_config.example.json>) / `ptrade_data_config.json` | 数据湖、入库目标、源窗口、并发与 SDK 适配器 | 数据配置中的相对路径相对该 JSON 所在目录解析 |

回测自动加载的优先级由低到高：

~~~text
代码 DEFAULTS → config.example.json → ptrade_config.json（否则 .local_config.json）
              → 策略目录 strategy_config.json → PT_SIM_* → CLI
~~~

`backtest --config FILE` 改为“代码 DEFAULTS + 指定文件”，不再隐式叠加公开模板和机器私有文件；策略配置、环境变量与 CLI 仍会叠加。映射实现见 [config.py](<../src/ptrade_sim/config.py>)。

机器配置只需放本机差异，例如：

~~~json
{
  "db_path": "data/quant.duckdb",
  "output_dir": "backtest_results",
  "acceleration": {"backend": "auto"},
  "preload": {"mode": "rolling", "threads": 2},
  "cache": {
    "minute_memory_budget": "512MB",
    "duckdb_memory_limit": "2GB"
  },
  "queue": {"enabled": true, "max_parallel": 1}
}
~~~

模板/机器/策略配置的嵌套对象逐层合并。损坏的 JSON 配置目前会记录日志并被忽略，不应把“文件存在”当成“已生效”；最终结果以本次 run 的配置留档为准。

### 环境变量的实际映射

这些是 [ENV_KEYS](<../src/ptrade_sim/config.py#L68-L92>) 明确登记的名称，不是任意把嵌套键大写就会生效。

| 环境变量 | 配置键 |
|---|---|
| `PT_SIM_DB_PATH` | `db_path` |
| `PT_SIM_STRATEGY` | `strategy` |
| `PT_SIM_START_DATE` / `PT_SIM_END_DATE` | `start_date` / `end_date` |
| `PT_SIM_CAPITAL_BASE` / `PT_SIM_BENCHMARK` | `capital_base` / `benchmark` |
| `PT_SIM_FREQUENCY` | `frequency` |
| `PT_SIM_OUTPUT_DIR` | `output_dir` |
| `PT_SIM_ACCELERATOR` | `acceleration.backend` |
| `PT_SIM_EXPORT_PYTHON` | `export.target_python` |
| `PT_SIM_PRELOAD_MODE` | `preload.mode` |
| `PT_SIM_ROLLING_WINDOW_DAYS` | `preload.rolling_window_days` |
| `PT_SIM_THREADS` | `preload.threads` |
| `PT_SIM_MINUTE_MEMORY_BUDGET` | `cache.minute_memory_budget` |
| `PT_SIM_DAILY_CACHE_CAPACITY` | `cache.daily_capacity` |
| `PT_SIM_QUEUE_ENABLED` / `PT_SIM_MAX_PARALLEL` | `queue.enabled` / `queue.max_parallel` |
| `PT_SIM_QUEUE_POLL_INTERVAL` / `PT_SIM_QUEUE_MAX_WAIT_SEC` | `queue.poll_interval` / `queue.max_wait_sec` |
| `PT_SIM_COMMISSION_RATIO` / `PT_SIM_MIN_COMMISSION` | `cost.commission_ratio` / `cost.min_commission` |
| `PT_SIM_STAMP_TAX` / `PT_SIM_SLIPPAGE_RATIO` | `cost.stamp_tax` / `cost.slippage_ratio` |

`cache.duckdb_memory_limit`、`cache.minute_memory_ratio`、`queue.cpu_slots_limit` 等用配置文件设置，没有同名的通用环境映射。

~~~powershell
$env:PT_SIM_THREADS = "2"
$env:PT_SIM_MINUTE_MEMORY_BUDGET = "512MB"
$env:PT_SIM_ACCELERATOR = "auto"
& ./.venv/Scripts/ptrade-local-backtest.exe env --json
~~~

`env --json` 给出机器配置来源、库覆盖、资源、缓存与实际可用后端；它不是指定策略的最终合并报告。策略生效配置由本次 run 留档。

进程级变量：`PTRADE_SIM_WEB_DIST` 选择前端目录；`PT_DATA_CONFIG` 选择数据配置；`PT_SIM_PORT` 是直接调用 `python -m ptrade_sim.server` 的默认端口。通过 `ptrade-local-backtest dashboard` 启动时直接用 `--port`，避免依赖不同入口的默认值。

## 物理表与数据契约

[数据契约](<../src/ptrade_sim/data_contract.py>) 登记 10 张核心表，`db verify --list-contract` 是可执行清单。

| 表 | 契约 | 用途/缺失行为 |
|---|---|---|
| `ashare_calendar` | 必需 | `date` 交易日历 |
| `ashare_stock_basic` | 必需 | 证券、上市/退市事实 |
| `ashare_1d_stock` | 必需 | 日线、复权因子、历史简称与状态 |
| `ashare_1d_index` | 必需 | 基准日线 |
| `ashare_1m_stock` | 必需 | 分钟模式使用；daily 可保持空表，不做分钟预热 |
| `ashare_1m_index` | 可选 | 指数分钟 |
| `ashare_1d_feature` | 可选 | valuation 市值等 |
| `ashare_index_weight` | 可选 | 缺失时 `get_index_stocks` 返回空列表 |
| `ashare_index_info_basic` | 可选 | 指数元信息 |
| `ashare_l2_auction` | 可选 | 9:25 纯竞价量价；无数据时只能走实际存在的分钟近似 |

物理日期 `date` 为 `YYYYMMDD`；分钟 `trade_time` 为 `YYYY-MM-DD HH:MM:SS`。API/输出日期使用 ISO 格式。证券对策略统一为 `.SS/.SZ`，导入时兼容 `.SH`；股票量 `volume` 为股、成交额 `money` 为元、前收为 `preclose`。单位归一应在入库契约完成，不在每次取数时临时转换。

### 旧 Parquet 布局：db build

旧表布局不是 CNE 数据集布局。分区表按 `<data-dir>/<ashare_table>/year=YYYY/month=MM/day=DD/data.parquet` 读取，非分区表按 `<data-dir>/<ashare_table>/data.parquet`；例外来源由契约的 `source_kind/source` 指定。

~~~powershell
& ./.venv/Scripts/ptrade-local-backtest.exe db build --db ./data/quant.duckdb --data-dir ./data/source --start-year 2025 --end-year 2025 --threads 2 --memory 2GB

# 只校验物理契约。
& ./.venv/Scripts/ptrade-local-backtest.exe db verify --db ./data/quant.duckdb --contract-only --json

# 与旧布局源按指定交易日抽样核对。
& ./.venv/Scripts/ptrade-local-backtest.exe db verify --db ./data/quant.duckdb --data-dir ./data/source --start-year 2025 --end-year 2025 --dates "20250603,20250612" --json

# 仅观察尾缀归一计划，不改库。
& ./.venv/Scripts/ptrade-local-backtest.exe db normalize --db ./data/quant.duckdb --dry-run
~~~

`--tables` 可以筛选表；`db build --overwrite` 是显式重建对应表，不等价于原生 `data ingest` 的键级增量。大规模既有历史不需要为启用 Rust 索引重新转换。CNE lake 的 `daily_bars/minute_bars` 等输入使用 [数据链流程](<native_data_chain.md>)。

## 完整策略示例

优先使用仓库的 [主板动量轮动源码](<../examples/demo_rotation/strategy.py>) 与 [参数](<../examples/demo_rotation/strategy_config.json>)。目录中优先识别 `strategy.py`；没有它时只能有一个入口 `.py`；多个候选会明确报错。

也可将下面的完整单文件保存为 `strategies/buy_hold/strategy.py`。它只需给定股票和基准的行情，不依赖未来数据或未定义变量，语法兼容 3.5/3.11：

~~~python
def initialize(context):
    g.security = get_strategy_params("security", "600000.SS")
    g.target_fraction = float(get_strategy_params("target_fraction", 0.5))
    set_universe([g.security])
    set_benchmark("000300.SS")
    set_commission(commission_ratio=0.0003, min_commission=5.0)
    run_daily(context, buy_once, time="09:31")


def buy_once(context):
    positions = context.portfolio.positions
    if g.security not in positions or positions[g.security].amount == 0:
        value = context.portfolio.portfolio_value * g.target_fraction
        order_target_value(g.security, value)


def handle_data(context, data):
    return None


def after_trading_end(context, data):
    log.info("期末资产 %.2f" % context.portfolio.portfolio_value)
~~~

同目录完整策略配置：

~~~json
{
  "name": "示例·买入持有",
  "frequency": "daily",
  "start_date": "2025-06-03",
  "end_date": "2025-06-12",
  "capital_base": 100000,
  "benchmark": "000300.SS",
  "params": {"security": "600000.SS", "target_fraction": 0.5}
}
~~~

`get_strategy_params` 是本平台扩展，导出时内嵌等价取参实现。不要用官方交易设置 `set_parameters` 假代替回测参数。

## 运行与周期

~~~powershell
& ./.venv/Scripts/ptrade-local-backtest.exe backtest --config ./config.example.json --strategy ./examples/demo_rotation --db-path ./data/quant.duckdb --start 2025-06-03 --end 2025-06-12 --capital 100000 --frequency daily --output-dir ./backtest_results --threads 2 --no-dashboard

& ./.venv/Scripts/ptrade-local-backtest.exe backtest --strategy ./examples/demo_rotation --db-path ./data/quant.duckdb --start 2025-06-03 --end 2025-06-12 --frequency minute --accelerator auto --port 8765
~~~

| 选项 | 含义 |
|---|---|
| `--config` | 指定配置，替换模板/机器自动发现层 |
| `--strategy` | 文件或目录入口 |
| `--start/--end` | 含端点的 ISO 日期 |
| `--capital/--db-path` | 初始资金/只读库路径 |
| `--frequency minute/daily` | 调度周期 |
| `--accelerator auto/python/rust` | 分钟索引后端 |
| `--output-dir` | run 结果根目录 |
| `--threads` | 预读线程偏好，资源准入仍会评估 |
| `--no-dashboard` | 不自动启动看板 |
| `--port` | 自动看板端口 |
| `--no-queue` | 本次跳过资源队列 |
| `--no-wait` | 资源不足直接返回，不等待 |

| 行为 | minute | daily |
|---|---|---|
| 调度 | 241 槽：09:30、09:31–11:30、13:01–15:00 | 每交易日 15:00 一次 |
| `handle_data` | 每槽一次（若定义） | 每交易日一次 |
| `run_daily` | 按指定时间 | 调度时间归一到 15:00 |
| 撮合参考价 | 当前 bar close；盘前市价单优先 09:31 close | 当日日线 close |
| 分钟历史 | 从实际 1m 行组装/重采样 | 返回空，不启动分钟预热 |

`get_history` 支持 `1d/1m/5m/15m/30m/60m/120m`；高于 1m 的分钟历史是从 1m 回测数据重采样，不是自动改用独立 TDX 5m lake。TDX 来源没有 09:30 bar 时不虚造，相关缺槽保持无数据。

### 撮合与费用

- T+1：当日新增持仓不能当日卖出，`closeable_amount` 反映可卖数量。
- 按板块/历史 ST 状态处理涨跌停；没有完整逐笔委托队列和部分成交模型。
- 价格保持源精度，复权通过 `adj_factor` 和 `fq=pre/post/dypre/None` 在取数时处理。
- 默认模拟费用：佣金 0.0003、最低 5 元，附加成交费 0.0000487，卖出印花税 0.001，比例滑点 0。
- 费用来自配置或策略 `set_commission/set_slippage/set_fixed_slippage`；默认值不会自动按历史政策日期变动。
- 停牌/缺证券 bar 使用现有填充与零量语义；首个无前收窗口可为 NaN，不能将它当成正常可成交数据。

### 资源、缓存与队列

默认滚动加载分钟日数据，容量/内存预算 + LRU。Rust 自有索引与包装对象也计入预算，不是独立不受控缓存。

~~~json
{
  "preload": {"mode": "rolling", "threads": 2},
  "cache": {"minute_memory_budget": "512MB", "duckdb_memory_limit": "2GB"},
  "queue": {"enabled": true, "max_parallel": 1, "max_wait_sec": 600}
}
~~~

`minute_memory_budget=auto` 取可用内存乘配置比率（默认 25%）；`preload.mode=all` 是全区间常驻。引擎缓存预算与 DuckDB 缓冲池是不同层，后者默认显式限制 2GB。DuckDB `memory_limit` 在同一进程同一库实例共享，不是每条连接独立额度。

队列默认放在 `~/.ptrade-sim/queue`，协调跨结果目录的回测。资源不足等待，受并发/CPU 槽位与等待上限限制。

~~~powershell
& ./.venv/Scripts/ptrade-local-backtest.exe queue --json
~~~

## 输出与错误判定

run 目录名取策略目录名；单文件取文件 stem。展示名来自策略配置 `name`，未设置则回退目录/文件名。

~~~text
backtest_results/<strategy-stem>-<timestamp>/
├── summary.json                  指标、数据错误/缺口与资源
├── daily_stats.csv               逐日净值，回测中持续写入
├── trades.csv                    成交；time=YYYY-MM-DD HH:MM:SS
├── run_config.json               生效配置留档（不含 preload 段）
├── strategy_config.json          策略配置副本
├── strategy_source.py            入口源码副本
├── strategy_export_report.json   通过/阻塞/错误均留档
├── strategy_export.py            仅静态转换通过时存在
├── progress.json                 运行状态与逐日进度
└── output.log                    回测日志
~~~

`summary.json` 包含收益、年化、Sharpe、回撤、月/年度统计、基准回归、费用、交易次数与资源画像。出现 `data_gaps` 表示历史回看覆盖不足；出现 `data_errors` 表示查询失败，CLI 返回数据退出码，而不是“完成”即有效结果。

| 退出码 | 类别 |
|---:|---|
| 0 | 成功 |
| 1 | 未分类/编程缺陷；某些用法错误 |
| 2 | 配置或策略定位；argparse 参数错误也使用 2 |
| 3 | 数据缺失、查询失败、数据任务失败/默认拒绝 partial |
| 4 | 策略导入/约定错误；静态导出阻塞 |
| 5 | 资源准入/队列 |
| 6 | 可选依赖/原生接口不可用 |
| 130 | 数据任务取消 |

回测的静态导出阻塞会留在报告中，但不改变该回测的成败；单独运行 `export` 的阻塞才返回 4。原生可执行文件的原始退出码有区别，控制层按 JSON 状态归一，见 [数据任务结果](<native_data_chain.md#结果与验收>)。

## 看板操作

先构建前端，然后在仓库根目录启动：

~~~powershell
$env:PTRADE_SIM_WEB_DIST = (Resolve-Path ./web/dist).Path
& ./.venv/Scripts/ptrade-local-backtest.exe dashboard --root ./backtest_results --host 127.0.0.1 --port 8765
~~~

打开 <http://127.0.0.1:8765>：

1. 运行列表查看状态、进度、区间和核心指标。
2. 点击 run 查看净值/回撤、月度/年度、成交分页、日志与源码。
3. “PTrade 转换”读取冻结快照；查看目标版本、参数和诊断，静态通过后下载。
4. “数据”页查询数据库/数据湖/原生工具状态；填写窗口、证券与数据集，先预览计划，再显式启动同步/下载/入库/校验。
5. 任务区查看阶段、进度、覆盖、错误与日志尾；取消只对服务拥有的任务有效。

看板结果页不参与回测。服务退出不终止独立的回测进程，但会取消服务自己拥有的数据任务。已有历史 task 的 PID 不用于重启后恢复或取消。数据页“隐藏本机路径”只影响页面展示，用于公开截图；不会修改 API 原值、有效配置或请求参数。

| HTTP | 用途 |
|---|---|
| `GET /api/ping` | 健康检查 |
| `GET /api/runs` | 运行列表 |
| `GET /api/run/{name}/detail` | 指标与曲线 |
| `GET /api/run/{name}/trades` | 成交分页 |
| `GET /api/run/{name}/log` | 日志分页 |
| `GET /api/run/{name}/source` | 入口源码 |
| `GET /api/run/{name}/export?format=json/python/manifest` | 冻结导出或历史副本静态转换 |
| `GET /api/data/status`、`GET /api/data/jobs` | 数据状态/任务列表，只读 |
| `POST /api/data/plan` | 只读计划预览，不启动任务 |
| `POST /api/data/jobs` | 显式创建任务，202 不等于任务成功 |
| `POST /api/data/jobs/{id}/cancel` | 拥有者限定取消 |

## API 支持清单

[api.py](<../src/ptrade_sim/api.py>) 注册 55 项，名称与 [接口测试](<../tests/test_api_surface.py>) 对齐。

| 分组 | 全部注册项 |
|---|---|
| 环境与调度（11） | `log`、`g`、`context`、`run_daily`、`get_frequency`、`get_business_type`、`is_trade`、`create_dir`、`get_research_path`、`get_user_name`、`get_strategy_params` |
| 设置（9） | `set_universe`、`set_benchmark`、`set_commission`、`set_slippage`、`set_fixed_slippage`、`set_volume_ratio`、`set_limit_mode`、`set_yesterday_position`、`set_parameters` |
| 行情（5） | `get_history`、`get_price`、`get_trend_data`、`get_snapshot`、`get_current_kline_count` |
| 证券信息（18） | `get_stock_name`、`get_stock_info`、`get_stock_status`、`filter_stock_by_status`、`get_Ashares`、`get_trade_days`、`get_all_trades_days`、`get_trading_day`、`get_trading_day_by_date`、`check_limit`、`get_fundamentals`、`get_index_stocks`、`get_stock_exrights`、`get_stock_blocks`、`get_industry_stocks`、`get_reits_list`、`get_market_list`、`get_market_detail` |
| 交易（9） | `order`、`order_value`、`order_target`、`order_target_value`、`cancel_order`、`get_open_orders`、`get_order`、`get_orders`、`get_trades` |
| 持仓（3） | `get_position`、`get_positions`、`get_all_positions` |

回调为 `initialize`、`before_trading_start`、`handle_data`、`after_trading_end`、`on_order_response`、`on_trade_response`。

### 受限实现与占位

- `get_Ashares`：沪深主板 + 创业板 + 科创板，排除北交所；按查询日的上市/退市日期，不用当前 `list_status` 过滤历史。
- `get_stock_info`：简称按回测日；未退市时 `de_listed_date=2900-01-01`；上市日是静态事实。
- `get_stock_status/filter_stock_by_status`：ST、停牌、退市按指定日；`DELISTING_SORTING` 不提供本地交易场景数据。
- `get_fundamentals`：只有 valuation；没有完整利润表/资产负债表/现金流表。
- `get_index_stocks`：依赖 `ashare_index_weight`；最新快照不被伪装成完整历史区间。
- `get_market_detail`：空 DataFrame，无逐笔盘口。
- `get_stock_exrights/get_stock_blocks/get_industry_stocks/get_reits_list`：占位，返回空值并在首次调用留日志。
- `set_volume_ratio/set_yesterday_position`：可调用的 no-op，不改变撮合行为。
- 实盘推送、真实委托队列、期货/期权完整交易模型不在本地实现范围。

[整理的官方参考](<ptrade_api.md>) 用于签名与官方语义核对；本地支持由源码和测试定义。
