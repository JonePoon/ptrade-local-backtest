# Windows x64 安装、构建与 GitHub 发布

[返回 README](<../README.md>) · [使用指南](<user_guide.md>) · [数据链](<native_data_chain.md>) · [Rust 与静态导出](<rust_acceleration_and_export.md>) · [验收记录](<validation.md>)

## 分发边界

**ptrade 本地回测框架**的主发行包为 `ptrade-local-backtest 0.1.0`，第一开发者/维护者 **JonePoon**，独立发行渠道为 [JonePoon/ptrade-local-backtest Releases](https://github.com/JonePoon/ptrade-local-backtest/releases)。本仓库不是 ptrade-sim 的 GitHub fork，但包含基础代码复用；作者、来源 commit 与许可范围见 [THIRD_PARTY_NOTICES](<../THIRD_PARTY_NOTICES.md>)。上游 PyPI `ptrade-sim` 不等于本项目新版。

对应版本的资产命名：

| 资产 | 内容/依赖 |
|---|---|
| `ptrade_local_backtest-0.1.0-py3-none-any.whl` | Python 主包和构建好的看板；不夹带本机 EXE/DLL/PyO3 扩展；Python ≥3.10 |
| `ptrade-local-backtest-data-windows-x64.zip` | 独立 fetch/store EXE、配置/安装脚本、文件 manifest、根 MIT/上游原 MIT/作者/第三方归属 |
| `ptrade_local_backtest_native-0.1.0-cp310-abi3-win_amd64.whl` | 可选 Rust 分钟索引；Windows x64、CPython ≥3.10 ABI |
| `ptrade_local_backtest-0.1.0.tar.gz` | Hatch 源码发行；含原生源码、脚本、前端源码、图文、部署模板与许可；前端需要自行构建 |
| `ptrade-local-backtest-0.1.0-windows-x64.zip` | Windows 完整安装包，聚合主 wheel、索引 wheel、原生数据 ZIP 与配套安装资料；与前三种单独发行保持区分 |

下载时以 Release 实际附件为准。下文 `v0.1.0` 命令针对该 tag；构建步骤产生的预期文件名与“已经上传/远端 CI 已通过”是不同事实。最新实际验收在 [validation](<validation.md>)。

## 从 Windows 完整包安装

完整 ZIP 的实际布局由 [完整项目打包器](<../scripts/package_project_release.py>) 生成；聚合三项安装资产和精确 Git 源码 ZIP，不含 Python 本身、Rust/Node 工具链或第三方依赖离线 wheelhouse。主包所需 Python 依赖仍在独立环境安装；安装好依赖并准备本地数据后，回测/复盘/静态导出不需要 PTrade 云端。

~~~text
ptrade-local-workspace/
├── packages/
│   ├── ptrade_local_backtest-0.1.0-py3-none-any.whl
│   ├── ptrade_local_backtest_native-0.1.0-cp310-abi3-win_amd64.whl
│   └── ptrade-local-backtest-data-windows-x64.zip
├── source/ptrade-local-backtest-0.1.0-source.zip
├── INSTALL.md
└── SHA256SUMS
~~~

源码 ZIP 名称由构建时精确输入决定，上面是本文构建命令的文件名。Hatch tar.gz 作为单独 Release 资产保留，不放入此完整 ZIP；包内 SHA256SUMS 记录五个构建输入的摘要，其中 tar.gz 是外层资产。发行目录的 SHA256SUMS 还追加完整 ZIP 自身摘要，二者范围不同。

在新的父目录下载、解包并进入包根目录：

~~~powershell
New-Item -ItemType Directory -Path ./packages -Force | Out-Null
gh release download v0.1.0 --repo JonePoon/ptrade-local-backtest --dir ./packages --pattern "ptrade-local-backtest-0.1.0-windows-x64.zip"
if ($LASTEXITCODE -ne 0) { throw "完整包下载失败" }
Expand-Archive -LiteralPath ./packages/ptrade-local-backtest-0.1.0-windows-x64.zip -DestinationPath ./ptrade-local-workspace
Set-Location ./ptrade-local-workspace

# 只核对完整包实际包含的 3 个 packages 资产与 1 个 source ZIP。
$sumLines = Get-Content -LiteralPath ./SHA256SUMS
$files = @(Get-ChildItem -LiteralPath ./packages -File) + @(Get-ChildItem -LiteralPath ./source -File)
if ($files.Count -ne 4) { throw "完整包安装/源码资产数不符" }
foreach ($file in $files) {
    $pattern = '^[a-f0-9]{64}  ' + [regex]::Escape($file.Name) + '$'
    $line = @($sumLines | Where-Object { $_ -match $pattern })
    if ($line.Count -ne 1) { throw "摘要记录不唯一或缺失：$($file.Name)" }
    $expected = $line[0].Substring(0, 64)
    $actual = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw "资产摘要不符：$($file.Name)" }
}

py -3.12 -m venv .venv
& ./.venv/Scripts/python.exe -m pip install --upgrade pip
& ./.venv/Scripts/python.exe -m pip install "./packages/ptrade_local_backtest-0.1.0-py3-none-any.whl[duckdb,dashboard,perf]" ./packages/ptrade_local_backtest_native-0.1.0-cp310-abi3-win_amd64.whl
if ($LASTEXITCODE -ne 0) { throw "Python 安装失败" }
Expand-Archive -LiteralPath ./packages/ptrade-local-backtest-data-windows-x64.zip -DestinationPath ./native-tools
& ./.venv/Scripts/python.exe ./native-tools/install_native_tools.py --source ./native-tools/bin --manifest ./native-tools/MANIFEST.json
if ($LASTEXITCODE -ne 0) { throw "原生工具摘要/doctor/安装失败" }
& ./.venv/Scripts/ptrade-local-backtest.exe --version
& ./.venv/Scripts/python.exe -c "import ptrade_local_backtest as p; from ptrade_sim.accelerator import backend_status; print(p.PROJECT_NAME,p.PRIMARY_DEVELOPER,p.__version__); s=backend_status('rust'); print(s); assert p.__version__=='0.1.0' and p.PRIMARY_DEVELOPER=='JonePoon' and s['available'] and s['effective']=='rust'"
& ./.venv/Scripts/ptrade-local-backtest.exe dashboard --root ./backtest_results --host 127.0.0.1 --port 8765
~~~

预期版本为 ptrade-local-backtest 0.1.0，产品/维护者常量正确，Rust backend available。浏览器打开 <http://127.0.0.1:8765>；发布 wheel 使用包内首页/assets。已有 run 可直接复盘；新回测仍需按 [物理表契约](<user_guide.md#物理表与数据契约>) 准备本地数据。若只安装某个组件，使用下方独立资产安装流程。

## 从 GitHub Release 安装独立组件

在新的工作目录运行。安装发布二进制不需要 Cargo、MSVC 或 Node；主包仍需要 Python，数据工具由主包控制或可独立调用。

### 1. 下载并建立独立环境

~~~powershell
New-Item -ItemType Directory -Path ./ptrade-workspace -Force | Out-Null
Set-Location ./ptrade-workspace
New-Item -ItemType Directory -Path ./packages -Force | Out-Null

# gh CLI 已安装时：公开 Release 可按资产名称下载。
gh release download v0.1.0 --repo JonePoon/ptrade-local-backtest --dir ./packages --pattern "ptrade_local_backtest-0.1.0-py3-none-any.whl" --pattern "ptrade-local-backtest-data-windows-x64.zip"
if ($LASTEXITCODE -ne 0) { throw "Release 下载失败" }

py -3.12 -m venv .venv
& ./.venv/Scripts/python.exe -m pip install --upgrade pip
& ./.venv/Scripts/python.exe -m pip install "./packages/ptrade_local_backtest-0.1.0-py3-none-any.whl[duckdb,dashboard,perf]"
if ($LASTEXITCODE -ne 0) { throw "主包安装失败" }
~~~

不用 gh 时，可在 Release 页下载同名文件，或在同一工作目录运行完整 URL 下载：

~~~powershell
$release = "https://github.com/JonePoon/ptrade-local-backtest/releases/download/v0.1.0"
Invoke-WebRequest ($release + "/ptrade_local_backtest-0.1.0-py3-none-any.whl") -OutFile ./packages/ptrade_local_backtest-0.1.0-py3-none-any.whl
Invoke-WebRequest ($release + "/ptrade-local-backtest-data-windows-x64.zip") -OutFile ./packages/ptrade-local-backtest-data-windows-x64.zip
~~~

主环境固定 DuckDB 1.5.6；不要把现有 SDK 环境的 DuckDB 2.0 依赖安装到这个环境。这里是依赖/文件版本边界，不是修改既有数据的迁移操作。

### 2. 解包、验 manifest、安装原生 CLI

~~~powershell
Expand-Archive -LiteralPath ./packages/ptrade-local-backtest-data-windows-x64.zip -DestinationPath ./native-tools
& ./.venv/Scripts/python.exe ./native-tools/install_native_tools.py --source ./native-tools/bin --manifest ./native-tools/MANIFEST.json
if ($LASTEXITCODE -ne 0) { throw "原生工具校验/安装失败" }
~~~

预期 ZIP 布局：

~~~text
native-tools/
├── bin/ptrade-fetch.exe
├── bin/ptrade-store.exe
├── install_native_tools.py
├── build_native_data.ps1
├── data_config.example.json
├── native_data_chain.md
├── LICENSE
├── NOTICE.tdxpy
├── AUTHORS.md
├── THIRD_PARTY_NOTICES.md
├── LICENSES/ptrade-sim-MIT.txt
└── MANIFEST.json
~~~

[安装脚本](<../scripts/install_native_tools.py>) 在执行任何 EXE、修改目标之前核对两工具的 bytes/SHA-256，再运行 doctor；store 必须返回 `engine_version=v1.5.6`。复制后再次核验 staged 字节，最后逐文件原子替换到当前 Python 的 `ptrade_sim/bin`。Release 安装结果应包含 `manifest_verified=true`。原生 bin 由安装器独立放入，不是主 `py3-none-any` wheel 自带的跨平台内容。

ZIP 中的构建脚本用于配套完整源码的构建流程，不含 Cargo 源码；只安装 ZIP 二进制不用运行它。

不指定 `--manifest` 时默认查找 bin 上一级的 `MANIFEST.json`；从自己构建目录安装且没有 manifest 时，receipt 会明确写 `manifest_verified=false`，不冒充分发摘要校验。

### 3. 核验新发行身份与只读计划

~~~powershell
& ./.venv/Scripts/ptrade-local-backtest.exe --version
& ./.venv/Scripts/python.exe -c "import importlib.metadata as m, duckdb; import ptrade_sim.data_tools, ptrade_sim.strategy_export; print(m.version('ptrade-local-backtest'),duckdb.__version__); assert m.version('ptrade-local-backtest')=='0.1.0'; assert duckdb.__version__=='1.5.6'"
& ./native-tools/bin/ptrade-fetch.exe doctor
& ./native-tools/bin/ptrade-store.exe doctor

# 把 ZIP 公开模板转换为本工作目录的配置，不触碰相邻正式 cne_lake。
& ./.venv/Scripts/python.exe -c "import json,pathlib; c=json.loads(pathlib.Path('native-tools/data_config.example.json').read_text(encoding='utf-8')); c['lake']=str(pathlib.Path('data/lake').resolve()); c['db']=str(pathlib.Path('data/quant.duckdb').resolve()); c['reference_refresh']['enabled']=False; pathlib.Path('ptrade_data_config.json').write_text(json.dumps(c,indent=2),encoding='utf-8')"
& ./.venv/Scripts/ptrade-local-backtest.exe data status --config ./ptrade_data_config.json --json
& ./.venv/Scripts/ptrade-local-backtest.exe data plan --config ./ptrade_data_config.json --start 2026-11-06 --end 2026-11-06 --symbols "000001.SZ,600000.SH" --datasets "daily_bars,minute_bars,minute_bars_5m" --json
~~~

预期主版本 0.1.0、Python DuckDB 1.5.6、原生 store v1.5.6；原生工具 available=true。plan 不下载、不写库，不证明 bars 或参考数据已齐全。新空库需要真实参考输入/前收/hfq 才能严格入库，流程见 [数据链](<native_data_chain.md>)。

### 4. 可选 Rust 索引 wheel

~~~powershell
gh release download v0.1.0 --repo JonePoon/ptrade-local-backtest --dir ./packages --pattern "ptrade_local_backtest_native-0.1.0-cp310-abi3-win_amd64.whl"
if ($LASTEXITCODE -ne 0) { throw "索引 wheel 下载失败" }
& ./.venv/Scripts/python.exe -m pip install ./packages/ptrade_local_backtest_native-0.1.0-cp310-abi3-win_amd64.whl
& ./.venv/Scripts/python.exe -c "from ptrade_sim.accelerator import backend_status; s=backend_status('rust'); print(s); assert s['available'] and s['effective']=='rust'"
~~~

索引发行包名为 `ptrade-local-backtest-native`，模块仍为 `_ptrade_sim_native`。未安装索引可用 `auto` 回退或明确 `python`；显式 `rust` 不能静默回退。版本/架构不兼容会被报告。

### 5. 启动发布看板

~~~powershell
$env:PT_DATA_CONFIG = (Resolve-Path ./ptrade_data_config.json).Path
& ./.venv/Scripts/ptrade-local-backtest.exe dashboard --root ./backtest_results --host 127.0.0.1 --port 8765
~~~

浏览器打开 <http://127.0.0.1:8765>。另一个 PowerShell 窗口：

~~~powershell
Invoke-RestMethod http://127.0.0.1:8765/api/ping
Invoke-WebRequest http://127.0.0.1:8765/ | Select-Object StatusCode
~~~

预期 API ping 正常、首页 200。发布 wheel 应从包内找到前端；没有已有 run 时列表为空是正常状态，数据页仍可预览工具/计划。

安装不部署/覆盖既有每日入口，不注册任何计划任务。接入并列 SDK 的显式模板步骤见 [部署说明](<../integrations/cne_lake/README.md>)。

## 从干净源码构建

构建要求：Windows x64、Python 3.12、Node 24、pnpm 11.8.0、Rust stable ≥1.85.1、MSVC C++ 构建工具。bundled DuckDB 首次会编译 C++，时间取决于机器和缓存，不用旧记录承诺固定分钟数。

### 1. 环境与前端

~~~powershell
git clone https://github.com/JonePoon/ptrade-local-backtest.git ptrade-local-backtest-build
Set-Location ptrade-local-backtest-build
py -3.12 -m venv .venv
& ./.venv/Scripts/python.exe -m pip install --upgrade pip
& ./.venv/Scripts/python.exe -m pip install -e ".[dev,duckdb,dashboard,perf]" build "maturin>=1.7,<2"
if ($LASTEXITCODE -ne 0) { throw "构建依赖安装失败" }
npm install --global pnpm@11.8.0
pnpm --dir web install --frozen-lockfile
if ($LASTEXITCODE -ne 0) { throw "前端依赖安装失败" }
pnpm --dir web run test
if ($LASTEXITCODE -ne 0) { throw "前端逻辑回归失败" }
pnpm --dir web run lint
if ($LASTEXITCODE -ne 0) { throw "前端 lint 失败" }
pnpm --dir web run build
if ($LASTEXITCODE -ne 0) { throw "前端类型检查/构建失败" }
~~~

对应 Release 的复现应检出其确切 tag/commit；默认开发分支的最新状态不能替代已发布版本身份。

### 2. 将前端复制进包再分开构建 wheel/sdist

下面基于新的干净检出，不涉及用户正式库或旧研究输出。

~~~powershell
New-Item -ItemType Directory -Path ./src/ptrade_sim/web -Force | Out-Null
Copy-Item -Path ./web/dist -Destination ./src/ptrade_sim/web -Recurse -Force
if (-not (Test-Path ./src/ptrade_sim/web/dist/index.html)) { throw "缺少包内前端" }

& ./.venv/Scripts/python.exe -m build --wheel --outdir ../work/ptrade-local-release
if ($LASTEXITCODE -ne 0) { throw "主 wheel 构建失败" }
& ./.venv/Scripts/python.exe -m build --sdist --outdir ../work/ptrade-local-release
if ($LASTEXITCODE -ne 0) { throw "sdist 构建失败" }
~~~

不能用默认 `python -m build` 代替这两个调用：默认先 sdist、再从不带已构建前端的 sdist 生成 wheel，会丢掉看板。主包同时提供公共导入门面 `ptrade_local_backtest` 和兼容内部 `ptrade_sim`，主/短 CLI 经公共门面转入同一 CLI 实现，旧 `ptrade-sim` 入口保留。主 wheel 的 [Hatch 配置](<../pyproject.toml>) 保留包内前端并排除本机原生 bin/动态库；源码发行包含前端源码、全部脚本、Cargo 锁文件、部署模板和公开图文。

独立校验主 wheel：

~~~powershell
& ./.venv/Scripts/python.exe -c "import zipfile; p='../work/ptrade-local-release/ptrade_local_backtest-0.1.0-py3-none-any.whl'; z=zipfile.ZipFile(p); n=z.namelist(); assert z.testzip() is None; assert 'ptrade_sim/web/dist/index.html' in n; assert 'ptrade_local_backtest/__init__.py' in n; assert all(any(x.endswith('/licenses/'+k) for x in n) for k in ('LICENSE','LICENSES/ptrade-sim-MIT.txt','AUTHORS.md','THIRD_PARTY_NOTICES.md','native-fetch/NOTICE.tdxpy')); assert any(x.startswith('ptrade_sim/web/dist/assets/') for x in n); assert not any(x.startswith('ptrade_sim/bin/') or x.endswith(('.exe','.dll','.pyd','.so')) for x in n); print('core wheel contents OK')"
~~~

### 3. 构建索引和原生工具

本段是从所选源码真正编译的命令，不描述本次原生 EXE 的来源。此轮新工具分发使用此前冻结原生 ZIP 经整体与 manifest 摘要核验的两工具，25 个相关 Rust 源码字节一致；不采用变化后的旧 target 文件。来源记录见 [validation](<validation.md#原生组件来源与构建状态>)。

~~~powershell
$env:PYO3_PYTHON = (Resolve-Path ./.venv/Scripts/python.exe).Path
$env:Path = (& ./.venv/Scripts/python.exe -c "import sys; print(sys.base_prefix)") + ";" + $env:Path
cargo test --release --locked --manifest-path native/Cargo.toml --lib
if ($LASTEXITCODE -ne 0) { throw "索引测试失败" }
& ./.venv/Scripts/python.exe -m maturin build --release --locked --manifest-path native/Cargo.toml --interpreter ./.venv/Scripts/python.exe --out ../work/ptrade-local-release
if ($LASTEXITCODE -ne 0) { throw "索引 wheel 构建失败" }

foreach ($crate in @('native-fetch', 'native-store')) {
    cargo test --release --locked --manifest-path "$crate/Cargo.toml"
    if ($LASTEXITCODE -ne 0) { throw "原生测试失败：$crate" }
    cargo build --release --locked --manifest-path "$crate/Cargo.toml"
    if ($LASTEXITCODE -ne 0) { throw "原生构建失败：$crate" }
}
& ./native-fetch/target/release/ptrade-fetch.exe doctor
if ($LASTEXITCODE -ne 0) { throw "下载器启动检查失败" }
& ./native-store/target/release/ptrade-store.exe doctor
if ($LASTEXITCODE -ne 0) { throw "入库器启动检查失败" }
& ./.venv/Scripts/python.exe scripts/package_data_release.py --project . --output ../work/ptrade-local-release
if ($LASTEXITCODE -ne 0) { throw "原生 ZIP 打包/校验失败" }
~~~

[原生打包器](<../scripts/package_data_release.py>) 先 doctor、读取源字节，ZIP 内保存固定文件元数据和每项 SHA-256；通过 CRC/bytes/SHA 校验后原子发布归档，生成 receipt。压缩和首次 DuckDB C++ 编译是构建成本，不是行情吞吐基准。

### 4. 完整回归与干净安装冒烟

~~~powershell
& ./.venv/Scripts/python.exe -m ruff check src tests scripts
& ./.venv/Scripts/python.exe -m ruff format --check src tests scripts
& ./.venv/Scripts/python.exe -m mypy
& ./.venv/Scripts/python.exe -m pytest --cov=ptrade_sim --cov-report=json:coverage.json
if ($LASTEXITCODE -ne 0) { throw "Python 回归失败" }
& ./.venv/Scripts/python.exe scripts/check_coverage.py --json coverage.json
if ($LASTEXITCODE -ne 0) { throw "覆盖率门槛失败" }

py -3.12 -m venv ../work/ptrade-local-release-smoke/.venv
& ../work/ptrade-local-release-smoke/.venv/Scripts/python.exe -m pip install "../work/ptrade-local-release/ptrade_local_backtest-0.1.0-py3-none-any.whl[duckdb,dashboard,perf]"
& ../work/ptrade-local-release-smoke/.venv/Scripts/ptrade-local-backtest.exe --version
& ../work/ptrade-local-release-smoke/.venv/Scripts/ptrade-local-backtest.exe export --strategy ./examples/demo_rotation --config ./config.example.json --target-python 3.11 --check --json
~~~

启动安装后的看板还应从仓库外的工作目录验证首页/assets，确保命中包内前端而非源码的 `web/dist`；同时核验 ZIP 的 manifest、安装后的 doctor、ABI 索引对照。具体实际结果和 skip 原因单独留档，不把这些预期步骤写成已执行。

### 5. 生成精确源码 ZIP 与 Windows 完整包

先完成上面 wheel/sdist/索引/原生 ZIP 构建，并选定已冻结的同一 Git revision。输出目录应只放当前平台的一个索引 wheel；[完整项目打包器](<../scripts/package_project_release.py>) 遇到零个或多个匹配索引 wheel、缺资产/必要源码身份、ZIP CRC/path 或主 wheel 前端/平台边界错误会失败。

打包器还对源码 ZIP、主 wheel、索引 wheel、原生工具 ZIP 执行**许可内容校验**：所有匹配的上游原 MIT 全文 SHA 必须一致；项目许可必须保留 JonePoon 与 ceke233 版权行；AUTHORS/THIRD_PARTY_NOTICES 必须存在且非空。含 TDX 的源码/主 wheel/工具 ZIP 还要求原 NOTICE 全文 SHA 一致；索引 wheel 本身不含 TDX 移植，不要求该通知。这不是只核对文件名存在。

~~~powershell
$revision = git rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw "源码 revision 获取失败" }
New-Item -ItemType Directory -Path ../work/ptrade-local-release -Force | Out-Null
$dist = (Resolve-Path ../work/ptrade-local-release).Path
$sourceZip = Join-Path $dist 'ptrade-local-backtest-0.1.0-source.zip'
git archive --format=zip --prefix=ptrade-local-backtest-0.1.0/ --output="$sourceZip" $revision
if ($LASTEXITCODE -ne 0) { throw "精确源码 ZIP 构建失败" }
& ./.venv/Scripts/python.exe scripts/package_project_release.py --output $dist --source-zip $sourceZip --revision $revision --version 0.1.0 --platform windows-x64
if ($LASTEXITCODE -ne 0) { throw "完整项目 ZIP 构建/结构核验失败" }
~~~

输出包括完整 ZIP、生成的安装文档、SHA256SUMS 和 release-receipt.json。receipt 记录所选 git_revision、各输入与完整 ZIP 的 bytes/SHA-256；其 verified 状态对应打包器执行的结构、归档摘要和上述许可内容检查，不替代完整测试、隔离安装或远端 CI。Hatch tar.gz 在发行目录单独保留，完整包中的源码是 --source-zip 指定的精确 Git ZIP。

## 作者与许可证分发核验

每种资产都按实际包含内容保留归属，根 [MIT](<../LICENSE>) 增加 JonePoon 版权并保留 ceke233；[原始 MIT](<../LICENSES/ptrade-sim-MIT.txt>) 和 [tdxpy NOTICE](<../native-fetch/NOTICE.tdxpy>) 不改写。

| 分发 | 应检查的归属成员 |
|---|---|
| 主 wheel | dist-info/licenses 下的根 LICENSE、LICENSES/ptrade-sim-MIT.txt、AUTHORS.md、THIRD_PARTY_NOTICES.md、native-fetch/NOTICE.tdxpy |
| 索引 wheel | dist-info/licenses 下的根 LICENSE、LICENSES/ptrade-sim-MIT.txt、AUTHORS.md、THIRD_PARTY_NOTICES.md；索引本身不含 TDX 移植实现 |
| sdist | 根许可/作者/第三方记录、LICENSES 原 MIT、native-fetch/NOTICE.tdxpy；native 独立构建所需许可副本 |
| 原生数据 ZIP | LICENSE、LICENSES/ptrade-sim-MIT.txt、AUTHORS.md、THIRD_PARTY_NOTICES.md、NOTICE.tdxpy，MANIFEST 对每项记录 bytes/SHA-256 |
| Windows 完整 ZIP | 保留上述独立资产及其完整归属文件与总包清单 |

上游原 MIT 的 SHA-256 为 **1c6df15d98d43336bd3bd7117d2739a1e303468458d71848bb30c1a4cb1eac70**；tdxpy NOTICE 的 SHA-256 为 **b65ba06aec3e18cd1fad4b402a5ae74af64db175a7df7ae3ceb68ff62aae5456**。从归档实际读取字节复核，不能因 pyproject 列有 license-files 就省略检查。

## 发布检查表

- 使用本独立项目的干净检出，仅发布源码白名单；不把正式 Parquet/DuckDB、私有 JSON、策略研究、work 日志/绝对路径打入源码包或 Git 提交。
- 保留 JonePoon/ceke233 版权、根 MIT、上游原 MIT、AUTHORS、THIRD_PARTY_NOTICES 和对应 tdxpy NOTICE；源码引用的上游 baseline 不因独立建库消失。
- 三个 Cargo.lock 与 Python/前端锁文件随源码；主 wheel、独立索引 wheel、平台 ZIP 分开。
- wheel 中必须有首页/assets；sdist 中必须有构建/覆盖率/打包脚本、源码、部署模板与公开截图。
- 记录资产 bytes/SHA-256、doctor、版本、平台、测试与覆盖率，Release 的二进制与 tag 源码对应。
- 本地命令通过不等于远端 Actions 通过；[Python/前端 CI](<../.github/workflows/ci.yml>) 与 [原生 CI](<../.github/workflows/native-data.yml>) 的实际运行需在推送后核验。
- 不在这一步注册、重置或修改用户的既有计划任务，也不为验收重建正式多年行情。

维护者可在上述检查完成并将同一源码 revision 发布到本独立仓库后创建 Release：

~~~powershell
$revision = git rev-parse HEAD
gh release create v0.1.0 --repo JonePoon/ptrade-local-backtest --target $revision --title "ptrade 本地回测框架 0.1.0" --notes-file CHANGELOG.md ../work/ptrade-local-release/ptrade_local_backtest-0.1.0-py3-none-any.whl ../work/ptrade-local-release/ptrade_local_backtest-0.1.0.tar.gz ../work/ptrade-local-release/ptrade_local_backtest_native-0.1.0-cp310-abi3-win_amd64.whl ../work/ptrade-local-release/ptrade-local-backtest-data-windows-x64.zip ../work/ptrade-local-release/ptrade-local-backtest-0.1.0-windows-x64.zip ../work/ptrade-local-release/ptrade-local-backtest-0.1.0-source.zip ../work/ptrade-local-release/SHA256SUMS
~~~

该发布命令是维护者流程，不由用户安装触发，也不代表当前文档编辑已执行上传。
