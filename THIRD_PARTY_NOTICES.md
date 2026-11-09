# 第三方来源、归属与许可范围

## 项目身份

- 中文名称：**ptrade 本地回测框架**。
- 独立公开仓库：[JonePoon/ptrade-local-backtest](https://github.com/JonePoon/ptrade-local-backtest)。
- 第一开发者与维护者：[JonePoon](https://github.com/JonePoon)，独立项目起始版本 **0.1.0**。
- 本项目不是 ptrade-sim 的 GitHub fork 仓库，也不是券商、恒生/PTrade 或上游项目的官方发行。

独立仓库仍包含参考与复用的基础代码。作者登记见 [AUTHORS](<AUTHORS.md>)；不能将整个代码库描述为“完全原创”或只作“思路借鉴”。

## ptrade-sim 基础代码

| 项目 | 记录 |
|---|---|
| 来源 | [ceke233/ptrade-sim](https://github.com/ceke233/ptrade-sim) |
| 参考/复用基线 | [1d0094783ad3daac482e7fafc4d241e4969a2d4e](https://github.com/ceke233/ptrade-sim/tree/1d0094783ad3daac482e7fafc4d241e4969a2d4e) |
| 原版权声明 | Copyright (c) 2026 ceke233 |
| 许可证 | MIT |
| 原始全文 | [LICENSES/ptrade-sim-MIT.txt](<LICENSES/ptrade-sim-MIT.txt>)，从种子许可证原样保留 |
| 实际复用范围 | PTrade 策略 API、回调/调度、撮合与账户、基础回测、数据契约/取数、结果记录及基础看板等实现；独立项目继续在其上修改与扩展 |
| 本地种子 | 此前已验收的 [JonePoon/ptrade-sim 4f8d3cd](https://github.com/JonePoon/ptrade-sim/commit/4f8d3cd)，白名单复制 241 个源码与公开配套文件，未复制旧 Git 历史、正式数据或私配 |

[CHANGELOG](<CHANGELOG.md>) 保留独立版本之前的历史记录；历史版本号、统计与旧仓库引用仍按当时身份解释，不能当作本项目 0.1.0 的新验收。

## 根 MIT 的范围

[LICENSE](<LICENSE>) 为本仓库第一方源码、对基础代码的修改和本项目编写文档提供 MIT 条款，增加 **Copyright (c) 2026 JonePoon**，并保留 **Copyright (c) 2026 ceke233**。JonePoon 的版权行不表示取得第三方原创代码的唯一著作权，也不取代原作者声明。

复用的 ptrade-sim 软件仍需保留上游版权与 MIT 许可声明；原始全文另外存档，避免品牌调整改变原许可证文本。独立版本的命名、安装入口与 Git 历史不改变上述归属。

## tdxpy 协议规格移植

原生下载器的协议字段和编解码规格来源于公开 CNE vendored **tdxpy 0.2.7** MIT 客户端。覆盖有符号价格变长编码、日期/数量编码、响应长度与 zlib 压缩、三阶段握手、股票/指数 K 线请求及指数附加字段。

- 原通知与完整许可：[native-fetch/NOTICE.tdxpy](<native-fetch/NOTICE.tdxpy>)。
- 原版权行：**Copyright (c) 2022, tdxpy**。
- 该通知保持原样；实现边界见 [下载器说明](<native-fetch/README.md#协议规格来源>)。

## 依赖、外部 SDK 与参考文档

Python 依赖由 [pyproject.toml](<pyproject.toml>) 和 [uv.lock](<uv.lock>) 记录；Rust 依赖由 [索引锁文件](<native/Cargo.lock>)、[下载器锁文件](<native-fetch/Cargo.lock>)、[入库器锁文件](<native-store/Cargo.lock>) 记录；前端依赖由 [package.json](<web/package.json>) 与 [pnpm 锁文件](<web/pnpm-lock.yaml>) 记录。依赖自身的许可证、版权与 NOTICE 仍适用，根 MIT 不替换它们。

CNE SDK 是独立外部环境，不随本仓库捆绑；其许可与安装介质由原 SDK 分发提供。[PTrade API 参考](<docs/ptrade_api.md>) 继承种子中的参考资料，仅用于核对接口签名和语义，不是本项目原创 API 官方文档或官方发行声明；本地实际支持由 [使用指南](<docs/user_guide.md#api-支持清单>)、源码与测试定义。

## 发行时保留的文件

源码、sdist、主 wheel、独立索引 wheel 和原生工具 ZIP 都应携带与其内容相对应的作者、许可与通知。核心归属集合为：

1. [根 MIT](<LICENSE>)。
2. [上游原始 MIT](<LICENSES/ptrade-sim-MIT.txt>)。
3. [作者登记](<AUTHORS.md>)。
4. 本 [第三方来源记录](<THIRD_PARTY_NOTICES.md>)。
5. 对包含 TDX 移植实现的分发保留 [tdxpy NOTICE](<native-fetch/NOTICE.tdxpy>)；主项目完整归属包同时保留此通知便于追溯。

构建器将这些文件纳入各自的许可/资料目录；验证实际归档成员与摘要，不只检查仓库文件存在。原生 ZIP 的文件级 SHA-256、doctor 与安装 receipt、wheel/sdist 的许可检查见 [Windows 构建与分发](<docs/windows_release.md>)。
