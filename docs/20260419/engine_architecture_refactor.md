# Engine 架构重构设计文档

> 日期: 2026-04-19 (更新: 2026-04-19)
> 状态: **已实施** — 235 tests passing

---

## 1. 当前架构（v1 — 回调耦合）

### 1.1 线程模型

系统只有 **2 个线程**：

```
┌─────────────────────────────────────────────────────────────────┐
│  [sim-bars 线程]  (SimBarWorker._run)                          │
│                                                                 │
│  while not stop:                                                │
│      1. 生成 bar (BarGenerator.next_bar)                        │
│      2. 写入 BarCache (bar_cache.append)   ← 持有 cache lock   │
│      3. 调用 _on_bars(round_bars)                               │
│         ├── 构造 BarData dict               ← Python, 持有 GIL │
│         ├── push_bars(batch)                ← C++ 多线程推理    │
│         │   └── [C++ ThreadPool, 8线程并行]                     │
│         ├── get_all_outputs()               ← Python, 持有 GIL │
│         └── signal_deque.append(result)                         │
│      4. wait(interval)                                          │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  [主线程]  (用户策略)                                            │
│                                                                 │
│  engine.get_factor_outputs()   → 读 signal_deque[-1]  (O(1))   │
│  engine.get_data()             → 读 BarCache.snapshot (copy)    │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 数据流

```
BarGenerator
    │
    ▼
BarCache.append()          ← sim-bars 线程写, 主线程读 (Lock 保护)
    │
    ▼
Engine._on_bars()
    │
    ├── 构造 batch dict     ← Python 循环 50 symbols
    ├── push_bars(batch)    ← 释放 GIL, C++ 8 线程并行推理
    ├── get_all_outputs()   ← 1 次 C++→Python 跨语言调用
    │
    ▼
signal_deque.append()      ← sim-bars 线程写, 主线程读 (deque 原子)
```

### 1.3 代码位置

| 组件 | 文件 | 关键方法 |
|------|------|----------|
| Engine | `factorengine/engine.py` | `__init__`, `_init_inference`, `_on_bars`, `get_factor_outputs` |
| SimBarWorker | `dataflow/simulation/worker.py` | `_run`, 接受 `on_bars` 回调 |
| SimDataflowManager | `dataflow/simulation/manager.py` | 转发 `on_bars` 给 worker |
| BarCache | `dataflow/livetrading/cache.py` | `append`, `snapshot` |
| InferenceEngine (C++) | `native/include/fe/runtime/inference_engine.hpp` | `push_bars`, `get_all_outputs` |
| pybind 绑定 | `native/pybind/fe_runtime_bind.cpp` | GIL 释放逻辑 |

### 1.4 当前架构的问题

| # | 问题 | 影响 |
|---|------|------|
| P1 | **职责耦合** — dataflow 线程里直接调 C++ 推理 | dataflow 模块反向依赖 engine runtime |
| P2 | **推理阻塞数据收集** — `_on_bars` 执行期间 worker 不能生成下一轮 bar | 实际 bar 间隔 = `interval + 推理耗时` |
| P3 | **回调注入不通用** — `on_bars` 回调需要穿透 Manager → Worker | 换数据源（live/新交易所）得每层都改 |
| P4 | **`_on_bars` 中有 Python 循环** — 50 个 symbol 的 BarData 构造在 GIL 下 | 微秒级开销, 暂可忽略 |

---

## 2. 目标架构（v2 — 信号解耦, 三线程）

### 2.1 线程模型

```
┌─────────────────────────────────────┐
│  [dataflow 线程]  (sim-bars)        │
│                                     │
│  while not stop:                    │
│      1. 生成 bar                    │
│      2. 写入 BarCache               │
│      3. bar_queue.put(round_bars)   │   ──── queue.Queue ────┐
│      4. wait(interval)              │                        │
│                                     │                        │
│  ※ 不知道推理的存在, 不依赖        │                        │
│    fe_runtime                       │                        │
└─────────────────────────────────────┘                        │
                                                               ▼
                                      ┌────────────────────────────────────┐
                                      │  [runtime 线程]  (factor-infer)    │
                                      │                                    │
                                      │  while not stop:                   │
                                      │      bars = bar_queue.get()        │
                                      │      batch = build_bar_data(bars)  │
                                      │      push_bars(batch)     ← C++   │
                                      │      result = get_all_outputs()    │
                                      │      signal_deque.append(result)   │
                                      │                                    │
                                      │  ※ 只管推理, 不管数据从哪里来    │
                                      └────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│  [主线程]  (策略/消费者)                       │
│                                              │
│  get_factor_outputs() → signal_deque[-1]     │
│  get_data()           → BarCache.snapshot()  │
└──────────────────────────────────────────────┘
```

### 2.2 通信机制

```
                    queue.Queue(maxsize=16)
dataflow 线程  ──────────────────────────────►  runtime 线程
                    put(round_bars)                get() 阻塞

                    collections.deque(maxlen=3)
runtime 线程   ──────────────────────────────►  主线程
                    append(result)                 [-1] 读取

                    BarCache (Lock 保护)
dataflow 线程  ──────────────────────────────►  主线程
                    append(bar)                    snapshot()
```

### 2.3 数据结构定义

```python
# bar_queue 中的消息格式 (与当前 on_bars 回调参数相同)
round_bars: dict[str, np.ndarray]
# key: symbol name
# value: shape-(8,) float64 array [ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote]

# signal_deque 中的消息格式 (不变)
{
    "ts": int,                                    # 毫秒时间戳
    "bar_index": int,                             # 第几轮推理
    "factors": dict[str, dict[str, float]],       # {symbol: {factor_id: value}}
}
```

---

## 3. 改动明细

### 3.1 `dataflow/simulation/worker.py` — 回退, 去掉回调

**目标**: SimBarWorker 职责回归纯粹 — 只生成 bar + 写 BarCache + 往 queue 发信号。

```python
# 改动前
class SimBarWorker:
    def __init__(self, ..., on_bars: OnBarsCallback | None = None):
        self._on_bars = on_bars

    def _run(self):
        while not self._stop_event.is_set():
            ...
            if self._on_bars is not None:
                self._on_bars(round_bars)     # ← 直接调推理, 耦合
            self._stop_event.wait(self._interval)

# 改动后
class SimBarWorker:
    def __init__(self, ..., bar_queue: queue.Queue | None = None):
        self._bar_queue = bar_queue

    def _run(self):
        while not self._stop_event.is_set():
            ...
            if self._bar_queue is not None:
                self._bar_queue.put(round_bars, block=False)  # ← 发信号, 不阻塞
            self._stop_event.wait(self._interval)
```

**关键**: `put(block=False)` + 捕获 `queue.Full`。如果 queue 满了说明 runtime 线程消费不过来, 可以选择 drop 或 log warning。

### 3.2 `dataflow/simulation/manager.py` — 透传 queue

```python
# 改动前
class SimDataflowManager:
    def __init__(self, ..., on_bars: OnBarsCallback | None = None):
        self._bar_worker = SimBarWorker(..., on_bars=on_bars)

# 改动后
class SimDataflowManager:
    def __init__(self, ..., bar_queue: queue.Queue | None = None):
        self._bar_worker = SimBarWorker(..., bar_queue=bar_queue)
```

### 3.3 `factorengine/engine.py` — 核心重构

**新增**: `_InferenceWorker` 内部类或独立方法, 作为 runtime 线程的 target。

```python
class Engine:
    def __init__(self, ...):
        ...
        # 推理相关
        self._bar_queue = queue.Queue(maxsize=16) if factor_group else None
        self._signal_deque = deque(maxlen=signal_buffer_size)
        self._runtime_thread = None

        if factor_group:
            self._init_inference(factor_group, num_threads)

        # dataflow 只拿到 queue, 不拿回调
        if mode == "simulation":
            self._dataflow = SimDataflowManager(
                ...,
                bar_queue=self._bar_queue,     # ← 传 queue, 不传回调
            )

    def _runtime_loop(self):
        """Runtime 线程主循环: 从 queue 取 bar, 推理, 写 deque。"""
        import fe_runtime as rt
        while not self._stop_event.is_set():
            try:
                round_bars = self._bar_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            # 构造 BarData batch
            ts_ms = 0
            batch = {}
            for sym, bar in round_bars.items():
                ts_ms = int(bar[0])
                close, volume = float(bar[4]), float(bar[5])
                open_, high, low = float(bar[1]), float(bar[2]), float(bar[3])
                prev_close = self._prev_closes.get(sym, close)
                ret = (close / prev_close - 1.0) if prev_close != 0 else 0.0
                batch[sym] = rt.BarData(close, volume, open_, high, low, ret)
                self._prev_closes[sym] = close

            # C++ 推理 (释放 GIL)
            self._inference.push_bars(batch)
            self._bars_pushed += 1

            # 收集结果
            result = self._inference.get_all_outputs()
            self._signal_deque.append({
                "ts": ts_ms,
                "bar_index": self._bars_pushed,
                "factors": result,
            })

    def start(self):
        if self._bar_queue is not None:
            self._stop_event = threading.Event()
            self._runtime_thread = threading.Thread(
                target=self._runtime_loop, daemon=True, name="factor-infer"
            )
            self._runtime_thread.start()
        self._dataflow.start()

    def stop(self):
        self._dataflow.stop()
        if self._runtime_thread is not None:
            self._stop_event.set()
            self._runtime_thread.join(timeout=5)
```

**新增参数** (`__init__`):

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `signal_buffer_size` | `int` | `3` | `signal_deque` 最大长度 |
| `bar_queue_size` | `int` | `16` | `bar_queue` 最大容量 |
| `bar_queue_timeout` | `float` | `0.5` | runtime 线程 `queue.get()` 超时秒数，影响停机响应速度 |

**删除**: `_on_bars` 方法 (不再需要回调)。

### 3.4 不需要改动的部分

| 组件 | 原因 |
|------|------|
| `InferenceEngine` (C++) | 接口不变: `push_bars`, `get_all_outputs` |
| `BarCache` | 读写模式不变 |
| `FactorRegistry` | 纯工厂, 不涉及线程 |
| `get_factor_outputs()` | 读 deque 逻辑不变 |
| `get_data()` | 读 BarCache 逻辑不变 |
| 所有因子定义 (`factor_bank.py`) | 不涉及 |
| 因子对齐测试 (`test_real_factors.py`, `test_new_factors.py`) | 不涉及 Engine |

---

## 4. 线程安全分析

| 共享资源 | 写方 | 读方 | 保护机制 |
|----------|------|------|----------|
| `BarCache._data` | dataflow 线程 | 主线程 (snapshot) | `threading.Lock` |
| `bar_queue` | dataflow 线程 (put) | runtime 线程 (get) | `queue.Queue` 内置锁 |
| `signal_deque` | runtime 线程 (append) | 主线程 ([-1]) | CPython GIL 保证原子 |
| `_prev_closes` | runtime 线程 | (仅 runtime 线程) | 无需保护, 单线程独占 |
| `_bars_pushed` | runtime 线程 | 主线程 (读 property) | int 赋值在 CPython 下原子 |

**结论**: 无需新增任何锁。

---

## 5. 生命周期时序

```
Engine.__init__()
    ├── _init_inference()          # 编译因子 DAG, 分配 kernel 内存
    ├── 创建 bar_queue             # Queue(maxsize=16)
    └── 创建 SimDataflowManager    # 拿到 bar_queue 引用

Engine.start()
    ├── 启动 runtime 线程           # factor-infer, 阻塞在 queue.get()
    └── 启动 dataflow               # sim-bars 线程开始生成 bar

    ┌──── 运行中 ─────────────────────────────────────────────┐
    │ sim-bars:     生成 bar → BarCache → queue.put()         │
    │ factor-infer: queue.get() → push_bars → deque.append()  │
    │ 主线程:       get_factor_outputs() / get_data()         │
    └─────────────────────────────────────────────────────────┘

Engine.stop()
    ├── 停止 dataflow               # sim-bars 线程退出
    ├── 设置 stop_event             # runtime 线程检测到 → 退出
    └── join runtime 线程
```

---

## 6. Queue 满/背压策略

当 runtime 线程的推理速度跟不上 dataflow 线程的数据生成速度时, `bar_queue` 会满。

**策略: 非阻塞 put + drop + warning**

```python
# SimBarWorker._run
try:
    self._bar_queue.put(round_bars, block=False)
except queue.Full:
    logger.warning("bar_queue full, dropping bar round %d", self._bar_count)
```

**为什么不用阻塞 put?**
- 阻塞会让 dataflow 线程的 `wait(interval)` 计时不准
- 对于 5s K线 + 微秒级推理, queue 根本不会满
- 只有压测场景 (`sim_bar_interval=0.01`) 可能触发, drop 可接受

---

## 7. 可扩展性

新架构下接入新数据源只需实现一个 DataflowManager, 满足:

```python
class NewExchangeManager:
    def __init__(self, ..., bar_queue: queue.Queue | None = None):
        # 内部 worker 往 bar_queue.put(round_bars)

    def start(self): ...
    def stop(self): ...
    def get_bar_snapshot(self, symbols=None): ...
```

Engine 不需要改动, 因为 runtime 线程只认 `bar_queue`。

---

## 8. 需要更新的测试

| 测试文件 | 改动 |
|----------|------|
| `tests/runtime_engine/test_engine_integration.py` | 适配新线程模型; 可能需要更长的 sleep 等 runtime 线程消费 |
| `tests/dataflow/test_simulation.py` | 无需改 (不涉及因子推理) |
| `tests/factors/test_inference_engine.py` | 无需改 (直接测 C++ 层) |
| `tests/factors/test_real_factors.py` | 无需改 |
| `tests/factors/test_new_factors.py` | 无需改 |

---

## 9. 改动检查清单

- [x] `dataflow/simulation/worker.py`: `on_bars` → `bar_queue`, `put(block=False)`
- [x] `dataflow/simulation/manager.py`: 透传 `bar_queue`
- [x] `factorengine/engine.py`:
  - [x] 新增 `_runtime_loop` 方法
  - [x] `start()` 启动 runtime 线程
  - [x] `stop()` 通知 + join runtime 线程
  - [x] 删除 `_on_bars` 方法
  - [x] 传 `bar_queue` 而不是 `on_bars_cb`
- [x] `tests/runtime_engine/test_engine_integration.py`: 适配验证 + 新增架构测试
- [x] `examples/engine_rt_demo.py`: API 不变, 无需改
- [x] 运行全量测试 `pytest tests/ -v` → **235 passed, 0 failed**
