# 作者与维护者

## 独立项目

**ptrade 本地回测框架**的第一开发者与维护者为 [JonePoon](https://github.com/JonePoon)。公开独立仓库为 [JonePoon/ptrade-local-backtest](https://github.com/JonePoon/ptrade-local-backtest)，独立版本从 **0.1.0** 开始。

“第一开发者”指这个独立项目的创建、产品定位、开发组织、维护与发行责任，不替代下面的基础代码来源作者，也不把复用代码改写为完全原创。

## 基础代码作者与继承关系

- **ceke233**：[ceke233/ptrade-sim](https://github.com/ceke233/ptrade-sim) 的原作者。本框架参考并实际复用了其 PTrade 策略 API、调度、撮合与账户、基础回测、结果记录及看板等实现。
- 上游参考/复用基线：[1d0094783ad3daac482e7fafc4d241e4969a2d4e](https://github.com/ceke233/ptrade-sim/tree/1d0094783ad3daac482e7fafc4d241e4969a2d4e)。
- **JonePoon**：此前在 [JonePoon/ptrade-sim](https://github.com/JonePoon/ptrade-sim) 的增量开发中维护原生分钟索引、下载/入库链、静态转换、数据任务和工程化整合。独立项目以此前已验收的 [4f8d3cd](https://github.com/JonePoon/ptrade-sim/commit/4f8d3cd) 源码快照为种子，随后调整独立品牌与发行身份。

本仓库**不是 GitHub 上 ptrade-sim 的 fork 仓库**；它采用独立 Git 历史与独立发行。仓库关系与代码来源是两件事：独立建库不消除上述代码复用、作者归属或许可要求。初始化按白名单复制 241 个源码与公开配套文件，不继承旧 `.git`、正式行情/数据库或私有配置。

## 第三方作者

- **tdxpy contributors**：原生 TDX 下载器的协议字段/编解码规格通过公开 CNE vendored tdxpy **0.2.7** 客户端移植；原 [tdxpy NOTICE](<native-fetch/NOTICE.tdxpy>) 保持不变。
- Python、Rust、前端依赖各自的作者与许可证由其发行包和锁文件保留；本文件不把这些依赖列为 JonePoon 或 ceke233 的原创。

## 许可与贡献登记

本项目原创与修改部分采用 [根 MIT 许可证](<LICENSE>)；根文件同时保留 JonePoon 与 ceke233 的版权行。上游原始 MIT 全文原样存于 [ptrade-sim 原始 MIT](<LICENSES/ptrade-sim-MIT.txt>)。范围与分发要求见 [第三方来源及许可](<THIRD_PARTY_NOTICES.md>)。

提交新贡献时在 PR 中记录作者、来源、修改范围和许可证；维护者合并后保留相应归属。贡献流程见 [贡献指南](<CONTRIBUTING.md>)。
