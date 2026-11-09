# ptrade 本地回测框架 · Web

前端包 `ptrade-local-backtest-web`，版本 **0.1.0**，第一开发者 **JonePoon**。独立项目：[JonePoon/ptrade-local-backtest](https://github.com/JonePoon/ptrade-local-backtest)。

技术栈保持 Vite、Vue 3、TypeScript、Tailwind v4、shadcn-vue / reka-ui、ECharts、Pinia，不因品牌调整改变依赖。
参考 / 复用 ptrade-sim 看板基础实现；原作者与许可证归属见 [第三方来源说明](../THIRD_PARTY_NOTICES.md)。内部 `ptrade_sim` 命名空间、API 路径与 DTO 继续兼容。

## 安装与开发

目录：`ptrade-local-backtest/web`。使用 Node 24+、pnpm 11。

```powershell
cd D:\PtradeCE\ptrade-local-backtest\web
pnpm install --frozen-lockfile
pnpm run dev --host 127.0.0.1
```

先在另一个终端启动后端：

```powershell
cd D:\PtradeCE\ptrade-local-backtest
ptrade-local-backtest dashboard --root D:\demo\backtest_results --port 8765
```

Vite 开发页默认端口 5173；`/api` 代理到 `http://127.0.0.1:8765`。后端静态托管逻辑见 [server.py](../src/ptrade_sim/server.py)。

## 检查与构建

```powershell
cd D:\PtradeCE\ptrade-local-backtest\web
pnpm test
pnpm run lint
pnpm exec vue-tsc -b
pnpm run build
```

构建脚本执行 `vue-tsc -b && vite build`，默认输出 `web/dist`。本次独立项目验收的最终产物单独输出：

```powershell
pnpm exec vue-tsc -b
pnpm exec vite build --outDir ../../work/ptrade-local-backtest_20261109/web/dist-final --emptyOutDir
$env:PTRADE_SIM_WEB_DIST = 'D:\PtradeCE\work\ptrade-local-backtest_20261109\web\dist-final'
ptrade-local-backtest dashboard --root D:\demo\backtest_results --port 18786
```

`PTRADE_SIM_WEB_DIST` 是兼容保留的显式构建目录覆盖；它优先于已安装包内和仓库内的旧构建产物。开发或验收时无需向 Python 源目录复制前端。
安装包含前端的 `ptrade-local-backtest[dashboard]` wheel 后，运行看板不需要 Node。

## 页面与兼容边界

- 回测记录：列表状态、进度、指标；详情资金曲线、月度扩展、交易分页与日志。
- PTrade 策略转换：本次 run 的策略 / 参数快照；静态诊断、Python 与报告下载；阻塞时不提供生产代码。
- 数据管理：配置、Rust 工具状态、库 / 湖覆盖、计划预览、任务状态与取消。默认隐藏本机路径，仅影响呈现，不改 API 值和提交参数。
- fixture 页面明确标记合成数据；`SYNTHETIC` 与 `MOCK` 状态不代表真实市场收益、原生任务执行或正式数据覆盖。
- 手机布局使用视口宽度自适应；图表与表格保留原数据口径。
- 主题偏好使用 `ptrade-local-backtest-theme`，不读取其他应用的主题键。

请求集中在 [client.ts](src/api/client.ts) 与 [data.ts](src/api/data.ts)，DTO 见 [types.ts](src/api/types.ts)。路由继续使用 `/api/runs`、`/api/run/{name}/detail`、`trades`、`log`、`source`、`export` 和 `/api/data/*`。交易、索引与数据库 schema 由后端管理，本前端品牌修改不改变这些契约。

测试入口：[dashboard.test.mjs](tests/dashboard.test.mjs)。基础组件目录 [ui](src/components/ui/) 保留上游组件结构。
