# ptrade-fetch

**ptrade 本地回测框架 0.1.0** 的独立 Rust CLI，可执行名保留 `ptrade-fetch`。运行期只使用 Rust TCP、zlib、调度器、Arrow/Parquet，不启动 Python；参考资料、复权与入库由上层协调器保留或编排。

第一开发者/维护者 JonePoon，独立公开项目 [ptrade-local-backtest](https://github.com/JonePoon/ptrade-local-backtest)。完整来源与许可范围见 [第三方通知](<../THIRD_PARTY_NOTICES.md>)；[tdxpy NOTICE](<NOTICE.tdxpy>) 原样保留。新数据工具 ZIP 为 `ptrade-local-backtest-data-windows-x64.zip`，不改原生工具内部接口。

## 命令接口

在独立仓库根目录运行。先把下方完整配置保存为新工作目标 ../work/ptrade-local-fetch-demo/fetch.json；配置中的相对 lake 以配置文件目录解析，不直接使用历史示例中的本机绝对路径。

~~~powershell
cargo build --release --locked --manifest-path native-fetch/Cargo.toml
if ($LASTEXITCODE -ne 0) { throw "fetch build failed" }
& ./native-fetch/target/release/ptrade-fetch.exe doctor --report ../work/ptrade-local-fetch-demo/doctor.json
& ./native-fetch/target/release/ptrade-fetch.exe plan --config ../work/ptrade-local-fetch-demo/fetch.json --report ../work/ptrade-local-fetch-demo/plan.json
& ./native-fetch/target/release/ptrade-fetch.exe fetch --config ../work/ptrade-local-fetch-demo/fetch.json --report ../work/ptrade-local-fetch-demo/fetch-report.json --progress ../work/ptrade-local-fetch-demo/fetch-progress.json
~~~

- doctor 不需要 config，不访问或修改湖。输出版本、原生能力、支持数据集和协议预算。
- plan/fetch 必须提供 config。额外 JSON 键忽略，所有主要参数外置。
- stdout 只输出最终 report JSON。日志到 stderr。progress 是原子替换的单个 JSON 快照，不是 JSONL。
- 退出码：0=succeeded，2=配置/路径错误，3=failed，4=partial，130=cancelled。
- report 和 progress 必须是两个不同的 `.json` 路径，均在 lake 之外，不能覆盖 config。已有路径与尚未创建目录的规范化别名均检查；冲突在 plan/fetch 网络请求或发布之前返回配置错误。父目录可创建；不会静默覆盖湖中的既有 Parquet shard。

## 配置

~~~json
{
  "lake": "./lake",
  "start": "2026-11-05",
  "end": "2026-11-06",
  "symbols": ["000001.SZ", "600000.SH"],
  "datasets": ["daily_bars", "minute_bars", "minute_bars_5m"],
  "provider": "tdx",
  "tdx_hosts": [["120.76.1.198", 7709], ["1.202.143.37", 7709]],
  "concurrency": 2,
  "timeout_secs": 5,
  "retries": 2,
  "requests_per_second": 4,
  "max_pages": 40,
  "page_size": 800,
  "resume": true,
  "refresh": false,
  "capture_wire": false
}
~~~

配置日期是历史示例，后续执行时按当次源保留期与 coverage 解释，不假定任何旧日期永远可下载。start/end 是含端点日期。相对 lake 路径相对 config 文件所在目录解析；上层可以传绝对快照。
SS 后缀规范化为 SH。SH/SZ 主市场受支持；BJ 等未知路由明确失败。
tdx_hosts 支持 [host,port]、{host,port} 和 host:port 三种项形态。
resume 默认 true。refresh=true 在同一窗口产生新的 fetched_at 修订 shard，不改旧文件。
capture_wire=true 保留这次进程自己收到的原始请求/响应字节及 SHA256，供协议独立复核。

## 数据契约

| 数据集 | frequency | data_version | volume | 附加列 |
|---|---|---|---|---|
| daily_bars | 日线 | v2 | 股票日线原手乘100后股 | pre_close可空 |
| index_bars | 1d | v1 | 保留TDX原量，单位未证实 | pre_close可空、frequency |
| minute_bars | 1m | v1 | TDX原生股，不乘100 | bar_time、frequency |
| minute_bars_5m | 5m | v1 | TDX原生股，不乘100 | bar_time、frequency |

核心列：symbol UTF8、trade_date DATE32、open/high/low/close Float64、volume Int64、amount Float64、source UTF8、data_version UTF8、fetched_at timestamp[us,UTC]。
分钟 bar_time 为 timestamp[us,None]，值是 Asia/Shanghai naive 收盘时刻。
首1m bar是09:31，连续时段09:31..11:30、13:01..15:00；排除午休、13:00 padding，不虚造09:30。
一个分钟 dataset 只存一个频率；两个历史长度和水位不能混合。
TDX K线不发布历史 pre_close，原生下载器写 null，绝不拿最新报价回填历史。

## 输出与恢复

~~~text
<lake>/curated/<dataset>/trade_date=YYYY-MM/part-<uuid>.parquet
<lake>/.ptrade-fetch/manifest.json
<lake>/.ptrade-fetch/watermarks.json
<lake>/.ptrade-fetch/wire/<run-id>/...
~~~

shard 先临时写、flush/sync，再原子独占发布。清单保存 SHA256、真实 min/max 日期、rows、symbol、frequency、窗口和 coverage。
水位在全部该 job 的 shard 和清单成功发布后推进；失败或 partial 不冒充完整窗口。
resume 对清单关联的文件逐个核验路径与摘要。完成记录必须有非空分片列表、明确 coverage.partial=false、与请求窗口匹配的 scope ID，以及唯一、已登记的分片引用；损坏响亮失败，不从水位猜成功。
修订回补追加新 shard；同 key 的 fetched_at 最新版本由入库层选择，不重写既有月分区。

## JSON 输出

report 共用：schema_version=1、operation、status(succeeded|partial|failed|cancelled)、counts、coverage[]、errors[]、warnings[]、files[{dataset,path,sha256,rows,start,end}]、timings。
错误含 code/message/job，捕获未验证字段不被解释成服务器内部状态；不输出秘密 headers。
coverage 声明 requested/observed、分页终止原因、partial、保留期和合法时段计数；历史不存在与网络失败区别报告，不编造缺失数据。
progress：status、phase、completed、total、rows、files、requests、retries、in_flight、peak_in_flight。

## 依赖

Rust >=1.85；Cargo.lock 固定全部传递依赖。Arrow/Parquet=57.0.0、Tokio=1.48.0、flate2=1.1.5(rust_backend)，其余精确版本见 [Cargo.toml](<Cargo.toml>) 与 [Cargo.lock](<Cargo.lock>)。
Windows 使用现有 MSVC Rust 工具链。测试使用本机 Rust TCP fixture，不连接正式湖。

## 协议规格来源

协议字段/编解码规格移植自公开 CNE vendored tdxpy **0.2.7** MIT 客户端源码，原 **Copyright (c) 2022, tdxpy** 与许可保留于 [NOTICE.tdxpy](<NOTICE.tdxpy>)：
_wire/helper.py（有符号价格变长编码、压缩日期和数量负指数修正）、_wire/parser/base.py（16字节响应头，12/14偏移的压缩/解压长度）、setup_commands.py（三阶段握手）、std/get_security_bars.py/std/get_index_bars.py（38字节请求及指数额外字段）。
每条响应按 TCP 原有有序字节精确读取，压缩长度/输出长度/zlib校验/记录计数/尾部均核验。
未给出独立TDX CRC，因此只报告实际 zlib Adler 校验和长度事实，不假造校验字段。
TDX有限历史只是观察到的源覆盖，不能宣称任意远古窗口完整。数据表类型和单位来自domain/schemas.py、bars.py、minute_bars.py。

## 验证命令与历史来源记录

~~~powershell
cargo fmt --manifest-path native-fetch/Cargo.toml --all -- --check
cargo test --release --locked --manifest-path native-fetch/Cargo.toml --all-targets
cargo clippy --release --locked --manifest-path native-fetch/Cargo.toml --all-targets -- -D warnings
cargo build --release --locked --manifest-path native-fetch/Cargo.toml
& ./.venv/Scripts/python.exe native-fetch/tests/verify_parquet.py ../work/ptrade-local-fetch-demo/fetch-report.json
~~~

需要离线 wire 对照时，在上方配置显式设 capture_wire=true 后重新 fetch，随后用已配置的独立 CNE SDK 环境执行：

~~~powershell
& ../cne_lake/.venv/Scripts/python.exe native-fetch/tests/verify_wire.py ../work/ptrade-local-fetch-demo/fetch-report.json
~~~

下述用例数和网络微窗是来源快照 4f8d3cd 的**历史记录**，不是新独立发行 0.1.0 的重新执行声明。新版本实际结果见 [validation](<../docs/validation.md>)；离线 fixture、真实网络和页面 MOCK 各自记录。


Rust unit/CLI/path/TCP fixture 共36项：输入/报告路径冲突、尚未创建目录的路径别名、空/部分/错窗口/重复引用清单拒绝、长度/尾部/zlib校验、指数额外字段、压缩日期、14数量负指数、溢出、乱序、断线重连同page、max_pages、RPS真实时刻、2连接峰值、恢复零网络、月分片修订不覆旧、digest损坏、manifest失败回滚、partial不推进、大UTF8参考兼容。
Python验证脚本仅离线读Parquet或已捕获字节，不构成运行时依赖，也不负责下载。

真实原生TDX只读测试：000001.SZ与600000.SH，2026-11-06一天，6job/6shard/578行；1m各240、5m各48、日线各1，7.7700403秒，peak_in_flight=2。
两个标的日线与分钟/5m volume总和均完全相等；amount相对差小于约2.2e-8，为TDX数量字段的Float32传输量化。
PyArrow逐文件核验实际DATE/Int64/UTCus/naiveShanghai类型、真实日期、时段和SHA256；公开CNE SDK离线解析6个独立实际frame，578行OHLC/volume/amount逐值完全相等。
历史探针的实际数据和原始 wire 只落来源项目的独立工作目标，未写正式湖。

### 明确的边界

- 股票/基金与指数日線分别遵循已核验单位；未证实的指数分钟量不填入股单位分钟dataset。目前指数只通过index_bars日线入口，原生指数解码由独立协议fixture核验，未冒充股票实测结果。
- 无权威交易日历时不猜休市日。请求首/尾日期没有实际观测即partial，不发布部分前缀、不推进完整窗口水位；报告提供observed范围和session_counts。
- 参考信息、上市日、股票状态和复权保持由协调器/现湖处理。K线历史pre_close始终null。
- 原始成功frame保存准确方向和哈希；失败或未收到的网络字节不会被标记为已完整捕获。响应头前12字节保持opaque，不假解释服务器状态。


### CNE SDK committed revision 可见性

原生清单是.native-fetch自己的发布和恢复证据，不是CNE SDK revision登记。
SDK collect_parquet_root默认committed=True，经resolve_committed_root选择当前generation：
无meta/revisions/<dataset>/current.json的legacy湖直接glob mutable curated；已有pointer的湖只看immutable generation，新native shard不会自动进入该generation。
actual work微窗无pointer，SDK只读扫描确认daily_bars=2、minute_bars=480、minute_bars_5m=96。
已有 CNE revision 湖由协调器的 active resolver / verified native overlay 与独立 merged-view 参考适配处理；原生 fetch 自身不会改写 current pointer 或派生 hfq，不声称单写 mutable curated 已经更新因子。完整流程见 [数据链](<../docs/native_data_chain.md#cne-sdk-与版本边界>)。
