# 独立版本验收与历史可复核基准

[返回 README](<../README.md>) · [贡献检查](<../CONTRIBUTING.md>) · [发布/安装](<windows_release.md>)

[逐项验收 JSON](<acceptance.json>) 由 Lead 按实际收集结果更新，应以其中 release/date 区分版本身份；来源计数不自动成为 0.1.0 的新验收。来源 0.3.0 的完整历史快照保留在本页后文，[2026-11-08 Windows 原始基准 JSON](<benchmarks/windows-x64-20261108.json>) 仍按历史样本解释。

## 独立版本 0.1.0

产品：**ptrade 本地回测框架**。第一开发者/维护者：**JonePoon**。独立项目 [JonePoon/ptrade-local-backtest](https://github.com/JonePoon/ptrade-local-backtest) 以已验收来源快照 4f8d3cd 初始化；不是 ptrade-sim 的 GitHub fork 仓库，但实际复用基础实现，参见 [作者/来源](<../THIRD_PARTY_NOTICES.md>)。

### 已核对的文档与元数据

- 主包 ptrade-local-backtest 0.1.0，主 CLI 同名、短别名 ptrade-local，二者经公共 ptrade_local_backtest 门面；旧 ptrade-sim CLI 和 ptrade_sim 导入兼容。内部 _ptrade_sim_native 和原环境变量/共享队列保留。
- 可选索引发行 ptrade-local-backtest-native 0.1.0；原生工具仍名 ptrade-fetch / ptrade-store。
- 当前源码的 Hatch / 原生 ZIP 配置已列入作者/许可集合；配置核对不等于归档或安装已验证。
- 上游原 MIT 原样存档 1064 bytes，SHA-256 **1c6df15d98d43336bd3bd7117d2739a1e303468458d71848bb30c1a4cb1eac70**。
- tdxpy NOTICE 保持原样 1162 bytes，SHA-256 **b65ba06aec3e18cd1fad4b402a5ae74af64db175a7df7ae3ceb68ff62aae5456**。

### 新版本执行结果的登记

已收集的新版本结果分项如下，不相加为完整 suite 总数：

| 项目 | 已收集结果 | 范围 |
|---|---|---|
| 身份/许可/入口定向回归 | 26 项通过 | 新名称、版权文件、公共门面与入口相关子集，不是完整 Python suite |
| 离线本地回测 | Python / Rust 两后端各完成 5 日；实际产生交易和 Bar；0 socket.connect 尝试 | [离线回归](<../tests/test_local_offline.py>) 阻断 connect 后执行；与身份回归合跑的 6 项子集含重叠项，不能另加成新总数 |
| 三个 CLI 与公共门面 | 长名/短名/旧名均输出 ptrade-local-backtest 0.1.0；产品/维护者常量正确 | 主/短入口经公共门面，旧 ptrade_sim 导入保持兼容 |
| 新索引 wheel | 构建 exit 0；METADATA Name/Author 正确，原始 MIT 全文 SHA 一致；安装后 Rust backend available | 实际构建/安装的 ptrade-local-backtest-native，不用缺扩展 skip 代替 |
| 前端 Node 回归 | 22/22（19 原用例 +3 品牌回归） | Node 24.21.0，实际 pnpm 11.28.5；冻结离线安装 526 reused /0 downloaded |
| 前端检查与构建 | lint 0 error /0 warning；vue-tsc -b exit 0；Vite build exit 0 | Vite 8.2.2，3135 模块；HTML 0.70KB /CSS 39.74KB /JS 840.92KB，gzip 285.68KB；截图用新构建，不推断全部发行资产已打包 |

完整 Python + coverage、完整工具/归档/隔离安装与远端 workflow 在文档冻结时仍按各自实际结果登记，Lead 后续在 [acceptance](<acceptance.json>) 更新；不从定向子集、构建配置或来源版本计数推断整版已通过/已上传。

### 新品牌浏览器与图像（2026-11-09）

七张图经实际 Chromium / agent-browser 采集，保留原像素，文件 bytes/SHA-256 已再次核对。只使用新品牌 local-* 图展示当前项目；收益、成交、库/湖与留存任务仍来自独立合成夹具。

| 场景 | 观察结果 | 证据范围 |
|---|---|---|
| [列表](<images/local-dashboard.png>) | 1440×1000；3 done +1 interrupted | done 各 194 日期/39 笔，interrupted 95 日期/19 笔；合成 CSV，不是行情业绩 |
| [详情](<images/local-detail.png>) | 1440×2054；曲线、指标、成交与日志 | 合成 run 产物与冻结源码 |
| [3.11 导出](<images/local-export.png>) | 1440×1000；ready；8321B 单文件实际 Chrome 下载 | 真实静态转换合成策略，不是券商执行 |
| [3.5 阻塞](<images/local-export-blocked.png>) | 1440×1000；source_syntax/JoinedStr；复制和 .py 禁用，报告可实际下载，API 422 | 真实静态诊断，不是旧解释器运行 |
| [数据工作台](<images/local-data.png>) | 1440×2307；真实 fetch 0.1.0 / store v1.5.6 doctor，available=true；控制层只读 plan ready=true | 冻结原生组件真实启动；40 日线/2 标的/20 日历/1 Parquet 和 3 个留存任务仍是 fixture，不是完整回测契约库或真实同步 |
| [手机首页](<images/local-mobile.png>) | 390×844 截图 | Chromium CSS 视口实测 320px/390px，document.scrollWidth 等于视口，无全页横向溢出；不是物理手机测试 |
| [手机数据](<images/local-data-mobile.png>) | 390×3480 全页截图，CSS 视口 390×844 | 隐藏路径默认勾选；相同 320px/390px 宽度检查，fixture 边界保持可见 |

**任务交互单独使用受控 HTTP MOCK**：fixture 的非 MOCK start 返回 409 保护；实际 UI start 收到 202 queued，受控状态转 running；实际 UI cancel 收到 200 cancelled。运行时启动禁用/取消启用，取消后反转，Chrome errors=[]。写请求上下文已确认都到 controlled-http-fixture，未启动原生 worker。这个 UI 生命周期观察不表示真实原生下载/事务入库成功，也不把三份留存任务的 succeeded 当作新执行结果。合成数据库前后 SHA 一致；UI 所有自有后台作业已收集，fixture 服务与浏览器在收尾后关闭。

~~~text
local-dashboard.png       f6a6ea6628443f27ff08bbfb36baf04ff07cd0788e76352c81f813b904da9229
local-detail.png          af7de30db0c6167534403da6f2f312d21bb13f21bc372739a9c9d7b09db2c2f3
local-export.png          51d44eac9429093bb1e9c9473e504462ab30c93f9a1d8f82987c4a0a0ed17b6d
local-export-blocked.png  b5a578f1b9f91c68e9149111916648610560142873efa436d63eb5bad3eaaef8
local-data.png            0ba090a9590dab1619df46d30a00305274be922af2ca1d419c36b288a08ecba3
local-mobile.png          6d10ee50c79d4620d8e169f620c2b99197d87685b4626afa9a6161b007d6ce02
local-data-mobile.png     8a7ecaad5fe77040ac0ea10a5ba1947dbea7411d707eb9f2a8f0aca08e38acb9
~~~

### 原生组件来源与构建状态

本次用于数据页探针和新工具分发的两个可执行文件从此前冻结的 0.3.0 原生 ZIP 提取；整体归档与内层 manifest 摘要经过核对，相关 **25 个 Rust 源码文件字节一致**。不使用后来变化的旧工作目录 target 二进制，也不把提取重新描述为“在新项目重新编译全部 Rust 工具”。原生工具自身版本仍为 0.1.0，来源归属随新分发保留。

新独立索引 wheel 的首次链接曾返回 LNK1104 临时文件路径失败；使用独立工作目录 TEMP/TMP 重试后实际构建 exit 0，不修改 OS 安全策略。Lead 已核对 METADATA 的 Name=ptrade-local-backtest-native / Author=JonePoon、原始 MIT 全文 SHA，并安装到新项目环境后确认真实 Rust backend available。完整分发/隔离安装检查仍按最终 receipt 另记。


### 性能解释

来源 2026-11-08 JSON 的 Python/Rust 引擎阶段中位数为 **4.7874791999929585 / 2.5811324999958742 s**，比值 **1.8547979230049643×（约 1.85×）**；200 股票 ×20 日，964000 Bar /100 笔成交，结果散列一致。计时包含引擎构造、读取、策略和 run IO，不含数据库生成/最终 pipeline 汇总。

这是两个本地后端的历史同工作量比较，**不是与 PTrade 云端的比较**。项目没有云端速度实测；更早 1.83× 的独立原始样本在后文原样保留，不混合样本或推广为全策略/端到端倍数。

## 来源快照 0.3.0 的历史验收（2026-11-08）

下面保留来源项目当时的已收集结果、原始摘要、截图与样本。历史截图仍按旧品牌保存；新品牌界面只引用 local-* 图，不拿旧图冒充 0.1.0 页面。

## 记录规则

这里记录实际收集的本地结果，不把配置存在、预期命令、安装过 wheel 或远端尚未结束的 job 写成成功。本节历史来源源码版本 0.3.0，原生组件各 0.1.0；本地与 GitHub Actions 的结果分开。

正式行情与私有配置不是测试夹具。常规 Python 回归使用合成 DuckDB；原生下载测试使用本地 TCP fixtures/独立微窗；原生入库使用独立合成 lake/目标库。没有为验证原生组件重新转换多年历史数据或修改计划任务。

## 来源版本本地回归快照（2026-11-08）

| 项目 | 已收集结果 | 范围 |
|---|---|---|
| 主 Python | 1031 passed / 7 skipped / 1 third-party warning；43.08 s | Python 3.12.12，DuckDB 1.5.6，全套 pytest + coverage |
| 组合行/分支覆盖率 | 85.84%（精确 85.84032240496678%） | 全局门槛 70%，23 个登记模块均过各自门槛 |
| Ruff lint / format | exit 0 | src、tests、scripts |
| Mypy | exit 0，24 个源码文件 | 检查目标 3.12，与运行下限分开 |
| Rust 索引 | 12 项 Rust 单测通过 | release locked 默认特征，真实 Python 链接 |
| 实际 release DLL 索引对照 | 53 passed / 0 skipped；0.57 s | 新构建 DLL 直接加载后的 Python/原生子集复验，不额外加进主 Python 总数 |
| Rust fetch | 36 passed / 0 skipped | release locked 全 targets；fmt/clippy/release build exit 0 |
| Rust store | 32 lib + 1 bin =33 项通过 | release locked，严格 clippy/build exit 0 |
| Rust 合计 | 12 +36 +33 =81 项通过 | 三个独立 crate，不计入 Python 根套件 |
| 真实原生 CLI probes | 12 项，exit 0 | 真实进程与合成输入，不是页面 MOCK |
| 入库/版本互操作 | 成功，exit 0 | 八数据集合成冷/暖入库，独立 Python 1.5.6 校验，独立 SDK 2.0 Arrow 只读互读 |
| 发布/安装与每日模板定向回归 | 22 项通过 | 包/installer 17，每日部署模板 5 |
| 前端 Node tests | 19/19 | Node 24.21.0，dashboard.test.mjs |
| 前端 eslint / Vue TS / Vite | exit 0 | TS app/node 两项目，Vite 8.2.2 生产构建 |

7 个 Python skip 都来自 Windows 当前令牌不具备真实符号链接创建权限；不能改写成“无跳过”。其它硬链接/无特权路径边界已按测试执行。第三方 warning 与本项目失败是不同结果，原始绝对私有路径日志不放进公开仓库。

本节作为来源历史保留，不随独立版本新增用例改写原计数和历史样本。新结果登记在本页独立版本章节。完整 suite、本地原生/前端/安装与远端 CI 要分别核验，不从一个通过状态推断全部链路完成。

### Python 复现命令

从仓库根目录、已安装开发依赖的独立环境执行：

~~~powershell
& ./.venv/Scripts/python.exe -m ruff check src tests scripts
& ./.venv/Scripts/python.exe -m ruff format --check src tests scripts
& ./.venv/Scripts/python.exe -m mypy
& ./.venv/Scripts/python.exe -m pytest --cov=ptrade_sim --cov-report=term --cov-report=json:coverage.json --junitxml=pytest.xml
& ./.venv/Scripts/python.exe scripts/check_coverage.py --json coverage.json
~~~

模块覆盖率登记与失败判定以 [check_coverage.py](<../scripts/check_coverage.py>) 和 [元测试](<../tests/test_coverage_floors.py>) 为准；没有降低已有门槛以通过。

### 原生复现命令

~~~powershell
$env:PYO3_PYTHON = (Resolve-Path ./.venv/Scripts/python.exe).Path
$env:Path = (& ./.venv/Scripts/python.exe -c "import sys; print(sys.base_prefix)") + ";" + $env:Path
foreach ($crate in @("native", "native-fetch", "native-store")) {
    cargo fmt --manifest-path "$crate/Cargo.toml" --all -- --check
    if ($LASTEXITCODE -ne 0) { throw "fmt failed: $crate" }
    cargo test --release --locked --all-targets --manifest-path "$crate/Cargo.toml"
    if ($LASTEXITCODE -ne 0) { throw "test failed: $crate" }
    cargo clippy --release --locked --all-targets --manifest-path "$crate/Cargo.toml" -- -D warnings
    if ($LASTEXITCODE -ne 0) { throw "clippy failed: $crate" }
}
& ./scripts/build_native_data.ps1
& ./native-fetch/target/release/ptrade-fetch.exe doctor
& ./native-store/target/release/ptrade-store.exe doctor
~~~

实际原生 wheel 安装/DLL 对照不能被缺扩展 skip 替代。direct report/progress 要独立 JSON、不能覆盖配置，fetch 输出在 lake 外，见 [数据链](<native_data_chain.md>)。

### 前端复现命令

~~~powershell
pnpm --dir web run test
pnpm --dir web run lint
pnpm --dir web run build
~~~

已使用 agent-browser 0.41.2 与本机 Playwright Chromium 1228（实际 Chrome/149.0.7827.55）验证页面通道；系统 Chrome 156 的 CDP 启动未成功，该失败独立保留，不冒充该浏览器已通过。截图与功能验证使用成功的 Chromium 通道。

### 真实浏览器图文与 MOCK 范围

| 图/场景 | 已观察 | 输入/范围 |
|---|---|---|
| [桌面列表](<images/resume-dashboard.png>) | 1440×1000，4 run，3 done +1 interrupted | 合成 run 产物，不是策略业绩 |
| [详情](<images/resume-detail.png>) | 1440×1962，资金/回撤/月度，194 日期/39 笔 | 合成曲线/成交 |
| [3.11 导出](<images/resume-export.png>) | ready，真实 .py 8321B / 报告 1629B 下载完成 | 真实后端静态转换合成源码 |
| [3.5 阻塞](<images/resume-export-blocked.png>) | JoinedStr/source_syntax；.py/复制禁用，API 422；报告 1405B 可下载 | 真实静态诊断，不是旧解释器执行 |
| [数据页](<images/resume-data.png>) | 1440×2211，全页顶部完整；真实 fetch 0.1.0 / store v1.5.6 doctor、只读计划 | 合成库存 40/2/20 行与持久 partial 样本；不是完整回测契约库/真实同步 |
| [手机列表](<images/resume-mobile.png>) | 390×1611，完整 4 run 列表，横向无溢出 | 合成 run |
| [手机数据](<images/resume-mobile-data.png>) | 390×844，只读数据入口，横向无溢出 | 显式隐藏本机路径，仅展示，不改 API |
| 任务交互 | start → queued/running/cancelled/succeeded | 单独明确的 MOCK 后端，不混入上面的原生数据截图或原生进程验收 |

截图均经 read_image 核对，不含正式行情、账号或完整私有路径；数据页选了显式路径隐藏。湖/库摘要在只读 UI 验证前后未变化。

### 来源版本原生 release 字节快照

三个 crate 的 fmt、release locked 测试、strict clippy 与 release build 均退出 0。doctor 不联网、不写湖：fetch 报 native Rust、python_subprocess=false、network_contacted=false、writes_lake=false；store 实测 engine_version=v1.5.6、ok=true、files=[]、未提交/回滚任何目标事务。

以下是本次本地编译组件摘要，不是整个 ZIP/wheel 的摘要；分发归档摘要由其 Release/manifest/receipt 给出。engine-probe 是验收探针，不在双工具 ZIP 内。

~~~json
{
  "components": [
    {"component": "ptrade-fetch", "logical_path": "bin/ptrade-fetch.exe", "sha256": "cc00ba44a3fd5de4efed43d72a7fe3a153b39626b20727fb0efba39352dac1f7"},
    {"component": "ptrade-store", "logical_path": "bin/ptrade-store.exe", "sha256": "a68f855c160855abfecc84e58c5e24d2c9e96b41dadfb1a56b55a47d99230522"},
    {"component": "PyO3 release DLL", "bytes": 327168, "sha256": "be482ee272126d71d67e0c5ce370ab3c3dfda3c3974ada2aa3c2db0e43db8236"},
    {"component": "engine-probe", "role": "test_only", "sha256": "d5a05340fa0931f3cc8e7e6c558e1e5d4bc279dbf79a766aa458e031d5deaf29"}
  ]
}
~~~

### 文档命令实际核验

本次另用公开配置与 --check（不写生产代码/行情）执行：示例 target3.11 返回 0、ready=true/static_only；target3.5 返回 4/source_syntax；Windows PowerShell5.1 正确转义的 JSON 参数返回 0，top_n=2/watch_days=10。未转义双引号的旧写法实际返回配置码 2，文档已改为明确的 5.1 转义及跨版本参数文件。完整买入持有示例的 3.5 目标 AST/编译检查退出 0；这是静态语法检查，不是券商运行。

### fresh 合成数据链复验

最新独立 happy-chain 使用明确合成 facts，退出 0、各阶段 succeeded：cold 输入含 2 日线/480 分钟/2 指数行；Rust 索引回测得到 2 笔成交、10 行历史、0 data_errors；重复 ingest 的 inserted/updated/files_changed 均为 0。原始 fixture、Parquet 与 current pointer 摘要不变。fetch 是已核验 resume，requests=0，不称为真实网络下载；新真实双证券微窗网络测试由单独记录区分。

## CI 配置与远端结果

[主 CI](<../.github/workflows/ci.yml>) 配置 lint/format、Mypy、前端 eslint/vue-tsc/Vite、Python 3.10–3.13、覆盖率和 wheel/sdist 前端可服务性。

[原生 CI](<../.github/workflows/native-data.yml>) 配置 Linux/Windows 三 crate fmt、locked test、strict clippy、release 构建、真实 abi3 wheel 安装、平台 ZIP 和安装原生后的 Python 回归。

以上是仓库配置范围；当前本地结果不是所有操作系统/所有解释器矩阵已通过的声明。独立版本发布后，以独立仓库的实际 workflow 运行结果核验远端，不能用来源仓库 CI 替代。

## 历史有效工作量基准

这组记录来自此前保存的有效工作量测量，仍是历史样本，不是本轮为整个数据链重新测出的加速倍数。保存的原始 JSON 文件 SHA-256：

~~~text
e55c4bf8688edd32aa2d920da8eec7a552734cd77d2c8ba4f9e5b63a17847812
~~~

原文件使用 UTF-8/CRLF；下面按 Markdown UTF-8/LF 完整复录相同 JSON 字段/样本。把代码块内容保存为 UTF-8、CRLF 并保留末尾 CRLF 可还原上面的源文件摘要；保存为 UTF-8/LF 且保留末尾 LF 的复录摘要为：

~~~text
cf51986c1beac1446e158dbc5f2b35614e77dc16a585e7a892c88c42209c0aeb
~~~

两种换行的实际字节摘要已经分别核对，记录不含私有文件路径。

### 条件与计时

- Windows 11、Python 3.12.12、NumPy 2.5.4、DuckDB 1.5.6、Rust 索引 0.1.0，线程 2。
- 索引：1000 个证券、228000 个含缺槽/重复槽的行，100000 次标量/范围查询、241000 个批量查询结果，3 次样本，先预热。
- 引擎：200 证券 ×20 交易日，3 次交替两后端；964000 Bar /100 笔成交，daily/trades SHA-256 和最终资产一致。
- 计时包含引擎构造、数据读、策略、进度/CSV/导出快照 IO；**不含数据库生成与最终 pipeline 指标汇总**。
- 两后端共同关闭普通日志，数据库构造不计时；非零工作量通过断言，空回测不被算作性能结果。

| 项目 | Python median | Rust median | Python/Rust |
|---|---:|---:|---:|
| 精确索引 | 0.2818656000 s | 0.0205611000 s | 13.7087× |
| 范围索引 | 0.2408396000 s | 0.0327175000 s | 7.3612× |
| 批量索引 | 0.5361079000 s | 0.0188484000 s | 28.4432× |
| 引擎阶段 | 4.7617044000 s | 2.6018836000 s | 1.8301× |

该引擎阶段耗时减少约 45.4%。索引构建也有复制/分配成本（原始记录保留 build_seconds 与 nbytes），不以查询倍数代替全流程或跨机器性能。

### 复现

[benchmark_acceleration.py](<../scripts/benchmark_acceleration.py>) 默认用临时合成契约库，不连接/修改正式行情：

~~~powershell
& ./.venv/Scripts/python.exe scripts/benchmark_acceleration.py --codes 1000 --queries 100000 --repeats 3 --engine-codes 200 --engine-days 20 --threads 2 --output ../work/ptrade-benchmark/benchmark.json
~~~

不同 CPU、磁盘、缓存、Python/依赖版本会改变时间；只比较同一次环境、同一数据和策略，并保留原始样本/结果散列。下载、SDK、入库和最终汇总未被这组计时覆盖，不做其端到端加速声明。

### 完整原始记录

~~~json
{
  "environment": {
    "platform": "Windows-11-10.0.26300-SP0",
    "python": "3.12.12 (main, Nov 19 2025, 22:43:30) [MSC v.1944 64 bit (AMD64)]",
    "numpy": "2.5.4",
    "duckdb": "1.5.6",
    "native": {
      "requested": "rust",
      "effective": "rust",
      "available": true,
      "version": "0.1.0",
      "reason": "Rust 原生分钟索引（API 1）",
      "state": "available",
      "module": "_ptrade_sim_native"
    },
    "threads": 2
  },
  "index": {
    "codes": 1000,
    "rows": 228000,
    "queries": 100000,
    "batch_queries": 241000,
    "repeats": 3,
    "equivalent": true,
    "workloads": {
      "python": {
        "scalar": {
          "seconds": [
            0.28370129999530036,
            0.2818656000017654,
            0.24442170000111219
          ],
          "median_seconds": 0.2818656000017654,
          "checksum": 10540674338
        },
        "ranges": {
          "seconds": [
            0.23969400000351015,
            0.2420873999944888,
            0.24083959999552462
          ],
          "median_seconds": 0.24083959999552462,
          "checksum": 11282592657
        },
        "batch": {
          "seconds": [
            0.5361078999994788,
            0.5366580999980215,
            0.5281953000085196
          ],
          "median_seconds": 0.5361078999994788,
          "checksum": 25877984000
        },
        "build_seconds": 2.1599989850074053e-05,
        "nbytes": 11988136
      },
      "rust": {
        "scalar": {
          "seconds": [
            0.02056109999830369,
            0.016984799993224442,
            0.02458430000115186
          ],
          "median_seconds": 0.02056109999830369,
          "checksum": 10540674338
        },
        "ranges": {
          "seconds": [
            0.028144600000814535,
            0.032717499998398125,
            0.03336630000558216
          ],
          "median_seconds": 0.032717499998398125,
          "checksum": 11282592657
        },
        "batch": {
          "seconds": [
            0.018585199999506585,
            0.018848400010028854,
            0.019970099994679913
          ],
          "median_seconds": 0.018848400010028854,
          "checksum": 25877984000
        },
        "build_seconds": 0.0008980000129668042,
        "nbytes": 12974800
      }
    },
    "speedup": {
      "scalar": 13.708682902423488,
      "ranges": 7.361185909904984,
      "batch": 28.443151658189905
    }
  },
  "engine": {
    "dataset": "synthetic_contract_database",
    "codes": 200,
    "days": 20,
    "repeats": 3,
    "seconds": {
      "python": [
        4.958983000004082,
        4.761704399992595,
        4.735666099993978
      ],
      "rust": [
        2.528082499993616,
        2.60188359998574,
        2.7542479999974603
      ]
    },
    "median_seconds": {
      "python": 4.761704399992595,
      "rust": 2.60188359998574
    },
    "speedup": 1.8300989329494572,
    "equivalent": true,
    "result": {
      "daily_sha256": "a580e3ab663bc705477040f14fdfbc60a16aacad9bfcef3da821a61524a9af30",
      "trades_sha256": "2f84d772791bbd13d50a6b5a841acb5cad5bafa3011eae4cf7cdfe6f42d88232",
      "trade_count": 100,
      "final_value": 999494.4739949999,
      "bar_checksum": 10707880.669999465,
      "bar_count": 964000
    },
    "timing_scope": "engine construction + data reads + strategy run + progress/CSV/export snapshots; excludes DB creation and final pipeline metrics"
  }
}
~~~

## 入库验收不是 Python/Rust 速度比

[入库自验证脚本](<../native-store/tests/benchmark.py>) 的冷/暖样本衡量原生写库与同输入重复执行，Python只负责合成数据和只读独立结果核验。暖执行零更新/零插入是幂等断言，不是用 Python 作为另一入库算法进行倍数比较。

~~~powershell
& ./.venv/Scripts/python.exe native-store/tests/benchmark.py --exe ./native-store/target/release/ptrade-store.exe --codes 20 --days 5 --minute-days 2 --output ../work/ptrade-store-validation
~~~

来源日期、单位、因子、前收、状态、重复输入、旧列和事务失败比“跑完”更重要；严格失败/取消不能提交数据或推进检查点。网络微窗与本地 TCP fixture、SDK adapter 与原生解码、静态转换与券商运行也分别保持证据边界。
