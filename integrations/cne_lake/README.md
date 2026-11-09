# ptrade 本地回测框架与既有 CNEquity 每日入口集成

这里保留完整的 [daily_update.py](<daily_update.py>) 部署模板，供明确选择部署的环境使用。原有 SDK/Python bridge 路径通过 backend=python 保留；默认 backend=rust 调用独立项目的数据 CLI。安装主包、打开看板或下载发行包不会自动部署模板，也不注册/修改任何计划任务。

项目第一开发者/维护者为 JonePoon，独立发行 **ptrade-local-backtest 0.1.0**。参考/复用的基础代码与归属见 [第三方来源](<../../THIRD_PARTY_NOTICES.md>)，不沿用旧 fork 的项目/Release 身份。

## 目录与部署

模板部署到与独立项目并列的 cne_lake 根目录后，按下面布局解析路径；直接从 integrations/cne_lake 运行不等价于已完成部署。现有每日入口与原 SDK 配置由现有环境保留，模板不是安装时覆盖它们的 hook。

~~~text
workspace/
├── ptrade-local-backtest/
│   ├── .venv/Scripts/python.exe
│   ├── ptrade_data_config.json
│   └── integrations/cne_lake/daily_update.py
└── cne_lake/
    ├── daily_update.py                 明确部署的模板副本
    ├── .venv/Scripts/python.exe        原 SDK 环境
    ├── lake.toml
    └── run.py                         既有 legacy bridge
~~~

新模板的 PT_ROOT 指向相邻 ptrade-local-backtest；Python 模块仍调用 ptrade_sim.cli，保持内部命名空间兼容。Rust 控制层使用本项目虚拟环境，SDK 参考刷新使用独立 CNEquity 环境。主 Python/Rust writer 固定 DuckDB **1.5.6**；SDK 按原部署使用其环境，不能把 SDK 依赖混装进回测环境。

backend=python 使用既有 CNEquity SDK 与 Python bridge，本仓库不复制或替换旧 ETL 算法，也不捆绑 SDK。该路径仍需现有 lake.toml、run.py 和对应 SDK。

## 数据配置

在新项目根目录准备自己的 ptrade_data_config.json；完整字段与参数优先级见 [数据链配置](<../../docs/native_data_chain.md#配置与路径>)。lake/db 可以选择独立测试目标或已备本地数据，不自动覆盖回测配置的 db_path。

指定 --config 时按配置选择 lake/db；显式 --lake / --db 再覆盖。无 --config 才沿用既有 cne_lake/lake 与 cne_lake/out/fixed.duckdb 默认布局。本文命令始终明确传配置。

## 先验证只读计划

在上述 workspace 目录执行：

~~~powershell
& ./ptrade-local-backtest/.venv/Scripts/python.exe ./cne_lake/daily_update.py --backend rust --config ./ptrade-local-backtest/ptrade_data_config.json --plan --no-reference-refresh
if ($LASTEXITCODE -ne 0) { throw "每日入口计划失败" }
~~~

--plan 不下载、不入库、不创建日志或任务目录；plan.ready 只描述原生工具探针，不证明数据源覆盖完整。新版模板的实际定向测试与独立版本结果见 [validation](<../../docs/validation.md>)。

## 运行显式增量入口

参考输入与目标配置准备好后，在同一 workspace 执行：

~~~powershell
& ./ptrade-local-backtest/.venv/Scripts/python.exe ./cne_lake/daily_update.py --backend rust --config ./ptrade-local-backtest/ptrade_data_config.json --start 2026-11-05 --end 2026-11-06 --symbols "000001.SZ,600000.SH" --output-dir ./work/daily-data-jobs --no-reference-refresh
if ($LASTEXITCODE -ne 0) { throw "每日入口未完整成功，检查阶段报告" }
~~~

去掉 --no-reference-refresh 会显式启用配置中的 SDK 参考阶段；这与主 CLI 默认禁用参考刷新不同。日期是示例历史窗口，覆盖取决于当次源数据和参考输入。

入口传播数据 CLI 的 failed/partial/cancelled 退出码，日志写到所选 output-dir/logs；不把进度 100% 或原生返回 0 代替 coverage。1m/5m、前收、hfq、历史状态、active revision 与事务边界详见 [完整数据链](<../../docs/native_data_chain.md>)。本说明没有计划任务创建、注册或变更命令。
