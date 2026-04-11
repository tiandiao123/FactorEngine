# Simulation Dataflow 设计文档

> 2026-04-11（已按实现更新）

## 1. 目标

在无网络环境（如公司内网服务器）下，用虚拟数据流替代 OKX WebSocket，使 **Engine + Scheduler + FactorRuntime** 全链路可运行。

核心需求：
- 不改动 `dataflow/livetrading/` 任何代码
- Engine 通过新参数切换 `mode="simulation"` / `mode="live"`（默认 `"live"`）
- Simulation 模式仅模拟 **bar（K线）** 数据流，不模拟 trades / books
- 虚拟 bar 按可控频率（如 1s、10s）产生
- 内置标的列表（`DEFAULT_SYMBOLS` / `EXTENDED_SYMBOLS`），格式与 OKX 完全一致
- Engine 的 `get_data()` / `get_trade_data()` / `get_book_data()` 接口不变
- Scheduler / FactorRuntime 完全复用，零改动

## 2. 现有架构回顾

```
Engine
  └─ DataflowManager (livetrading)
       ├─ BarDataflowWorker  → OKXBarCollector (WebSocket) → BarCache
       ├─ TradeDataflowWorker → OKXTradeCollector (WebSocket) → TradeCache
       └─ BookDataflowWorker  → OKXBookCollector (WebSocket) → BookCache
```

Engine 通过 `DataflowManager` 的以下方法读取数据：
- `get_bar_snapshot(symbols)` → `dict[str, ndarray(N, 8)]`
- `get_trade_snapshot(symbols)` → `dict[str, ndarray(N, 3)]`
- `get_book_snapshot(symbols)` → `dict[str, ndarray(N, 20)]`
- `bar_count` / `trade_count` / `book_count` 属性

关键点：**Engine 只依赖 DataflowManager 的公开接口**，不关心数据来源是 WebSocket 还是本地生成。

## 3. 设计方案

### 3.1 新增模块结构

```
dataflow/
  simulation/
    __init__.py        # 包导出
    symbols.py         # 标的列表 + 参考基准价
    generator.py       # BarGenerator — 虚拟 kbar 数据生成器
    worker.py          # SimBarWorker — 后台线程，定时生成数据写入 Cache
    manager.py         # SimDataflowManager — 与 DataflowManager 同接口
```

### 3.2 标的列表 — `symbols.py`

提供与 OKX 格式完全一致的标的列表，无需联网获取：

```python
DEFAULT_SYMBOLS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
    "DOGE-USDT-SWAP", "XRP-USDT-SWAP",
]

EXTENDED_SYMBOLS = DEFAULT_SYMBOLS + [
    "BNB-USDT-SWAP", "ADA-USDT-SWAP", "AVAX-USDT-SWAP",
    "DOT-USDT-SWAP", "LINK-USDT-SWAP", "MATIC-USDT-SWAP",
    "UNI-USDT-SWAP", "ATOM-USDT-SWAP", "LTC-USDT-SWAP",
    "ARB-USDT-SWAP", "OP-USDT-SWAP", "APT-USDT-SWAP",
    "FIL-USDT-SWAP", "NEAR-USDT-SWAP", "SUI-USDT-SWAP",
]

SYMBOL_BASE_PRICES = {
    "BTC-USDT-SWAP": 85000.0,
    "ETH-USDT-SWAP": 3000.0,
    # ... 每个标的都有量级正确的参考价
}
```

### 3.3 接口契约 — SimDataflowManager

`SimDataflowManager` 实现与 `DataflowManager` **相同**的公开接口（仅 bar 数据有效，trades/books 返回空）：

```python
class SimDataflowManager:
    def __init__(
        self,
        symbols: list[str],
        bar_interval_seconds: float = 1.0,  # 虚拟数据产生频率
        bar_window_length: int = 1000,
        volatility: float = 0.001,
        base_volume: float = 100.0,
        seed: int | None = None,            # 可复现的随机种子
    ): ...

    def start(self): ...
    def stop(self): ...

    def get_bar_snapshot(self, symbols=None) -> dict[str, np.ndarray]: ...
    def get_trade_snapshot(self, symbols=None) -> dict[str, np.ndarray]: ...  # 返回 {}
    def get_book_snapshot(self, symbols=None) -> dict[str, np.ndarray]: ...   # 返回 {}

    @property
    def bar_count(self) -> int: ...
    @property
    def trade_count(self) -> int: ...   # 恒为 0
    @property
    def book_count(self) -> int: ...    # 恒为 0
    @property
    def data_cache(self) -> dict[str, np.ndarray]: ...
    @property
    def lock(self) -> threading.Lock: ...
```

### 3.4 数据生成器 — `generator.py`

**BarGenerator**：生成仿真 OHLCV bar（仅此一个生成器，不模拟 trades / books）

```python
class BarGenerator:
    """Generate synthetic OHLCV bars with random-walk midprice."""

    def __init__(self, base_price: float = 3000.0, volatility: float = 0.001,
                 base_volume: float = 100.0, seed: int | None = None): ...

    def next_bar(self, ts_ms: int) -> np.ndarray:
        """返回 shape=(8,) 的 float64 数组:
        [ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote]
        """
```

生成逻辑：
- midprice 做 geometric random walk: `mid *= exp(N(0, σ))`
- open = 上一根 close（初始 = base_price，每个标的从 `SYMBOL_BASE_PRICES` 获取）
- high = max(open, close) × (1 + |U(0, σ/2)|)
- low  = min(open, close) × (1 − |U(0, σ/2)|)
- vol  = base_volume × (1 + |N(0, 0.3)|)
- vol_ccy = vol × close, vol_ccy_quote = vol_ccy

每个 symbol 有独立的 generator 实例和独立的 seed（`seed + idx`），保证可复现。

### 3.5 Simulation Worker — `worker.py`

```python
class SimBarWorker:
    """后台线程定时产生虚拟 bar 并写入 BarCache。"""

    def __init__(
        self,
        symbols: list[str],
        bar_cache: BarCache,
        generators: dict[str, BarGenerator],
        interval_seconds: float = 1.0,   # 产生频率
    ): ...

    def start(self): ...   # 启动 daemon thread
    def stop(self): ...    # 设置 Event 停止

    @property
    def bar_count(self) -> int: ...
```

核心循环：
```python
def _run(self):
    while not self._stop_event.is_set():
        ts_ms = int(time.time() * 1000)
        for symbol in self.symbols:
            bar = self._generators[symbol].next_bar(ts_ms)
            self._bar_cache.append(symbol, bar)
            self._bar_count += 1
        self._stop_event.wait(self._interval)
```

特点：
- 复用 `BarCache`（直接 import 自 `dataflow.livetrading.cache`）
- 不需要 asyncio，纯 `threading.Thread` + `threading.Event`
- 每次循环为每个 symbol 产生一根 bar

### 3.6 Engine 改动

Engine 新增 `mode`、`sim_bar_interval`、`sim_seed` 参数，仅在构造函数中增加分支选择 DataflowManager：

```python
class Engine:
    def __init__(
        self,
        symbols: list[str],
        data_freq: str = "5s",
        pull_interval: str = "10s",
        # ... 原有参数 ...
        mode: str = "live",                      # 新增
        sim_bar_interval: float | None = None,   # 新增: simulation 产生频率（秒）
        sim_seed: int | None = None,             # 新增: 随机种子
    ):
        # ... 原有解析逻辑 ...

        if mode == "live":
            # 原有逻辑，不变（含 resolve_bar_channel 校验）
            self._dataflow = DataflowManager(...)
        elif mode == "simulation":
            from dataflow.simulation.manager import SimDataflowManager
            self._dataflow = SimDataflowManager(
                symbols=symbols,
                bar_interval_seconds=sim_bar_interval or 1.0,
                bar_window_length=bar_window_length,
                seed=sim_seed,
            )
        else:
            raise ValueError(f"Unknown mode: {mode!r}. Use 'live' or 'simulation'.")

        # 后续代码完全不变 — start/stop/get_data 都委托给 self._dataflow
```

**改动范围**：仅 `Engine.__init__` 增加 `if/elif` 分支。其他所有方法（`start`、`stop`、`get_data` 等）因为都是委托给 `self._dataflow`，无需任何改动。

### 3.7 频率语义区别

| 参数 | Live 模式 | Simulation 模式 |
|------|-----------|-----------------|
| `data_freq` | OKX K线周期（决定订阅 channel） | **不使用**（simulation 没有 OKX channel 概念） |
| `sim_bar_interval` | 不使用 | 虚拟数据产生频率（实际秒数） |
| `pull_interval` | Engine fetch 周期 | Engine fetch 周期（不变） |

Simulation 模式下 `data_freq` 仍可设置（用于语义标记"这是什么级别的bar"），但不会触发 `resolve_bar_channel()` 校验。

## 4. 使用示例

### 4.1 离线开发 — Simulation 替代 Live

```python
from dataflow.simulation.symbols import DEFAULT_SYMBOLS
from factorengine.engine import Engine

engine = Engine(
    symbols=DEFAULT_SYMBOLS,         # 内置标的列表，无需联网
    data_freq="5s",                  # 语义标记（simulation 下不校验 channel）
    pull_interval="10s",             # 每 10 秒 fetch 一次
    mode="simulation",               # 关键：切换到仿真
    sim_bar_interval=1.0,            # 每秒产一根虚拟 bar
    sim_seed=42,                     # 可复现
)
engine.start()

# 以下代码和 live 模式完全一样
import time
for _ in range(5):
    time.sleep(30)
    snapshot = engine.get_data()
    for sym, arr in snapshot.items():
        print(f"{sym}: {arr.shape[0]} bars, latest_close={arr[-1, 4]:.2f}")

engine.stop()
```

### 4.2 配合 Scheduler + FactorRuntime

```python
from dataflow.simulation.symbols import DEFAULT_SYMBOLS
from factorengine.engine import Engine
from factorengine.scheduler import Scheduler, FactorRuntime, FactorSpec, compute_bar_momentum

engine = Engine(
    symbols=DEFAULT_SYMBOLS,
    mode="simulation",
    sim_bar_interval=0.5,     # 每 0.5 秒产一根 bar（加速测试）
    sim_seed=123,
)

runtime = FactorRuntime(
    engine=engine,
    symbols=engine.symbols,
    factor_specs=[
        FactorSpec(name="mom_10", source="bars", window=10, compute_fn=compute_bar_momentum),
    ],
)

scheduler = Scheduler(interval_seconds=5.0, on_tick=runtime.evaluate)

engine.start()
scheduler.start()
# ... 运行一段时间后 ...
scheduler.stop()
engine.stop()
```

## 5. 不影响分析

| 组件 | 是否改动 | 原因 |
|------|----------|------|
| `dataflow/livetrading/*` | **否** | 完全不动 |
| `dataflow/__init__.py` | **否** | simulation 走独立包路径 |
| `factorengine/engine.py` | **仅 `__init__`** | 增加 `mode` 分支，其余方法不变 |
| `factorengine/scheduler/*` | **否** | 只依赖 `Engine.get_data()` 等接口 |
| `dataflow/simulation/*` | **新增** | 全部新代码 |

## 6. 文件清单

```
新增:
  dataflow/simulation/__init__.py       # 包导出
  dataflow/simulation/symbols.py        # 标的列表 + 参考基准价
  dataflow/simulation/generator.py      # BarGenerator
  dataflow/simulation/worker.py         # SimBarWorker
  dataflow/simulation/manager.py        # SimDataflowManager
  tests/test_simulation.py              # 端到端 smoke test

改动:
  factorengine/engine.py                # __init__ 增加 mode 分支（~20 行）
```
