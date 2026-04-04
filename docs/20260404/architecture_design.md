# FactorEngine 系统架构设计

## 核心思想

整个系统由两个独立线程组成，通过一个共享的 `data_cache` 解耦：

```
┌─────────────────┐      shared       ┌─────────────────┐
│   Thread 1      │    data_cache     │   Thread 2      │
│   Dataflow      │ ──────────────▶   │   FactorEngine  │
│   (数据搜集)     │   dict[symbol,    │   (因子计算)     │
│                 │    2D array]      │                 │
└─────────────────┘                   └─────────────────┘
```

- **Dataflow 线程**：负责 WebSocket 连接、数据搜集、写入 cache。对外只暴露 cache。
- **FactorEngine 线程**：完全不关心数据怎么来的，只负责从 cache 拿数据、算因子。

两者唯一的交互点就是 `data_cache`。

---

## data_cache 设计

```python
data_cache: dict[str, np.ndarray]   # 或 dict[str, list[list[float]]]
```

- **Key**: symbol，如 `"BTC-USDT-SWAP"`
- **Value**: 二维数组，shape = `(window_length, num_fields)`
  - 每一行是一个时间点的数据（如 ts, open, high, low, close, vol 等）
  - 每一列是一个字段
- **配置项**：
  - `freq`: 搜集频率，如 `1s` 或 `5s`（每隔多久往 cache 追加一行）
  - `window_length`: cache 最大保留行数，如 `1000`
- **滚动机制**: 当某个 symbol 对应的数据行数超过 `window_length`，从头部剔除旧数据

### 线程安全

`data_cache` 被两个线程同时读写，需要加锁：
- Dataflow 线程：写入（append + trim）
- FactorEngine 线程：读取（copy）

方案：每个 symbol 一把 `threading.Lock`，或整个 cache 一把 `threading.RLock`。
FactorEngine 拿数据时做一次 **copy**（深拷贝），拿完立刻释放锁，之后在自己线程里慢慢算。

---

## Dataflow 线程（Thread 1）

职责：
1. 连接 OKX WebSocket（`/ws/v5/business` candle 频道）
2. 按 `freq` 周期接收数据
3. 解析后追加到 `data_cache[symbol]`
4. 超过 `window_length` 时裁剪

不再有 `BarDispatcher`、`BarAggregator` 等复杂层级。
核心就是：**WebSocket → 解析 → 往 cache dict 里 append**。

Dataflow 对外只暴露：
- `data_cache` （被 FactorEngine 读）
- `start()` / `stop()` 生命周期方法

---

## FactorEngine 线程（Thread 2）

职责：
1. 提供 `get_data()` API，从 `data_cache` 拷贝当前快照
2. 用拷贝的数据跑因子计算
3. 完全不关心数据搜集的细节

### FactorEngine 对外 API

```python
class FactorEngine:
    def __init__(self, data_cache, lock):
        """接收 data_cache 的引用和锁"""
        self._cache = data_cache
        self._lock = lock

    def get_data(self, symbols: list[str] | None = None) -> dict[str, np.ndarray]:
        """从 cache 拷贝数据快照。

        Args:
            symbols: 指定 symbol 列表，只返回这些 symbol 的数据。
                     如果不传（None），返回 cache 中所有 symbol。
        
        返回: {symbol: ndarray of shape (N, num_fields)}
        """
        with self._lock:
            if symbols is None:
                return {sym: arr.copy() for sym, arr in self._cache.items()}
            return {sym: self._cache[sym].copy()
                    for sym in symbols if sym in self._cache}

    def compute_factors(self, snapshot: dict[str, np.ndarray]):
        """基于快照进行因子计算。用户继承此方法实现自己的逻辑。"""
        raise NotImplementedError
```

关键：`get_data()` 是 FactorEngine 唯一的数据入口，返回的是 **copy**，之后随便算，不阻塞 Dataflow。

---

## 启动流程

```python
# engine.py — 真正的入口
class Engine:
    def __init__(self, symbols, freq="1s", window_length=1000):
        self.data_cache = {}          # shared dict
        self.lock = threading.RLock() # shared lock
        
        self.dataflow = Dataflow(symbols, freq, window_length,
                                  self.data_cache, self.lock)
        self.factor_engine = FactorEngine(self.data_cache, self.lock)

    def start(self):
        # Thread 1: dataflow
        self.dataflow_thread = threading.Thread(target=self.dataflow.run, daemon=True)
        self.dataflow_thread.start()
        
        # Thread 2 由外部调用者控制（测试脚本 or 实盘循环）
        # FactorEngine 不自己起线程，而是暴露 get_data() 让外面调

    def stop(self):
        self.dataflow.stop()
```

---

## 测试脚本（tests/ 下）

```python
# tests/test_live.py
from factorengine.engine import Engine
import time

engine = Engine(
    symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    freq="1s",
    window_length=1000,
)
engine.start()

# 简单的 while loop 测试
while True:
    time.sleep(10)
    snapshot = engine.factor_engine.get_data()
    for symbol, data in snapshot.items():
        print(f"{symbol}: shape={data.shape}, latest={data[-1] if len(data) > 0 else 'empty'}")
```

这就是 main.py 该做的事 — 它只是个测试，放在 `tests/` 下。

---

## 现有代码 vs 目标架构的差距

| 方面 | 现有 | 目标 |
|------|------|------|
| 架构 | 单线程 asyncio，callback 驱动 | 双线程，cache 解耦 |
| 数据传递 | BarDispatcher push → FactorEngine | FactorEngine 主动 pull（get_data） |
| data_cache | 不存在，数据在 BarAggregator 的 deque 里 | 独立的 shared dict，线程安全 |
| FactorEngine | 被动接收 on_batch() | 主动调 get_data()，完全自主 |
| main.py | 是入口 | 应该是测试，放 tests/ |
| 入口 | main.py | engine.py 的 Engine.start() |

---

## 已确认的设计决策

1. **freq**: 订阅 OKX `candle1s`，Dataflow 内部聚合成 5s bar 写入 cache
2. **data_cache 字段**: `[ts, open, high, low, close, vol]`，暂不含 ticker 数据
3. **Dataflow 线程内部用 asyncio**（aiohttp WebSocket），Thread 2（FactorEngine）纯同步
4. **get_data() 支持两种模式**: 传 symbol list 则筛选返回，不传则返回全部
5. **纯内存 cache，不落盘**: 实盘系统，不需要 Writer/Parquet，去掉所有落盘逻辑
6. **用 threading 而非 multiprocessing**: Dataflow 是 I/O bound，GIL 不影响；后续如果因子计算成为 CPU 瓶颈再拆进程
