# ClockDriver 统一调度重构方案

> 日期: 2026-05-04
> 目标: 彻底落地方案 B — ClockDriver 统一调度所有 Worker, 消除跨 Worker 耦合
> 前置: Phase 2 已完成 (`trades`-only 模式可用)

---

## 目录

1. [问题分析: 当前代码的耦合](#1-问题分析-当前代码的耦合)
2. [目标架构: 方案 B 完整形态](#2-目标架构-方案-b-完整形态)
3. [当前架构 vs 目标架构对比](#3-当前架构-vs-目标架构对比)
4. [逐文件变更清单](#4-逐文件变更清单)
5. [核心难点: kline 数据的被动到达](#5-核心难点-kline-数据的被动到达)
6. [ClockDriver 升级: 从纯回调到聚合中心](#6-clockdriver-升级-从纯回调到聚合中心)
7. [实施步骤](#7-实施步骤)
8. [测试计划](#8-测试计划)
9. [向后兼容性](#9-向后兼容性)

---

## 1. 问题分析: 当前代码的耦合

### 1.1 耦合链路

当前 `kline_trades` 模式下, `KlinePublisher` 越权负责了 trades 数据的 flush:

```text
当前 kline_trades 模式:

  Engine.__init__:
    accumulators = trades_worker.accumulators     ← 取出 trades 的内部状态
    kline_worker = KlineWorker(accumulators=...)  ← 传给 kline 侧
                                    │
                                    ▼
                   KlineWorker.__init__(accumulators=...)
                                    │
                                    ▼
                   KlinePublisher.__init__(accumulators=...)
                                    │
                                    ▼
                   KlinePublisher._flush_round():
                     round_bars = {sym: bar ...}        ← kline 自己的
                     for sym in round_bars:
                       acc = accumulators[sym]           ← 越权读 trades
                       round_trades[sym] = acc.snapshot_and_reset()  ← 越权 flush
                     bar_queue.put((round_bars, round_trades))
```

**问题**:

1. `KlinePublisher` 知道 `TradesAccumulator` 的存在 → **跨模块耦合**
2. `KlineWorker` 的构造函数需要 `accumulators` 参数 → **接口污染**
3. 如果加 orderbook, `KlinePublisher` 还得加 `ob_accumulators` → **无法扩展**
4. `kline_trades` 和 `trades` 模式用了两套完全不同的 flush 逻辑 → **不一致**
5. `KlinePublisher` 的 `TYPE_CHECKING` import `TradesAccumulator` → **不应该存在的依赖**

### 1.2 涉及的文件和耦合点

| 文件 | 耦合点 |
|------|--------|
| `dataflow/okx/kline/publisher.py` | `__init__(accumulators=...)`, `_flush_round()` 中读取和重置 trades accumulators, `TYPE_CHECKING` import |
| `dataflow/okx/kline/worker.py` | `__init__(accumulators=...)` 透传给 publisher, `TYPE_CHECKING` import |
| `factorengine/engine.py` | `accumulators = trades_worker.accumulators` 然后传给 `KlineWorker`, 两种模式不同的 ClockDriver wiring |
| `dataflow/okx/trades/worker.py` | docstring 提到 "kline_trades 模式下 KlinePublisher 直接读 accumulators" |

---

## 2. 目标架构: 方案 B 完整形态

### 2.1 核心原则

**每个 Worker 只管自己的数据收集和 flush, 互不知道对方存在。
ClockDriver 负责统一调度 tick, 收集各 Worker 的 flush 结果, 聚合后推入 bar_queue。**

### 2.2 架构全景

```text
目标架构 (所有模式统一):

  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
  │  KlineWorker    │    │  TradesWorker   │    │  OBWorker (P3)  │
  │                 │    │                 │    │                 │
  │  WS → Adapter   │    │  WS → Accumulator│   │  WS → Accumulator│
  │  → 内部 buffer  │    │  → 内部 buffer  │    │  → 内部 buffer  │
  │                 │    │                 │    │                 │
  │  flush() →      │    │  flush() →      │    │  flush() →      │
  │  return bars    │    │  return trades  │    │  return ob_snap │
  └────────┬────────┘    └────────┬────────┘    └────────┬────────┘
           │                      │                      │
           │    Worker 之间没有任何引用关系!               │
           │                      │                      │
           └──────────────────────┼──────────────────────┘
                                  │
                        ┌─────────▼─────────┐
                        │   ClockDriver     │
                        │   freq=5s         │
                        │                   │
                        │   tick():         │
                        │     1. 调用所有    │
                        │        registered │
                        │        flush()    │
                        │     2. 聚合结果   │
                        │     3. bar_queue  │
                        │        .put(...)  │
                        └─────────┬─────────┘
                                  │
                           bar_queue
                                  │
                        ┌─────────▼─────────┐
                        │  Engine           │
                        │  ._runtime_loop() │
                        └───────────────────┘
```

### 2.3 各模式下 Worker 启动和注册

```text
data_source="kline":
  启动: KlineWorker
  注册: clock.register(kline_worker)
  聚合: payload = {round_bars, None}

data_source="trades":
  启动: TradesWorker
  注册: clock.register(trades_worker)
  聚合: payload = {round_bars_from_trades, round_trades}

data_source="kline_trades":
  启动: KlineWorker + TradesWorker
  注册: clock.register(kline_worker) + clock.register(trades_worker)
  聚合: payload = {round_bars_from_kline, round_trades}

data_source="kline_trades_ob":  (未来 Phase 3)
  启动: KlineWorker + TradesWorker + OBWorker
  注册: clock.register(...) × 3
  聚合: payload = {round_bars, round_trades, round_ob}
```

**注意**: 每新增一种 Worker, 只需:
1. 实现 Worker (含 `flush()` 接口)
2. `clock.register(new_worker)`
3. ClockDriver 聚合逻辑自动包含

---

## 3. 当前架构 vs 目标架构对比

### 3.1 kline_trades 模式

```text
当前:
  KlinePublisher._flush_round():
    round_bars = 攒齐 kline bars
    round_trades = 遍历 accumulators, snapshot_and_reset()  ← 越权!
    bar_queue.put((round_bars, round_trades))

目标:
  ClockDriver.tick():
    round_bars   = kline_worker.flush()    ← kline 只管 kline
    round_trades = trades_worker.flush()   ← trades 只管 trades
    payload = 聚合(round_bars, round_trades)
    bar_queue.put(payload)
```

### 3.2 trades 模式 (已经是对的)

```text
当前和目标一致:
  ClockDriver.tick():
    trades_worker.flush() → (round_bars, round_trades) → bar_queue
```

### 3.3 kline 模式

```text
当前:
  KlinePublisher._flush_round():
    round_bars = 攒齐 kline bars
    bar_queue.put((round_bars, None))
  (KlinePublisher 直接推 bar_queue, 没有 ClockDriver)

目标:
  ClockDriver.tick():
    round_bars = kline_worker.flush()
    bar_queue.put((round_bars, None))
  (统一走 ClockDriver)
```

---

## 4. 逐文件变更清单

### 4.1 `dataflow/okx/kline/publisher.py` — 剥离 trades 耦合

**删除**:
- `accumulators` 构造参数
- `_flush_round()` 中 trades snapshot 逻辑 (第 184-191 行)
- `TYPE_CHECKING` import `TradesAccumulator`
- docstring 中 trades 相关描述

**修改**:
- `_flush_round()` → 只推 `round_bars` (不再是 tuple)
- 或者: `_flush_round()` 不再直接推 `bar_queue`, 而是**写入内部 buffer** 供 `flush()` 读取

**新的 KlinePublisher 职责**:
- 收集 bars, 攒批, forward-fill → 产出 `round_bars`
- 不知道 bar_queue 的存在 (可选方向)
- 或保留 bar_queue 推送 (仅用于 kline-only 纯模式, 兼容旧行为)

### 4.2 `dataflow/okx/kline/worker.py` — 剥离 trades 耦合, 加 flush()

**删除**:
- `accumulators` 构造参数
- `TYPE_CHECKING` import `TradesAccumulator`
- docstring 中 trades 相关描述

**新增**:
- `flush() -> dict[str, np.ndarray] | None` — 返回当前可用的最新 round_bars
  - 具体实现见 [第 5 节](#5-核心难点-kline-数据的被动到达)

### 4.3 `dataflow/okx/trades/worker.py` — 简化 flush()

**修改**:
- `flush()` 不再直接推 `bar_queue`, 而是**返回 `(round_bars, round_trades)`**
- 删除 `bar_queue` 构造参数
- 推 bar_queue 的职责交给 ClockDriver

**新签名**:
```python
def flush(self) -> tuple[dict[str, np.ndarray], dict[str, dict]]:
    """Snapshot all accumulators, return (round_bars, round_trades)."""
```

### 4.4 `dataflow/clock_driver.py` — 升级为聚合中心

**当前**: ClockDriver 只是简单地依次调用回调, 不关心返回值。

**目标**: ClockDriver 成为数据聚合中心:

```python
class ClockDriver:
    def register(self, name: str, flush_fn: Callable):
        """注册一个带名字的 flush 回调."""

    def _on_tick(self):
        """每个 tick:
        1. 调用所有 flush_fn, 收集返回值
        2. 聚合 round_bars + round_trades + round_extras
        3. bar_queue.put(payload)
        """
```

详见 [第 6 节](#6-clockdriver-升级-从纯回调到聚合中心)。

### 4.5 `factorengine/engine.py` — 统一 wiring

**删除**:
- `accumulators = trades_worker.accumulators` 取引用的逻辑
- 传 `accumulators` 给 `KlineWorker` 的代码
- 两种模式分别 wiring 的分支逻辑

**新增**:
- 所有 live 模式统一创建 ClockDriver
- 根据 `data_source` 注册对应 Worker 到 ClockDriver
- ClockDriver 持有 bar_queue 引用

```python
# 目标代码 (伪码):
if mode == "live":
    self._clock_driver = ClockDriver(
        freq_seconds=self.data_freq_seconds,
        bar_queue=self._bar_queue,
        symbols=symbols,
    )

    if data_source in ("kline", "kline_trades"):
        self._kline_worker = KlineWorker(symbols, freq_seconds=...)
        self._clock_driver.register("kline", self._kline_worker)

    if data_source in ("trades", "kline_trades"):
        self._trades_worker = TradesWorker(symbols)
        self._clock_driver.register("trades", self._trades_worker)

    # 未来:
    # if data_source includes "ob":
    #     self._ob_worker = OBWorker(symbols)
    #     self._clock_driver.register("ob", self._ob_worker)
```

---

## 5. 核心难点: kline 数据的被动到达

### 5.1 问题

kline 和 trades 有本质的区别:

| 特性 | trades | kline |
|------|--------|-------|
| 到达方式 | 持续流, 每秒几十~几百笔 | OKX 服务端定时推送, 每秒一根 1s candle |
| flush 时机 | 任何时候都可以 snapshot | ClockDriver tick 时, 当前时间窗口的 bars 可能还没到齐 |
| 数据完整性 | snapshot 那一刻的累积值就是正确的 | 需要攒齐所有 symbols 的 bars |

ClockDriver 每 5s tick 一次, 但 OKX 推送 kline 的时间可能有 0.5~2s 延迟。tick 那一刻, `KlinePublisher` 内部的 `_pending_round` 可能:
- 已经攒齐 → 直接返回
- 只到了部分 symbols → 需要 forward-fill
- 完全空的 (刚刚被上一轮 flush 清空) → 返回 None

### 5.2 解决方案: KlineWorker.flush() 读取"最新完整 round"

```python
class KlineWorker:
    def flush(self) -> dict[str, np.ndarray] | None:
        """返回最新可用的 round_bars.

        策略:
        1. 如果 publisher._pending_round 已攒齐所有 symbols → flush 并返回
        2. 如果未攒齐但有 _last_round → forward-fill 后返回
        3. 如果都没有 (第一轮, 数据还没来) → 返回 None
        """
        return self._publisher.flush_for_clock()
```

KlinePublisher 新增 `flush_for_clock()` 方法:

```python
def flush_for_clock(self) -> dict[str, np.ndarray] | None:
    """供 ClockDriver 调用的 flush — 返回数据但不推 bar_queue."""
    if not self._pending_round and self._last_round is None:
        return None

    round_bars = dict(self._pending_round)

    # Forward-fill missing symbols.
    if self._last_round is not None:
        for sym in self._expected_symbols:
            if sym not in round_bars:
                round_bars[sym] = self._last_round[sym].copy()

    # 第一轮没有 last_round 可 fill → 等下一次
    if self._last_round is None and set(round_bars) < self._expected_symbols:
        return None

    # 更新内部状态.
    self._last_round = round_bars
    self._pending_round.clear()
    self._round_start_time = None
    self._rounds_flushed += 1

    return round_bars
```

**关键**: `flush_for_clock()` **只返回数据, 不推 bar_queue**。推 bar_queue 的职责在 ClockDriver。

### 5.3 kline-only 模式是否也走 ClockDriver?

**推荐: 是。** 统一走 ClockDriver 可以:
- 保持所有模式的代码路径一致
- kline-only 模式也享受 wall-clock 对齐
- 未来从 `kline` 升级到 `kline_trades` 只需多注册一个 Worker, 零代码改动

但这意味着原来的 "kline 到齐就立刻 flush" 变成了 "等 ClockDriver tick 才 flush"。
这是可以接受的, 因为:
- ClockDriver tick 间隔 = bar frequency (比如 5s)
- kline 数据本身就是按 bar frequency 到达的
- 最多延迟一个 tick 周期, 对因子计算没有影响

**替代方案**: kline-only 模式保留原来的 KlinePublisher 直推 bar_queue (兼容旧行为),
只有 `kline_trades` 和 `trades` 走 ClockDriver。这样改动最小, 但代码路径不统一。

**建议**: 采用统一方案, 所有模式走 ClockDriver。

---

## 6. ClockDriver 升级: 从纯回调到聚合中心

### 6.1 当前 ClockDriver

```python
class ClockDriver:
    def register(self, callback: Callable[[], None]):
        self._callbacks.append(callback)

    async def _tick_loop(self):
        for cb in self._callbacks:
            cb()   # fire-and-forget, 不关心返回值
```

### 6.2 目标 ClockDriver

```python
from dataclasses import dataclass
from typing import Protocol

class FlushableWorker(Protocol):
    """所有 Worker 的 flush 接口协议."""
    def flush(self) -> dict | tuple | None: ...

@dataclass
class WorkerSlot:
    name: str           # "kline", "trades", "ob", ...
    worker: FlushableWorker

class ClockDriver:
    def __init__(self, freq_seconds, bar_queue, symbols):
        self._freq = freq_seconds
        self._bar_queue = bar_queue
        self._symbols = symbols
        self._slots: list[WorkerSlot] = []

    def register(self, name: str, worker: FlushableWorker):
        """注册一个 Worker.  名字用于聚合时区分数据类型."""
        self._slots.append(WorkerSlot(name=name, worker=worker))

    def _on_tick(self, ts_ms: int):
        """每个 tick 的核心逻辑."""
        results: dict[str, Any] = {}
        for slot in self._slots:
            try:
                results[slot.name] = slot.worker.flush()
            except Exception:
                logger.exception("flush error: %s", slot.name)

        payload = self._aggregate(ts_ms, results)
        if payload is not None:
            try:
                self._bar_queue.put(payload, block=False)
            except queue.Full:
                logger.warning("bar_queue full, dropping tick %d", ts_ms)

    def _aggregate(self, ts_ms, results) -> tuple | None:
        """聚合各 Worker 的 flush 结果为统一的 bar_queue payload.

        返回: (round_bars, round_trades) 或 None (无数据)
        """
        round_bars = None
        round_trades = None

        # kline worker 返回 dict[str, np.ndarray] | None
        kline_data = results.get("kline")
        if kline_data is not None:
            round_bars = kline_data

        # trades worker 返回 (bars_from_trades, trades_dict)
        trades_data = results.get("trades")
        if trades_data is not None:
            trades_bars, trades_dict = trades_data
            round_trades = trades_dict
            # 如果没有 kline 数据, 用 trades 自建的 OHLCV 作为 round_bars
            if round_bars is None:
                round_bars = trades_bars

        if round_bars is None:
            return None

        return (round_bars, round_trades)
```

### 6.3 聚合规则

| data_source | kline flush 结果 | trades flush 结果 | 聚合后 payload |
|-------------|-----------------|------------------|---------------|
| `kline` | `{sym: bar}` | 无 | `({sym: bar}, None)` |
| `trades` | 无 | `({sym: bar}, {sym: trades})` | `({sym: bar_from_trades}, {sym: trades})` |
| `kline_trades` | `{sym: bar}` | `({sym: bar}, {sym: trades})` | `({sym: bar_from_kline}, {sym: trades})` |

**kline_trades 模式下**: round_bars 来自 kline (更权威), round_trades 来自 trades。
trades 自建的 OHLCV (trades_bars) 被丢弃 — 它只在 trades-only 模式下使用。

---

## 7. 实施步骤

### Step 1: KlinePublisher 剥离 trades 耦合

- 删除 `accumulators` 参数和相关逻辑
- 新增 `flush_for_clock()` 方法 — 返回 round_bars, 不推 bar_queue
- 保留原有的 `_flush_round()` 内部逻辑 (攒批、forward-fill、dedup), 但 flush_for_clock 用新路径
- 删除 `TYPE_CHECKING` import

**测试**: 更新 `test_okx_kline_publisher.py`, 删除 trades 相关测试, 新增 `flush_for_clock` 测试

### Step 2: KlineWorker 剥离 trades 耦合, 加 flush()

- 删除 `accumulators` 参数
- 不再将 bar_queue 传给 KlinePublisher (或改为可选)
- 新增 `flush() -> dict[str, np.ndarray] | None` — 委托给 publisher.flush_for_clock()

**测试**: 更新 `test_okx_kline_worker.py`

### Step 3: TradesWorker.flush() 改为返回值

- `flush()` 返回 `(round_bars, round_trades)` 而不是直接推 bar_queue
- 删除 `bar_queue` 构造参数
- 删除内部的 bar_queue.put 逻辑

**测试**: 更新 `test_engine_trades_integration.py` 中的 `TestTradesWorkerFlush`

### Step 4: ClockDriver 升级为聚合中心

- `register(name, worker)` 替代 `register(callback)`
- 新增 `_aggregate()` 聚合逻辑
- 新增 `bar_queue` 和 `symbols` 参数
- tick 时调用各 Worker.flush(), 聚合, 推 bar_queue

**测试**: 更新 `test_clock_driver.py`, 新增聚合测试

### Step 5: Engine 统一 wiring

- 删除 `accumulators` 传递链
- 所有 live 模式统一创建 ClockDriver
- 根据 data_source 注册对应 Worker
- 更新 docstring

**测试**: 更新 `test_engine_trades_integration.py`, 新增 `kline_trades` 走 ClockDriver 的测试

### Step 6: 清理文档和 example

- 更新 v2 文档
- 更新 `engine_rt_trades.py` 注释
- 更新 `trades_kline_full_pipeline.md`

---

## 8. 测试计划

### 8.1 需要更新的测试文件

| 测试文件 | 变更原因 |
|---------|---------|
| `test_okx_kline_publisher.py` | 删除 trades accumulator 相关测试, 新增 `flush_for_clock()` 测试 |
| `test_okx_kline_worker.py` | 删除 accumulators 参数, 新增 `flush()` 测试 |
| `test_okx_kline_e2e.py` | queue payload 格式可能变化 |
| `test_clock_driver.py` | 新增 register(name, worker) 和聚合逻辑测试 |
| `test_engine_trades_integration.py` | TradesWorker.flush 签名变化, Engine wiring 变化, 新增 kline_trades 走 ClockDriver 测试 |

### 8.2 新增测试

```text
ClockDriver 聚合测试:
  - test_aggregate_kline_only        — 只有 kline worker 注册
  - test_aggregate_trades_only       — 只有 trades worker 注册
  - test_aggregate_kline_trades      — 两个 worker 注册, kline bars 优先
  - test_aggregate_kline_returns_none — kline flush 返回 None (第一轮), 跳过
  - test_aggregate_bar_queue_push    — 验证 payload 格式和推送

KlineWorker.flush 测试:
  - test_flush_returns_round_bars    — 有数据时返回 dict
  - test_flush_returns_none_first_round — 第一轮无数据返回 None
  - test_flush_forward_fills         — 部分 symbols 到达, forward-fill
  - test_flush_clears_pending        — flush 后 pending 清空

Engine 集成测试:
  - test_kline_trades_uses_clock_driver — kline_trades 模式下 ClockDriver 启动
  - test_kline_only_uses_clock_driver   — kline 模式也走 ClockDriver
  - test_no_accumulator_in_kline_worker — KlineWorker 不再有 accumulators 参数
```

---

## 9. 向后兼容性

### 9.1 Engine API — 无变化

```python
# 这些调用方式完全不变:
Engine(data_source="kline", ...)
Engine(data_source="trades", ...)
Engine(data_source="kline_trades", ...)
Engine(factor_group="okx_perp", ...)
Engine(factor_group=["okx_perp", "okx_perp_trades"], ...)
```

### 9.2 bar_queue payload 格式 — 无变化

```python
# 仍然是 (round_bars, round_trades | None)
(dict[str, np.ndarray], dict[str, dict] | None)
```

`Engine._runtime_loop()` 不需要改动。

### 9.3 Worker 接口变化 (内部)

| 类 | 变更 |
|----|------|
| `KlineWorker` | 删除 `accumulators` 参数; 新增 `flush()` |
| `KlinePublisher` | 删除 `accumulators` 参数; 新增 `flush_for_clock()` |
| `TradesWorker` | 删除 `bar_queue` 参数; `flush()` 改为返回值 |
| `ClockDriver` | `register(callback)` → `register(name, worker)` |

这些都是内部接口, 不影响用户。

### 9.4 simulation 模式 — 不受影响

simulation 模式使用 `SimDataflowManager`, 不经过 ClockDriver, 保持原样。

---

## 附录: 依赖关系 (重构前 vs 后)

### 重构前

```text
Engine
  ├── KlineWorker
  │     ├── KlinePublisher
  │     │     └── TradesAccumulator (via accumulators 参数)  ← 耦合!
  │     └── KlineAdapter
  ├── TradesWorker
  │     ├── TradesAccumulator
  │     └── OKXTradesCollector
  └── ClockDriver (仅 trades-only 模式)
```

### 重构后

```text
Engine
  ├── KlineWorker
  │     ├── KlinePublisher  ← 不知道 trades 的存在
  │     └── KlineAdapter
  ├── TradesWorker
  │     ├── TradesAccumulator
  │     └── OKXTradesCollector
  └── ClockDriver (所有 live 模式)
        ├── 注册: kline_worker
        ├── 注册: trades_worker
        └── 聚合 → bar_queue
```

**KlinePublisher 和 TradesAccumulator 之间: 零依赖。**
