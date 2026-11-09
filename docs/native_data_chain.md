# ptrade 本地回测框架原生数据链：配置、下载、入库与任务

[返回 README](<../README.md>) · [Windows 工具安装](<windows_release.md>) · [回测使用](<user_guide.md>) · [验收记录](<validation.md>)

工具 ZIP 内附带本文副本；完整源码/相对链接可在 [GitHub 在线数据链文档](https://github.com/JonePoon/ptrade-local-backtest/blob/main/docs/native_data_chain.md) 查看。

本文对应独立项目 `ptrade-local-backtest 0.1.0`；原生工具仍为 `ptrade-fetch` / `ptrade-store`。原生数据 ZIP 名为 `ptrade-local-backtest-data-windows-x64.zip`；发行与来源许可见 [分发文档](<windows_release.md>)、[第三方通知](<../THIRD_PARTY_NOTICES.md>)。

## 架构与职责

~~~text
Rust ptrade-fetch：TDX TCP / zlib / 解码 / 分页 / 并发 / RPS / 重试
      ↓
不可变 Parquet shard + SHA-256 manifest + 完整子任务的成功水位
      ↓
Python 只读 resolver：CNE active revision + verified native overlay
      ↓                         ↘ 独立 SDK 环境：参考信息 / 隔离 merged-view hfq
Rust ptrade-store：DuckDB 1.5.6；staging → 校验 → 业务键 UPDATE/INSERT → COMMIT
      ↓
既有 PTrade 物理表 → 可选 Rust 索引 + Python 策略/API/撮合
~~~

下载与入库算法在两个独立 Rust 可执行文件内，不启动 Python 代替原生算法。Python 控制层负责配置、进程生命周期、SDK 边界、任务与最终状态。

| 模块 | 职责 |
|---|---|
| [native-fetch](<../native-fetch/Cargo.toml>) / [独立说明](<../native-fetch/README.md>) | TDX 协议、限流/重试、Parquet、清单与恢复 |
| [native-store](<../native-store/Cargo.toml>) / [独立说明](<../native-store/README.md>) | DuckDB 1.5.6、校验、事务增量写入 |
| [data_tools.py](<../src/ptrade_sim/data_tools.py>) | 数据配置、阶段编排、原生进程、SDK 适配、结果归一 |
| [data_jobs.py](<../src/ptrade_sim/data_jobs.py>) | 持久任务、幂等请求、进度、拥有者限定取消 |
| [data_reference.py](<../src/ptrade_sim/data_reference.py>) | active revision / native overlay 精确文件解析与隔离因子派生 |
| [data_api.py](<../src/ptrade_sim/data_api.py>) | 同源 HTTP、受限覆盖参数、任务接口 |
| [DataView.vue](<../web/src/views/DataView.vue>) | 数据状态、计划、操作、日志与覆盖 |
| [公开模板](<../data_config.example.json>) | 路径、窗口、频率、并发、主机、参考刷新 |
| [每日入口部署模板](<../integrations/cne_lake/daily_update.py>) | 保留旧调用形态，默认转入 Rust 数据 CLI |

## 配置与路径

数据配置独立于回测配置。公开模板的 `../cne_lake/lake` 与 `../cne_lake/out/fixed.duckdb` 是既有并列布局；独立安装可指定自己的湖和库，不会隐式改写回测的 `db_path`。

选择顺序：显式 `--config` → `PT_DATA_CONFIG` → 工作目录/仓库的 `ptrade_data_config.json` → 公开模板。加载后，`PT_DATA_*` 覆盖配置；CLI 再覆盖。数据 JSON 中的相对路径以该 JSON 所在目录为基准。

下面是完整的数据配置例子，保存为仓库根目录的 `ptrade_data_config.json`。它使用本机演示目标；没有完整参考数据的新湖可以先下载，但不能被假定已满足严格入库/回测契约。

~~~json
{
  "schema_version": 1,
  "lake": "../work/ptrade-data-demo/lake",
  "db": "../work/ptrade-data-demo/quant.duckdb",
  "start": "2026-11-06",
  "end": "2026-11-06",
  "trailing_days": 12,
  "symbols": ["000001.SZ", "600000.SH"],
  "datasets": [
    "daily_bars", "index_bars", "minute_bars", "minute_bars_5m",
    "corporate_actions", "instruments", "trading_calendar",
    "adj_factors", "index_constituents", "trading_status"
  ],
  "provider": "tdx",
  "tdx_hosts": [],
  "concurrency": 2,
  "timeout_secs": 20,
  "retries": 3,
  "requests_per_second": 4,
  "max_pages": 64,
  "page_size": 800,
  "threads": 2,
  "memory_limit": "2GB",
  "require_volume_v2": true,
  "allow_partial": false,
  "resume": true,
  "refresh": true,
  "fetch_exe": null,
  "store_exe": null,
  "reference_refresh": {
    "enabled": false,
    "python": "../cne_lake/.venv/Scripts/python.exe",
    "config": "../cne_lake/lake.toml",
    "timeout_secs": 1800,
    "before": [
      "instruments", "trading_calendar", "trading_status",
      "corporate_actions", "index_constituents"
    ],
    "after": ["derive_adj_factors"]
  }
}
~~~

字段说明：

- `start/end` 是含端点的 ISO 日期。未给 end 时用可读取的交易日历，否则有明确的工作日回退；未给 start 时从 end 回退 `trailing_days` 个自然日。预估窗口不是完整覆盖声明。
- `symbols=[]` 表示从已有 instruments 发现证券，不等价于原生下载器能凭空得到完整市场。
- `provider=tdx` 是当前原生下载源；当前 SH/SZ 路由受支持，未知路由明确失败。
- `tdx_hosts=[]` 在控制层会被去掉，使原生工具采用其默认主机；可显式配置 `[host,port]`。
- 并发、RPS、重试、页数、单页与超时均有边界校验。历史长度受 TDX 源保留期限制，不能把分页结束当成任意历史覆盖完整。
- `require_volume_v2=true` 对股票日线量单位采用严格版本校验。
- `fetch_exe/store_exe` 显式路径优先；未指定时依次寻找包内 bin、源码 release 目录与 PATH。
- `refresh=true` 追加新的修订 shard，不改旧文件；`resume=true` 按清单/摘要验证恢复，不只相信水位。

| 环境变量 | 含义 |
|---|---|
| `PT_DATA_CONFIG` | 数据配置文件 |
| `PT_DATA_LAKE` / `PT_DATA_DB` | 湖/目标库 |
| `PT_DATA_START` / `PT_DATA_END` | 日期窗口 |
| `PT_DATA_PROVIDER` | 源 |
| `PT_DATA_FETCH_EXE` / `PT_DATA_STORE_EXE` | 显式原生工具路径 |

## 构建与只读预览

在仓库根目录运行，主 Python 环境与原生工具安装方式见 [Windows 文档](<windows_release.md>)。

~~~powershell
& ./scripts/build_native_data.ps1
& ./native-fetch/target/release/ptrade-fetch.exe doctor
& ./native-store/target/release/ptrade-store.exe doctor

& ./.venv/Scripts/ptrade-local-backtest.exe data status --config ./ptrade_data_config.json --json
& ./.venv/Scripts/ptrade-local-backtest.exe data plan --config ./ptrade_data_config.json --output-dir ../work/ptrade-data-demo/jobs --json
~~~

`status` 和 `plan` 不下载、不入库、不创建持久任务目录。它们会读取现有信息并执行原生 doctor（入库 doctor 只开内存库）。`plan.ready` 只表示所需二进制的启动探针通过，不表示输入参考完整或未来下载一定完整。

### 操作阶段

| CLI | 动作 |
|---|---|
| `data fetch` | 按配置下载原生 bars；启用参考刷新时含 before/after |
| `data ingest` | 解析已提交参考/overlay，事务入库 |
| `data sync` | 可选 reference_before → fetch → reference_after 或 resolve → ingest → verify |
| `data verify` | 解析输入根后只读校验目标库；任务记录/报告仍会写入输出目录 |
| `data jobs` | 只读任务列表 |
| `data cancel` | 请求本地服务取消它拥有的任务 |

完整命令：

~~~powershell
# 下载：仅 bars，控制层自动从完整配置筛选四种原生数据集。
& ./.venv/Scripts/ptrade-local-backtest.exe data fetch --config ./ptrade_data_config.json --output-dir ../work/ptrade-data-demo/jobs --datasets "daily_bars,minute_bars,minute_bars_5m" --no-refresh-reference --json

# 已存在日历/证券/前收/hfq 等参考后，进行严格入库与验证。
& ./.venv/Scripts/ptrade-local-backtest.exe data ingest --config ./ptrade_data_config.json --output-dir ../work/ptrade-data-demo/jobs --no-refresh-reference --json
& ./.venv/Scripts/ptrade-local-backtest.exe data verify --config ./ptrade_data_config.json --output-dir ../work/ptrade-data-demo/jobs --json

# 完整同步，显式启用已配置的独立 SDK 参考阶段。
& ./.venv/Scripts/ptrade-local-backtest.exe data sync --config ./ptrade_data_config.json --output-dir ../work/ptrade-data-demo/jobs --refresh-reference --idempotency-key demo-sync-20261106 --json

& ./.venv/Scripts/ptrade-local-backtest.exe data jobs --output-dir ../work/ptrade-data-demo/jobs --json
~~~

CLI 前台等待该任务结束；Ctrl+C 请求取消并等待清理。只有 `--refresh-reference` 显式开启或配置 enabled=true 才运行 SDK 网络参考步骤。`--no-refresh-reference` 明确覆盖配置 true。

## 原生独立入口与离线自验证

可以单独运行原生工具，不需要 Python 执行下载/入库算法。Python 在下面只用于创建配置/合成输入与独立校验。

### 下载器：单日双证券微窗

从已读公开样例生成新工作配置，不使用样例中的本机绝对湖路径：

~~~powershell
& ./.venv/Scripts/python.exe -c "import json,pathlib; p=pathlib.Path('../work/ptrade-fetch-demo').resolve(); p.mkdir(parents=True,exist_ok=True); c=json.loads(pathlib.Path('native-fetch/examples/tdx.json').read_text(encoding='utf-8')); c['lake']=str(p/'lake'); c['start']=c['end']='2026-11-06'; (p/'fetch.json').write_text(json.dumps(c,indent=2),encoding='utf-8')"
& ./native-fetch/target/release/ptrade-fetch.exe plan --config ../work/ptrade-fetch-demo/fetch.json --report ../work/ptrade-fetch-demo/plan.json
& ./native-fetch/target/release/ptrade-fetch.exe fetch --config ../work/ptrade-fetch-demo/fetch.json --report ../work/ptrade-fetch-demo/fetch-report.json --progress ../work/ptrade-fetch-demo/fetch-progress.json
& ./.venv/Scripts/python.exe native-fetch/tests/verify_parquet.py ../work/ptrade-fetch-demo/fetch-report.json
~~~

同样的历史日期在以后运行可能超过 TDX 保留期；以当次 coverage 为准。原生直接配置只选 `daily_bars/index_bars/minute_bars/minute_bars_5m`；不要把完整控制层的十种数据集直接交给 fetch。

### 入库工具：完整合成输入

[benchmark.py](<../native-store/tests/benchmark.py>) 创建八种参考/行情数据集，执行原生冷/暖入库，再用 Python DuckDB 1.5.6 只读验证结果：

~~~powershell
& ./.venv/Scripts/python.exe native-store/tests/benchmark.py --exe ./native-store/target/release/ptrade-store.exe --codes 20 --days 5 --minute-days 2 --output ../work/ptrade-store-demo
& ./native-store/target/release/ptrade-store.exe plan --config ../work/ptrade-store-demo/config.json --report ../work/ptrade-store-demo/plan.json
& ./native-store/target/release/ptrade-store.exe verify --config ../work/ptrade-store-demo/config.json --report ../work/ptrade-store-demo/verify.json
~~~

预期：`engine_version=v1.5.6`、独立 Python 校验 true、暖入库插入/更新/变更文件计数均为 0。这里的输出路径由 `--output` 决定，不打开正式行情库；它不是 Python/Rust 入库速度对比，更不是端到端性能倍数。

直接原生调用的 `--report` / `--progress` 必须是互不相同的 `.json` 文件，不能覆盖输入配置；fetch 的输出文件还必须位于 lake 之外。配置错误会非零退出，不以报告写入覆盖湖内文件。上面的直接调用示例分别使用独立配置、报告与进度路径。

## 数据不变量

| 数据集 | 源频率/版本 | 量单位 | 入库角色 |
|---|---|---|---|
| `daily_bars` | 股票日线 v2 | TDX 日线原始手 ×100 后为股 | `ashare_1d_stock` |
| `index_bars` | 指数 1d v1 | 原值，未独立证实单位时 unknown | `ashare_1d_index` |
| `minute_bars` | 1m v1 | TDX 股票分钟已是股，不再 ×100 | 股票 1m；符合输入契约的指数分钟分流到指数表 |
| `minute_bars_5m` | 5m v1 | 股票原生股 | 独立湖/覆盖记录，不写入 1m 表 |
| `corporate_actions` | 参考输入 | 不适用 | SDK 派生因子的输入，不是额外本地核心行情表 |
| `instruments/trading_calendar/trading_status` | 参考 | 不适用 | 基础、日历与历史状态 |
| `adj_factors` | hfq | 独立因子，不对窗口再归一 | 日线 `adj_factor` |
| `index_constituents` | 时点/快照 | 按源明确含义 | 真实区间或显式 snapshot_date |

- 股价保存未复权 OHLC，金额为元；入库不把 OHLC 预先复权。
- 物理日期取文件内的真实 `trade_date/as_of_date`，不使用月分区目录覆盖日期。
- TDX 1m 是 240 根结束标签：09:31–11:30、13:01–15:00；5m 通常 48 根，各自独立保留期/水位。不生成 09:30 竞价或 13:00 padding。
- 回测保留 241 槽调度轴，缺少源 09:30 时该槽不是新增虚构行情。
- TDX 历史 K 线不提供可信历史 pre_close，fetch 写 null；store 优先用源前收，其次用窗口之前的可信湖/旧库 anchor 和 hfq 比值回推。
- 新日线无可靠前收或 hfq 因子严格失败回滚，不用当日 close、NULL 或中性因子 1 冒充成功。
- 历史 ST/risk_warning 与停牌按当日参考信息；未知为 NULL/unknown，不用最新名称全历史回填。
- 分别比较 bar/factor/status 修订时间，较新状态不能把较旧行情价格带回来。
- 只更新 staging 中实际存在的业务键，保留未出现的旧行和自定义旧列。没有按窗口 DELETE 或 drop 核心表；明确的非交易日事实仅能 tombstone 对应日历键。
- 数据、来源指纹与入库检查点在一次事务提交；失败/取消回滚。不会根据下载缺行/断网/partial 删除旧日期区间。

## CNE SDK 与版本边界

主 Python 与 Rust writer 固定 **DuckDB 1.5.6**：Rust crate `duckdb/libduckdb-sys=1.10506.0` 对应该引擎版本。现有 CNE SDK 使用它自己的环境（包括它原有 DuckDB 2.0 依赖），不把 SDK Python wheel 混装进回测环境，也不把 1.5.6 目标库交给 SDK 写。

SDK 的安装介质与 [lake.toml 配置方式](<../integrations/cne_lake/README.md>) 由现有 SDK 部署提供；本仓库不捆绑 SDK。可先核验所需接口：

~~~powershell
& ./.venv/Scripts/python.exe -c "import duckdb; print(duckdb.__version__); assert duckdb.__version__ == '1.5.6'"
& ../cne_lake/.venv/Scripts/python.exe -c "import cnequity.config, cnequity.storage.revisions, cnequity.derive.adj_factors; print('SDK interfaces import OK')"
~~~

### active revision 和 native overlay

- 没有 `meta/revisions/<dataset>/current.json` 的旧湖可读 legacy curated/derived；没有 SDK 时会明确报告未做 SDK schema 验证。
- 有 pointer 时只读已验证的 active generation，不退回废弃 mutable 历史；SDK 缺失/不兼容明确失败。
- fetch 发布的 shard 由 native manifest 逐文件验 SHA-256，作为 overlay 输入；它不是自动登记为 CNE generation。
- resolver 返回每种数据集的精确 `dataset_roots` 文件列表和 `native_files`。映射为空不隐式扫别的目录。
- 入库前再次核验文件未变化；不把 SDK-only 文件误标为 native，也不混入未列出的新 shard。

### 参考刷新

`reference_before` 仅选择已配置的参考步骤，禁止重复使用 SDK 下载 bars。控制层写临时 TOML，把 data.root 指到选定湖，把 SDK DuckDB 指到任务自己的工作库，不改原 TOML。

`reference_after` 当前仅支持 `derive_adj_factors`。它在独立 merged-view 中复制已验证 active 输入 + native overlay，调用 SDK hfq 派生，并验证新 bar 日期的因子覆盖与原输入未变。不会删除旧 generation、改原 current pointer，或假称在旧视图上 derive 已看到新行情。

## 数据看板与 HTTP 任务

从仓库根目录启动与选定数据配置同一进程的看板：

~~~powershell
$env:PT_DATA_CONFIG = (Resolve-Path ./ptrade_data_config.json).Path
$env:PTRADE_SIM_WEB_DIST = (Resolve-Path ./web/dist).Path
& ./.venv/Scripts/ptrade-local-backtest.exe dashboard --root ../work/ptrade-data-demo/jobs --host 127.0.0.1 --port 8765
~~~

打开 <http://127.0.0.1:8765/data>。页面允许修改日期/证券/数据集，不能通过任务 body 替换可执行文件、库路径或 shell 命令；路径由服务启动配置/CLI 决定。

在另一个 PowerShell 窗口执行：

~~~powershell
Invoke-RestMethod http://127.0.0.1:8765/api/data/status
$body = '{"overrides":{"start":"2026-11-06","end":"2026-11-06","symbols":["000001.SZ","600000.SH"],"datasets":["daily_bars","minute_bars","minute_bars_5m"]}}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/api/data/plan -ContentType application/json -Body $body

# 显式启动下载任务，不执行入库。
$start = '{"operation":"fetch","overrides":{"start":"2026-11-06","end":"2026-11-06","symbols":["000001.SZ","600000.SH"],"datasets":["daily_bars","minute_bars","minute_bars_5m"]}}'
$job = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/api/data/jobs -Headers @{"Idempotency-Key"="demo-fetch-20261106"} -ContentType application/json -Body $start
Invoke-RestMethod ("http://127.0.0.1:8765/api/data/jobs/" + $job.id)
Invoke-RestMethod -Method Post -Uri ("http://127.0.0.1:8765/api/data/jobs/" + $job.id + "/cancel") -ContentType application/json -Body '{}'
~~~

同一个 Idempotency-Key + 操作/有效配置返回同一个持久任务；不同配置不能复用同一个 key。HTTP 202 只表示已接收，不表示已下载/提交。浏览器写请求必须同源；本地 CLI 无 Origin 可调用。生产构建由 8765 同一 FastAPI 服务托管，避免把开发代理的读接口成功当成写接口同源验证成功。

CLI 的 `data cancel` 要请求实际拥有任务的本地服务；CLI 自己启动的前台任务用 Ctrl+C，而不是让另一进程按历史 PID 猜取消：

~~~powershell
& ./.venv/Scripts/ptrade-local-backtest.exe data cancel --job-id $job.id --server http://127.0.0.1:8765 --json
~~~

## 结果与验收

任务写入 `<output-dir>/.data_jobs/<id>/`：配置快照、job、阶段日志、阶段 report/progress、最终 report。查询不创建它们，显式任务创建才写入。进度是原子替换的单个 JSON，不是 JSONL。

| 状态 | 含义 |
|---|---|
| `queued/running` | 已排队/执行中，不是终态 |
| `succeeded` | 所执行阶段成功，仍需读取该阶段 coverage |
| `partial` | 输入覆盖/状态有未知或部分完成，不等于 succeeded |
| `failed` | 配置/依赖/数据/执行失败，errors 给出阶段 |
| `cancelled` | 拥有的任务取消/清理，通常退出 130 |

默认 `allow_partial=false`：控制层遇 partial 停止后续阶段并非零退出。显式设 true 才允许后续执行/退出 0，但最终状态仍保留 partial。100% 进度或退出 0 不能替代 coverage。

fetch 原始退出码：0 succeeded、2 配置、3 failed、4 partial、130 cancelled。store 的 partial 可为退出 0；控制层依据 JSON 状态统一判定。sync 跨下载/SDK/入库多个阶段，不是整个湖的单一事务；全局 partial 可能含已完成子任务 shard，store 非严格 unknown 的 partial 也可能已提交。控制层不会假称将此前成功阶段全部回滚。单次 store 的严格校验失败/取消则回滚其目标事务，不推进入库检查点；fetch 未完整子任务不推进成功水位。

重启后保留任务记录，但没有进程所有权的旧 running task 显示 unavailable/failed，不根据记录 PID 恢复或杀未知进程。

### 回归命令

~~~powershell
cargo test --release --locked --manifest-path native-fetch/Cargo.toml
cargo test --release --locked --manifest-path native-store/Cargo.toml
& ./.venv/Scripts/python.exe -m pytest tests/test_data_tools.py tests/test_data_jobs.py tests/test_data_reference.py tests/test_data_api.py -q
~~~

真实网络微窗、离线 TCP fixtures、事务与取消测试属于不同证据范围，具体结果见 [验收记录](<validation.md>)。正式大规模行情不需要重复下载/重新转换来验收这些模块。

## 既有每日入口部署

公开仓库提供 [部署说明](<../integrations/cne_lake/README.md>) 与 [完整 daily_update.py](<../integrations/cne_lake/daily_update.py>)。它不是安装时自动运行的 hook，也不注册/修改任何计划任务。

必须把模板部署到与仓库并列的现有 `cne_lake`，不能直接从 `integrations/cne_lake` 运行后假定相对路径已正确：

~~~text
workspace/
├── ptrade-local-backtest/
│   ├── .venv/Scripts/python.exe
│   └── ptrade_data_config.json
└── cne_lake/
    ├── daily_update.py
    ├── .venv/Scripts/python.exe
    ├── lake.toml
    └── run.py
~~~

部署到该布局后，从 workspace 目录执行只读计划：

~~~powershell
& ./ptrade-local-backtest/.venv/Scripts/python.exe ./cne_lake/daily_update.py --backend rust --config ./ptrade-local-backtest/ptrade_data_config.json --plan --no-reference-refresh
~~~

已准备参考输入时执行显式增量同步：

~~~powershell
& ./ptrade-local-backtest/.venv/Scripts/python.exe ./cne_lake/daily_update.py --backend rust --config ./ptrade-local-backtest/ptrade_data_config.json --start 2026-11-06 --end 2026-11-06 --symbols "000001.SZ,600000.SH" --output-dir ./work/daily-data-jobs --no-reference-refresh
~~~

模板默认 backend=rust；去掉 `--no-reference-refresh` 会显式开启 SDK 参考刷新（与主 CLI 默认 disabled 区别）。`--backend python` 保留既有 SDK + legacy `run.py` 路径，该旧算法与配置须由现有部署提供，仓库模板不冒充已经重写它们。
