# ptrade-local-backtest-native

**ptrade 本地回测框架**的独立可选 PyO3/numpy 扩展，Python 发行名 `ptrade-local-backtest-native`，版本 **0.1.0**，第一开发者/维护者 **JonePoon**。主包 `ptrade-local-backtest` 不强制依赖它，也不因安装主包触发 Rust 构建。

Python 命名空间 `ptrade_sim`、扩展模块 `_ptrade_sim_native` 和现有 Cargo crate 标识 `ptrade-sim-native` 是保留的内部兼容坐标，不是本项目的 Python 发行名。来源与归属见 [AUTHORS](<../AUTHORS.md>)、[第三方通知](<../THIRD_PARTY_NOTICES.md>)，根 [MIT](<../LICENSE>) 和 [上游原 MIT](<../LICENSES/ptrade-sim-MIT.txt>) 保留。

## 接口

~~~python
import numpy as np
from ptrade_sim.accelerator import backend_status, make_minute_index

# 两个重复槽返回首个精确行；缺槽仍保持缺失。先按下方命令安装原生扩展。
slot = np.array([1, 2, 2, 4], dtype=np.int32)
starts = {"600000.SS": (0, 4)}
status = backend_status(backend="rust")
index = make_minute_index(slot, starts, slot_count=241, backend="rust")
row = index.row_of("600000.SS", 2)
start, stop = index.rows_upto("600000.SS", 2, True)
rows = index.rows_of(["600000.SS", "missing", "600000.SS"], 2)
allocated_bytes = index.nbytes
assert row == 1 and (start, stop) == (0, 3) and rows == [1, -1, 1]
print(status, row, start, stop, rows, allocated_bytes)
~~~

slot 必须为一维 native-endian int32 numpy 数组，每个 code 的范围内非降序。
starts 是 code -> (start, end) 映射，半开范围必须在数组内；可为空、可重叠。
slot_count 可省略或为正整数。它是时间轴元数据，不会裁剪数据或查询边界。
查询为整数，支持 int32/int64 边界及超出 int64 的 Python 整数。
构造复制槽数组和代码，之后不持有输入的 Python 对象；输入变化不影响查询。

backend 取 auto/python/rust，字符串按 strip/lower 归一；不读取环境变量。
auto 仅在扩展模块确实未安装时返回 None；python 始终返回 None 且不导入扩展。
rust 缺扩展，以及 auto/rust 遇到加载或接口不兼容时，抛现有 DependencyError。
非法 backend 抛 ConfigError；非法槽/范围数据抛 DataError。

backend_status 的 JSON 字段：requested、effective、available、version、reason、state、module。
state 为 available/missing/incompatible；显式 python 为 disabled，不探查 Rust。
effective 为 rust/python；要求 Rust但缺失或扩展不兼容时为 null。
扩展导出 INDEX_API_VERSION=1，旧版本或缺方法不会被静默当成 Python 后端。

## 内存与复杂度

构造 O(N+C)，范围重叠时验证为 O(各范围长度之和)。
code/range 表负载不超过 50%，平均查 code O(1)；碰撞极端情况 O(C)。
row_of、rows_upto 为 O(log R)，R 是该代码行数。
rows_of 为 O(K log R)，一次 Python 边界且计算部分释放 GIL。
标量查询保留 GIL，避免加解锁成本超过短二分查找。

nbytes 统计 Rust 结构体、int32 槽副本、固定长度 code/range 表、UTF-8 代码字节。
这是自有分配载荷，不含 allocator bookkeeping、PyO3 Python 包装对象和临时输出。
开放寻址表自有且定长，不用 HashMap.capacity 推测内存；查询不增加索引内存。

## 构建与验证

工作目录为独立仓库根目录。Python ≥3.10；下面使用已建立的 Python 3.12 虚拟环境。索引 crate 声明 Rust ≥1.74，构建全部原生组件时使用 Rust stable ≥1.85.1；Windows 使用 MSVC C++ 构建工具，Linux/macOS 构建本机平台 wheel。

~~~powershell
& ./.venv/Scripts/python.exe -m pip install "maturin>=1.7,<2"
$env:PYO3_PYTHON = (Resolve-Path ./.venv/Scripts/python.exe).Path
$env:Path = (& ./.venv/Scripts/python.exe -c "import sys; print(sys.base_prefix)") + ";" + $env:Path
cargo fmt --manifest-path native/Cargo.toml --all -- --check
if ($LASTEXITCODE -ne 0) { throw "native fmt failed" }
cargo test --release --locked --manifest-path native/Cargo.toml --all-targets
if ($LASTEXITCODE -ne 0) { throw "native tests failed" }
cargo clippy --release --locked --manifest-path native/Cargo.toml --all-targets -- -D warnings
if ($LASTEXITCODE -ne 0) { throw "native clippy failed" }
& ./.venv/Scripts/python.exe -m maturin build --release --locked --manifest-path native/Cargo.toml --interpreter ./.venv/Scripts/python.exe --out ./native/dist
if ($LASTEXITCODE -ne 0) { throw "native wheel build failed" }
& ./.venv/Scripts/python.exe -m pip install --force-reinstall --no-deps ./native/dist/ptrade_local_backtest_native-0.1.0-cp310-abi3-win_amd64.whl
& ./.venv/Scripts/python.exe -m pytest tests/test_accelerator.py tests/test_acceleration_integration.py -q
& ./.venv/Scripts/python.exe -c "from ptrade_sim.accelerator import backend_status; s=backend_status('rust'); print(s); assert s['requested']==s['effective']=='rust' and s['available'] and s['state']=='available' and s['version']=='0.1.0'"
~~~

普通 Rust 单测保持默认特征并真实链接 Python；maturin 仅在构建扩展时按配置开启 extension-module。Windows 发布 wheel 采用 abi3-py310；其它平台使用实际生成的平台文件名，不能安装 Windows wheel。

成功判定：Rust 检查/测试退出 0；扩展安装后 Python 对照实际执行，不把原生项 skip 算作原生通过；状态要求 requested/effective=rust、available=true、state=available、version=0.1.0。未装扩展时纯 Python/缺依赖测试仍可运行，原生对照明确 skip。

[Cargo.lock](<Cargo.lock>) 固定依赖；构建物实际许可成员、安装和独立版本验收见 [Windows 分发](<../docs/windows_release.md>)、[validation](<../docs/validation.md>)。上面的命令与成功条件不是对新版本已经全部实测通过的声明。
