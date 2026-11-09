# ptrade-store

**ptrade 本地回测框架 0.1.0** 的独立 Rust CLI，可执行名保留 `ptrade-store`。DuckDB C API 引擎固定 **1.5.6**；不通过 Python 写库，也不打开 SDK 2.0 的源数据库。

第一开发者/维护者 JonePoon，独立公开项目 [ptrade-local-backtest](https://github.com/JonePoon/ptrade-local-backtest)。来源和许可范围见 [AUTHORS](<../AUTHORS.md>)、[第三方通知](<../THIRD_PARTY_NOTICES.md>)、[根 MIT](<../LICENSE>) 与 [上游原 MIT](<../LICENSES/ptrade-sim-MIT.txt>)；工具 ZIP 分发保留完整作者/许可集合。

## 文件

~~~text
Cargo.toml / Cargo.lock      精确 duckdb =1.10506.0；独立 bundled parquet/json 构建
src/main.rs                 CLI、参数文件、报告输出和 Ctrl+C
src/lib.rs                  单写者事务、doctor/plan/ingest/verify 编排
src/config.rs               显式配置、日期/证券代码、数据根和清单
src/source.rs               Parquet 发现、SHA256、物理日期与来源字段
src/stage.rs                去重、前收/复权、历史状态、分钟分流
src/apply.rs                业务键 UPDATE/INSERT 与原子来源检查点
src/validate.rs             数值、OHLC、单位、日期、键和覆盖验证
src/tables.rs               PTrade 核心表与不破坏旧列的增量建表
src/progress.rs             原子 JSON 进度与报告文件
src/model.rs                结构化错误、结果、取消状态
src/tests.rs                Rust 合成库回归测试
src/bin/engine_probe.rs     独立 C API 引擎/互读小库探针
 tests/compat_probe.py       Python1.5.6/Rust1.5.6+SDK2只读 Arrow 互操作测试
 tests/benchmark.py          冷暖入库基准及独立 Python 结果验证
~~~

## CLI

~~~text
ptrade-store doctor
ptrade-store plan   --config job.json --report plan.json --progress progress.json
ptrade-store ingest --config job.json --report ingest.json --progress progress.json
ptrade-store verify --config job.json --report verify.json --progress progress.json
~~~

无 config 的 doctor 仅在内存连接读取真实引擎版本/能力，不创建库或扫描湖。
plan 使用已有目标只读连接或内存连接，不创建目标；verify 只读目标。
ingest 一次 BEGIN/COMMIT，所有目标数据与自有来源检查点一起提交；失败明确 ROLLBACK。

## 配置

下例对应后面 benchmark 命令生成的八数据集合成输入（20 证券、5 日、最后 2 日分钟线），路径按仓库根目录解析。benchmark 会生成完整 config.json 与实际输入，不需要填写虚构文件摘要。

~~~json
{
  "lake": "../work/ptrade-local-store-validation/lake",
  "db": "../work/ptrade-local-store-validation/target.duckdb",
  "start": "2025-01-02",
  "end": "2025-01-08",
  "symbols": [],
  "datasets": ["daily_bars", "index_bars", "instruments", "trading_calendar", "minute_bars", "adj_factors", "index_constituents", "trading_status"],
  "threads": 2,
  "memory_limit": "1GB",
  "require_volume_v2": true,
  "unit_fault_factor": 2.0,
  "strict_unknowns": false
}
~~~

未知额外字段忽略。日期接受 YYYYMMDD / YYYY-MM-DD。证券代码归一 SH→SS。
未给 datasets 时默认上述八个数据集。threads 默认2，memory_limit 默认1GB，volume v2 默认严格。
参数中的路径按调用进程工作目录解析；控制层宜传绝对路径。

存在 dataset_roots 的 dataset 只读这些目录/Parquet 文件；明确空数组也不回退 mutable。
未映射的 dataset 才扫描 curated/derived。SDK current generation 和 SHA 验证 native overlay 应由调用方的只读 resolver 给定。
dataset_roots 可混合 SDK active 单文件与 native overlay。native_files 仅描述 native overlays（SDK-only dataset 可为空列表），不要求 SDK 文件误标 native；匹配 native 条目的实际 SHA 必须相等，全部文件提交前再次检查不变。
不混入已废弃的 mutable generation，也不声明 SDK after-derive 在旧视图上已完成更新。

## 数据语义

- 真实日期来自文件内 trade_date/as_of_date，不采用 trade_date=YYYY-MM 的 Hive 月目录。
- fetched_at UTC 微秒最新值去重；同一时间按固定文件路径与行号稳定决胜。
- daily_bars v2 成交量为股，不乘100。关闭 require_volume_v2 后，仅已知 v1 TDX/Sina 库值按手→股转换；未知 v1 来源拒绝。
- minute_bars v1 为原生股，不复用日线单位转换；仅 1m 入 1m 表。minute_bars_5m 独立读取/覆盖报告，不写入 1m。
- index_bars 仅1d。指数分钟由 minute_bars 中的指数证券写入独立 ashare_1m_index。指数原成交量单位未独立证实，原值保留并报告 unknown，不乘100。
- 240 根结束时间为09:31起、13:01起；不虚造09:30、不生成13:00午休 padding。
- 日线 pre_close 源值优先；其后用窗口之前的湖历史/旧库前一行及 hfq 因子比值回推。
- hfq 因子按 symbol/trade_date/adjust_type=hfq 对齐；不是按窗口归一，也不是 qfq 倒数。
- 新日线缺有效 preclose 或 hfq 因子时严格失败回滚。没有 anchor 不以当日 close 或 neutral factor=1 冒充；有效旧 key 信息保留。
- 历史 ST 用当天 risk_warning，null 表示未知而非False。suspended 与 ST 可同时存在。不会用最新 instruments.name 重写历史 name/ST。
- 独立 bar/factor/status 来源时钟避免较新状态更新带入较旧 bar 价格。
- 指数成分 as_of_date 保持 snapshot_date；不伪造完整历史成员区间。旧权威区间不被 snapshot 清空。

核心表不 drop，不按日期窗口 DELETE，不做全历史 ST UPDATE。
只更新实际 staging 主键、插入缺失主键，保留未知自定义旧列。
交易日历唯一删除是有明确 is_trading=false 源事实且版本通过的单个 calendar key tombstone；不会删除行情日历史。

## 报告

stdout 只输出最终 JSON；--report 原子写同一结果，--progress 原子更新一个 JSON 快照（不是 JSONL）。

~~~text
schema_version=1
operation=doctor|plan|ingest|verify
status=succeeded|partial|failed|cancelled
engine_version=v1.5.6
counts / coverage / errors / warnings / files
before / after / tables / duplicates / unknowns / validation
transaction={committed,rolled_back}
started_at / finished_at / timings
~~~

退出0：成功或携明确 unknown/coverage 的 partial。控制层可按 allow_partial 拒绝 partial。
退出2：配置；3：数据/schema/required缺失；6：引擎不兼容；130：取消；1：IO/其它 DuckDB 失败。
required 输入缺失、数值/单位/键/前收/因子校验失败绝不 commit。

## 编译与测试

工作目录为独立仓库根目录；Rust stable ≥1.85.1，下面使用 Python 3.12 虚拟环境作合成数据与独立校验。Windows 需 MSVC C++ 构建工具，DuckDB bundled 首次编译 C++，耗时取决于机器和缓存，不承诺固定分钟数。

~~~powershell
cargo build --release --locked --manifest-path native-store/Cargo.toml --bins --jobs 2
cargo test --release --locked --manifest-path native-store/Cargo.toml --all-targets --jobs 2 -- --test-threads=2
cargo fmt --manifest-path native-store/Cargo.toml --all -- --check
cargo clippy --release --locked --manifest-path native-store/Cargo.toml --all-targets -- -D warnings
& ./native-store/target/release/ptrade-store.exe doctor
& ./.venv/Scripts/python.exe native-store/tests/compat_probe.py --exe ./native-store/target/release/engine-probe.exe
& ./.venv/Scripts/python.exe native-store/tests/benchmark.py --exe ./native-store/target/release/ptrade-store.exe --codes 20 --days 5 --minute-days 2 --output ../work/ptrade-local-store-validation
~~~

互读测试只在 native-store/work 中创建测试库；测试自己的旧格式小库先复制，原件不传入原生写操作。安装好独立 SDK 环境后，可给互读探针追加 `--sdk-python ../cne_lake/.venv/Scripts/python.exe`，没有 SDK 则只核验 Python 1.5.6 / Rust 1.5.6。
SDK 2.0 边界探针采用只读 Arrow IPC 交换；Python 测试驱动写自己生成的 Parquet，生产 Rust 路径不调用 Python。来源历史探针的环境限制与新版本的实际结果分开记录，见 [validation](<../docs/validation.md>)。

成功判定：doctor 实际报告 engine_version=v1.5.6；Rust 检查/测试退出 0；合成冷/暖入库与独立只读结果核验一致，暖入库 rows_inserted/rows_updated/files_changed=0。该冷/暖样本不是 Python/Rust 入库算法倍数，也不是 PTrade 云端速度对照。

## 内存和复杂度

Parquet 按所选 roots 读取，双 SHA 校验 O(文件字节数)；排序/去重 O(N log N)，边界 lag O(N)。
短窗口保留其之前所选证券的历史以寻找正确 anchor，不截断 lag 后冒用首日 close。
文件指纹和键级 revision 检查点在同一事务维护；重复输入不会重复插入/更新。
仍会读取相关已知历史用于数值验证、覆盖和边界，不声称所有未变 Parquet 都无需 IO。
DuckDB 内存/线程预算由配置控制，超预算或 SQL 失败不会隐藏成空结果。
