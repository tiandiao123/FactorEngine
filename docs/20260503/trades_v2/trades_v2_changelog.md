# Trades Integration v2 — Phase 2 变更与设计详解

> 日期: 2026-05-03
> 基于: Phase 1 (`kline_trades` 模式) → Phase 2 (`trades`-only 模式 + ClockDriver)
> 代码分支: `feature/trades_dataflow`

---

## 目录

1. [v1 → v2 变更总览](#1-v1--v2-变更总览)
2. [为什么需要 v2？— v1 的局限性](#2-为什么需要-v2--v1-的局限性)
3. [三个核心变更详解](#3-三个核心变更详解)
   - 3.1 TradesAccumulator 增加 OHLCV
   - 3.2 ClockDriver 统一时钟
   - 3.3 TradesWorker.flush() 自主推送
4. [三种数据模式全景对比](#4-三种数据模式全景对比)
5. [Engine 层变更: factor_group 多组支持](#5-engine-层变更-factor_group-多组支持)
6. [数据流向详解 — 从 WS 到因子输出](#6-数据流向详解--从-ws-到因子输出)
7. [文件变更索引](#7-文件变更索引)
8. [测试覆盖](#8-测试覆盖)
9. [后续 Phase 3 路线图](#9-后续-phase-3-路线图)

---

## 1. v1 → v2 变更总览

```text
┌─────────────────────────────────────────────────────────────────────┐
│                        v1 (Phase 1)                                │
│                                                                     │
│  ✅ kline 模式       — 原有功能, 完整可用                            │
│  ✅ kline_trades 模式 — KlinePublisher 驱动 flush, trades 搭便车     │
│  ❌ trades 模式       — stub, accumulator 收集但永远不 flush          │
│  ❌ factor_group      — 只支持单个 group                             │
│  ❌ Accumulator       — 只有 vwap/buy_vol/sell_vol, 无 OHLCV         │
│                                                                     │
├─────────────────────────────────────────────────────────────────────┤
│                        v2 (Phase 2)                                │
│                                                                     │
│  ✅ kline 模式       — 不变                                         │
│  ✅ kline_trades 模式 — 不变 (向后兼容)                              │
│  ✅ trades 模式       — ClockDriver 驱动 flush, 完整可用!             │
│  ✅ factor_group      — 支持 str | list[str], 可混合多组因子         │
│  ✅ Accumulator       — 新增 open/high/low/close/volume 自建 OHLCV   │
└─────────────────────────────────────────────────────────────────────┘
```

**一句话总结**: v2 让 `data_source="trades"` 从一个 stub 变成了真正可运行的模式 — 只需一条 trades WS 连接, 就能同时产出 OHLCV + trades 聚合特征, 驱动完整的因子推理流水线。

---

## 2. 为什么需要 v2？— v1 的局限性

### 问题 1: trades-only 模式是空壳

v1 中 `data_source="trades"` 的代码路径:

```text
v1 trades-only 模式:

  TradesWorker.start()
       │
       ▼
  OKXTradesCollector → on_trade() → TradesAccumulator.update()
       │
       │    ← 谁来 flush?
       │    ← 没有 KlinePublisher (因为不用 kline)
       │    ← 没有 ClockDriver (还没实现)
       │
       ▼
  accumulators 无限增长, bar_queue 永远为空, runtime_loop 空转
```

**结果**: trades 数据收了但永远不会被消费, 因子输出为空。

### 问题 2: Accumulator 缺少 OHLCV

v1 的 `TradesAccumulator.snapshot_and_reset()` 只返回:

```python
{"vwap": ..., "buy_vol": ..., "sell_vol": ..., "trade_count": ...}
```

在 `kline_trades` 模式下没问题 — OHLCV 来自 kline WS。但在 trades-only 模式下, 没有 kline 数据源, `BarData` 的 `open/high/low/close/volume` 全是 NaN:

```text
v1 trades-only:
  BarData.close  = NaN  ← 基于 close 的因子全废
  BarData.volume = NaN  ← 基于 volume 的因子全废
  BarData.vwap   = 有值  ← 只有 trades 原生特征可用
```

这意味着混合使用 kline + trades 因子是不可能的。

### 问题 3: 只能加载单个 factor_group

v1 的 `Engine(factor_group="okx_perp")` 只接受字符串。如果你想同时跑 kline 因子(`okx_perp`) 和 trades 因子(`okx_perp_trades`), 需要手动构造 `InferenceEngine`, 非常不便。

---

## 3. 三个核心变更详解

### 3.1 TradesAccumulator 增加 OHLCV

**核心思路**: K 线本质就是 trades 在固定时间窗口内的聚合。

```text
open  = 窗口内第一笔 trade 的 px
high  = max(所有 trade 的 px)
low   = min(所有 trade 的 px)
close = 窗口内最后一笔 trade 的 px
vol   = Σ(sz)
```

**代码变更** (`dataflow/okx/trades/accumulator.py`):

```python
# v1: 只有 4 个 slots
__slots__ = ("_lock", "_sum_pv", "_sum_v", "_buy_vol", "_sell_vol", "_count")

# v2: 新增 4 个 OHLCV slots
__slots__ = ("_lock", "_sum_pv", "_sum_v", "_buy_vol", "_sell_vol", "_count",
             "_open", "_high", "_low", "_close")
```

`update()` 热路径新增逻辑 (仍然 O(1)):

```python
def update(self, px, sz, side):
    with self._lock:
        # ... 原有 vwap/buy_vol/sell_vol 逻辑 ...

        # v2 新增: OHLCV 跟踪
        if self._count == 0:
            self._open = px          # 第一笔 = open
        if px > self._high:
            self._high = px          # 跟踪最高价
        if px < self._low:
            self._low = px           # 跟踪最低价
        self._close = px             # 最后一笔 = close
        self._count += 1
```

`snapshot_and_reset()` 返回值扩展:

```python
# v1 返回 4 个字段
{"vwap": ..., "buy_vol": ..., "sell_vol": ..., "trade_count": ...}

# v2 返回 9 个字段
{"vwap": ..., "buy_vol": ..., "sell_vol": ..., "trade_count": ...,
 "open": ..., "high": ..., "low": ..., "close": ..., "volume": ...}
```

**为什么这么做?**

```text
trades-only 模式下的 BarData 对比:

  v1:  BarData(close=NaN, volume=NaN, ..., vwap=100.5, buy_vol=3000)
                 ↑ 不可用                      ↑ 可用

  v2:  BarData(close=101.0, volume=5000, ..., vwap=100.5, buy_vol=3000)
                 ↑ 从 trades 自建!              ↑ 可用

  → 所有因子输入都有值, kline 因子和 trades 因子可以混合使用
```

### 3.2 ClockDriver — 统一时钟驱动

**核心问题**: 在 trades-only 模式下, 谁来定期触发 `snapshot_and_reset()`?

v1 设计文档讨论了两个方案:

```text
方案 A: 各 Worker 自建 timer
  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
  │ TradesWorker │  │ OBWorker    │  │ FundWorker  │
  │  timer: 5s  │  │  timer: 5s  │  │  timer: 5s  │
  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
         │                │                │
     各自 flush        各自 flush        各自 flush
    (时间有微小偏移)

方案 B: 独立 ClockDriver (v2 采用)
                ┌──────────────┐
                │ ClockDriver  │
                │   freq=5s    │
                │              │
                │  :00  :05    │
                │  :10  :15    │  ← 对齐到 wall-clock 边界
                └──────┬───────┘
                       │
          ┌────────────┼────────────┐
          │            │            │
     trades.flush  ob.flush    fund.flush
      (同一瞬间)   (同一瞬间)   (同一瞬间)
```

**为什么选方案 B?**

| 维度 | 方案 A (各自 timer) | 方案 B (ClockDriver) |
|------|-------------------|---------------------|
| 新数据流接入 | 每个 Worker 自建 timer | `clock.register(worker.flush)` 一行 |
| 多数据流时间同步 | 各自计时, 有偏移 | 统一节拍, 精确同步 |
| flush 频率调整 | 改每个 Worker | 改一处 `ClockDriver(freq)` |
| 代码维护 | timer 逻辑分散 | timer 逻辑集中 |
| 调试 | 对比各 Worker flush 时间 | 看 ClockDriver 日志 |

**ClockDriver 实现要点** (`dataflow/clock_driver.py`):

```python
class ClockDriver:
    def __init__(self, freq_seconds: float):
        self._freq_ms = freq_seconds * 1000
        self._callbacks = []          # 注册的 flush 回调列表

    def register(self, callback):     # 注册回调
        self._callbacks.append(callback)

    async def _tick_loop(self):       # 核心循环
        while self._running:
            now_ms = time.time() * 1000
            # 对齐到下一个 wall-clock 边界
            next_boundary = now_ms - (now_ms % self._freq_ms) + self._freq_ms
            sleep_s = (next_boundary - now_ms) / 1000
            await asyncio.sleep(sleep_s)

            # 同一时刻触发所有回调
            for cb in self._callbacks:
                cb()
```

**Wall-clock 对齐示意** (freq=5s):

```text
时间线:  00  01  02  03  04  05  06  07  08  09  10  11  12  13  14  15
                                  ↑                       ↑
                              tick #1                  tick #2
                           所有 worker                所有 worker
                           同时 flush                 同时 flush
```

无论 `ClockDriver` 何时启动, 它都对齐到 wall-clock 的 5s 边界 (`:00`, `:05`, `:10`, ...), 保证全局时间一致。

### 3.3 TradesWorker.flush() — 自主推送

v1 的 `TradesWorker` 是"被动"的 — 只负责收集, 由 `KlinePublisher` 在 flush 时读取。

v2 新增 `flush()` 方法, 让 `TradesWorker` 在 trades-only 模式下可以"主动"推送:

```text
v1 TradesWorker (被动):
  WS → on_trade() → accumulator.update()
  (等待 KlinePublisher 来读 snapshot)

v2 TradesWorker (可主动):
  WS → on_trade() → accumulator.update()
                          ↓
  ClockDriver.tick() → flush()
                          ↓
                  snapshot_and_reset() 所有 accumulators
                          ↓
                  构建 BarPoint: [ts, open, high, low, close, vol, NaN, NaN]
                          ↓
                  bar_queue.put((round_bars, round_trades))
```

**`flush()` 的数据转换流程**:

```text
  TradesAccumulator.snapshot_and_reset()
  ─────────────────────────────────────
  返回:
  {
    "vwap": 100.5,      ─┐
    "buy_vol": 3000.0,   │ trades 聚合特征
    "sell_vol": 2000.0,  ─┘
    "trade_count": 42,
    "open": 100.0,       ─┐
    "high": 102.0,        │ 从 trades 自建的 OHLCV
    "low": 99.0,          │
    "close": 101.0,       │
    "volume": 5000.0     ─┘
  }
         │
         ▼
  拆分为两部分:
         │
    ┌────┴────────────────────────┐
    │                             │
    ▼                             ▼
  round_bars[sym]              round_trades[sym]
  = np.array([                 = {"vwap": 100.5,
      ts_ms,                      "buy_vol": 3000.0,
      100.0,   # open             "sell_vol": 2000.0,
      102.0,   # high             "trade_count": 42,
      99.0,    # low              "open": 100.0,
      101.0,   # close            ...}
      5000.0,  # volume
      NaN,     # vol_ccy
      NaN,     # vol_ccy_quote
  ])
         │                             │
         └──────────┬──────────────────┘
                    ▼
        bar_queue.put((round_bars, round_trades))
                    │
                    ▼
        Engine._runtime_loop()  ← 完全复用, 无需改动!
```

---

## 4. 三种数据模式全景对比

### 架构流程图

#### 模式 1: `data_source="kline"` (原始模式)

```text
  OKX business WS
        │
  OKXKlineCollector
        │
  KlineAdapter (1s→Ns)
        │
  KlinePublisher
    _flush_round():
      round_bars = {...}
      round_trades = None
        │
  bar_queue.put((round_bars, None))
        │
  Engine._runtime_loop()
    BarData(close, volume, open, high, low, ret,
            vwap=NaN, buy_vol=NaN, sell_vol=NaN)
```

#### 模式 2: `data_source="kline_trades"` (v1 引入)

```text
  OKX business WS              OKX public WS
        │                            │
  OKXKlineCollector           OKXTradesCollector
        │                            │
  KlineAdapter                TradesAccumulator
        │                      (per-symbol, O(1))
        │                            │
  KlinePublisher ◄──── 读取 ────── accumulators
    _flush_round():
      round_bars = {...}
      round_trades = {sym: acc.snapshot_and_reset()}
        │
  bar_queue.put((round_bars, round_trades))
        │
  Engine._runtime_loop()
    BarData(close, volume, open, high, low, ret,
            vwap=✓, buy_vol=✓, sell_vol=✓)
```

#### 模式 3: `data_source="trades"` (v2 新增, 完整可用)

```text
                          OKX public WS
                               │
                         OKXTradesCollector
                               │
                         TradesAccumulator  ← v2: 同时维护 OHLCV!
                          (per-symbol, O(1))
                               │
  ClockDriver(freq=5s) ──────►│
    每 5s (wall-clock 对齐):   │
      trades_worker.flush() ──►│
                               │
                         flush():
                           snapshot_and_reset()
                           构建 BarPoint (OHLCV from trades)
                               │
                         bar_queue.put((round_bars, round_trades))
                               │
                         Engine._runtime_loop()
                           BarData(close=✓, volume=✓, open=✓, high=✓, low=✓, ret=✓,
                                   vwap=✓, buy_vol=✓, sell_vol=✓)

  只需 1 条 WS 连接!
  所有 BarData 字段都有值!
```

### 对比表

| 维度 | `kline` | `kline_trades` | `trades` (v2) |
|------|---------|----------------|---------------|
| WS 连接 | 1 (business) | 2 (business + public) | 1 (public) |
| flush 驱动 | KlinePublisher | KlinePublisher | ClockDriver |
| OHLCV 来源 | OKX candle | OKX candle | trades 自建 |
| trades 特征 | 无 | 有 (vwap, buy/sell_vol) | 有 |
| 频率灵活性 | 受限 OKX candle 频道 | 受限 OKX candle 频道 | 任意 (3s, 7s, 13s...) |
| 线程模型 | okx-kline + factor-infer | okx-kline + okx-trades + factor-infer | okx-trades + clock-driver + factor-infer |
| 适用场景 | 传统因子 | 混合因子 | 高频/trades-only 策略 |

---

## 5. Engine 层变更: factor_group 多组支持

v1 只能传单个字符串:

```python
# v1: 只能用一组因子
Engine(factor_group="okx_perp")          # ✅
Engine(factor_group="okx_perp_trades")   # ✅
# 想同时用两组? 做不到!
```

v2 支持 `str | list[str]`:

```python
# v2: 可以混合多组因子
Engine(factor_group="okx_perp")                           # ✅ 向后兼容
Engine(factor_group=["okx_perp", "okx_perp_trades"])      # ✅ 新增!
```

**内部实现**:

```text
factor_group=["okx_perp", "okx_perp_trades"]
       │
       ▼
_init_inference(groups=["okx_perp", "okx_perp_trades"])
       │
       ├── reg.load_group("okx_perp")        → 加载 0001~0010
       ├── reg.load_group("okx_perp_trades") → 加载 T0001~T0006
       │
       ▼
  for sym in symbols:
    for group in groups:
      graphs = reg.build_group(group)
      for fid, graph in graphs.items():
        inference.add_factor(sym, fid, graph)
       │
       ▼
  self._factor_ids = sorted([0001,...,0010, T0001,...,T0006])
                   = 16 个因子, 共享一个 InferenceEngine
```

---

## 6. 数据流向详解 — 从 WS 到因子输出

以 `data_source="trades"`, `factor_group=["okx_perp", "okx_perp_trades"]` 为例:

```text
Step 1: OKX trades WS 推送一笔交易
────────────────────────────────────
  {"instId": "BTC-USDT-SWAP", "px": "67500.5", "sz": "0.3", "side": "buy", "ts": "1714..."}

Step 2: OKXTradesCollector._dispatch() 解析
────────────────────────────────────
  on_trade("BTC-USDT-SWAP", 67500.5, 0.3, "buy", 1714...)

Step 3: TradesWorker._on_trade() 路由
────────────────────────────────────
  accumulators["BTC-USDT-SWAP"].update(67500.5, 0.3, "buy")

Step 4: TradesAccumulator.update() 累积 (O(1), 锁内)
────────────────────────────────────
  _sum_pv += 67500.5 * 0.3  = 20250.15
  _sum_v  += 0.3
  _buy_vol += 0.3
  _open = 67500.5  (如果是第一笔)
  _high = max(_high, 67500.5)
  _low  = min(_low, 67500.5)
  _close = 67500.5
  _count += 1

  ... 5 秒内可能有几十~几百笔类似的 update ...

Step 5: ClockDriver 到达 wall-clock 边界 (如 :05)
────────────────────────────────────
  → 调用 trades_worker.flush()

Step 6: TradesWorker.flush() 快照并构造 payload
────────────────────────────────────
  for sym in symbols:
    snap = accumulator[sym].snapshot_and_reset()
    # snap = {"vwap":67502.3, "buy_vol":45.2, "sell_vol":38.1,
    #         "trade_count":83, "open":67498.0, "high":67510.0,
    #         "low":67495.5, "close":67505.0, "volume":83.3}

    round_bars[sym] = np.array([ts, 67498.0, 67510.0, 67495.5, 67505.0, 83.3, NaN, NaN])
    round_trades[sym] = snap

  bar_queue.put((round_bars, round_trades))

Step 7: Engine._runtime_loop() 消费
────────────────────────────────────
  round_bars, round_trades = bar_queue.get()

  for sym, bar in round_bars.items():
    close  = bar[4]  = 67505.0   ← 来自 trades 自建
    volume = bar[5]  = 83.3      ← 来自 trades 自建
    open_  = bar[1]  = 67498.0
    high   = bar[2]  = 67510.0
    low    = bar[3]  = 67495.5
    ret    = close / prev_close - 1   ← Engine 计算

    vwap     = round_trades[sym]["vwap"]     = 67502.3
    buy_vol  = round_trades[sym]["buy_vol"]  = 45.2
    sell_vol = round_trades[sym]["sell_vol"] = 38.1

    batch[sym] = rt.BarData(close, volume, open_, high, low, ret,
                            vwap, buy_vol, sell_vol)

Step 8: C++ InferenceEngine.push_bars(batch)
────────────────────────────────────
  → 所有 16 个因子 (0001~0010 + T0001~T0006) 并行计算
  → 输出写入 signal_deque

Step 9: 用户调用 engine.get_factor_outputs()
────────────────────────────────────
  → 返回 {"BTC-USDT-SWAP": {"0001": 0.023, ..., "T0001": -0.15, ...}}
```

---

## 7. 文件变更索引

### 新增文件

| 文件 | 说明 |
|------|------|
| `dataflow/clock_driver.py` | ClockDriver 统一时钟驱动组件 |
| `tests/dataflow/test_clock_driver.py` | ClockDriver 单元测试 (8 tests) |

### 修改文件

| 文件 | 变更内容 |
|------|---------|
| `dataflow/okx/trades/accumulator.py` | 新增 OHLCV 字段: `_open/_high/_low/_close`, `update()` 跟踪, `snapshot_and_reset()` 返回扩展 |
| `dataflow/okx/trades/worker.py` | 新增 `flush()` 方法、`set_freq()`、`bar_queue` 参数、`flush_count` 属性 |
| `factorengine/engine.py` | 新增 `_clock_driver` 生命周期; `factor_group` 支持 `list[str]`; trades-only 模式完整 wiring; 移除 Phase 2 stub 警告 |
| `examples/engine/engine_rt_trades.py` | 更新为 `factor_group=["okx_perp", "okx_perp_trades"]` 多组示例 |
| `tests/trades/test_accumulator.py` | 新增 `TestOHLCV` 测试类 (5 tests), 更新已有测试覆盖 OHLCV 字段 |
| `tests/runtime_engine/test_engine_trades_integration.py` | 新增 `TestTradesWorkerFlush` (4 tests), `TestEngineTradesOnlyMode` (1 test), `test_engine_multi_group_param` |

---

## 8. 测试覆盖

```text
Phase 2 新增测试: 19 个
总测试数: 146 (全部通过)

覆盖范围:
├── TradesAccumulator OHLCV
│   ├── test_ohlcv_basic_sequence      — open/high/low/close 正确性
│   ├── test_ohlcv_monotonic_up        — 单调上涨序列
│   ├── test_ohlcv_monotonic_down      — 单调下跌序列
│   ├── test_ohlcv_single_trade        — 单笔: O=H=L=C
│   └── test_ohlcv_reset_between_buckets — 跨 bucket 重置
│
├── ClockDriver
│   ├── test_register_and_tick         — 回调在 2×freq 内触发
│   ├── test_multiple_callbacks        — 所有回调同步触发
│   ├── test_tick_count                — tick 计数正确
│   ├── test_stop_is_idempotent        — 多次 stop 不崩溃
│   ├── test_no_callbacks_warning      — 无回调时 warning
│   ├── test_callback_exception        — 异常不影响其他回调
│   ├── test_freq_property             — 属性访问
│   └── test_ticks_are_roughly_aligned — wall-clock 对齐 drift<100ms
│
├── TradesWorker.flush()
│   ├── test_flush_builds_bar_and_trades — BarPoint + trades 正确构建
│   ├── test_flush_resets_accumulators   — flush 后 accumulator 归零
│   ├── test_flush_without_bar_queue     — 无 queue 时不报错
│   └── test_flush_count_increments      — 计数递增
│
├── Engine 集成
│   ├── test_engine_multi_group_param    — factor_group=list 加载多组
│   └── test_trades_only_runtime_loop    — trades-only 全链路推理
```

---

## 9. 后续 Phase 3 路线图

Phase 2 完成后, `ClockDriver` 提供了统一的扩展接口。后续新数据流接入只需:

```text
Phase 3: 新数据流接入
─────────────────────

1. 实现 Worker (e.g. OrderbookWorker)
   - 包含 accumulator + WS collector
   - 暴露 flush() 方法

2. 在 Engine.__init__ 中注册:
   self._clock_driver.register(self._orderbook_worker.flush)

3. 完成! 零侵入扩展。

接入路径:
─────────
Phase 3a: clock.register(orderbook_worker.flush)
Phase 3b: clock.register(funding_worker.flush)
Phase 3c: clock.register(liquidation_worker.flush)
```

**架构全景 (Phase 3 展望)**:

```text
                         OKX WebSocket
          ┌──────────────────┼──────────────────┐
          │                  │                  │
     trades WS          orderbook WS       funding API
          │                  │                  │
  TradesAccumulator   OBAccumulator     FundAccumulator
          │                  │                  │
          └──────────┬───────┴──────────────────┘
                     │
              ClockDriver (freq=5s)
                每 5s 统一触发:
                  → trades.flush()
                  → orderbook.flush()
                  → funding.flush()
                     │
              bar_queue  ← 统一的 (round_bars, round_extras)
                     │
              Engine._runtime_loop()
                     │
              InferenceEngine (C++)
                     │
              signal_deque → 用户消费
```

---

## 附录: 关键设计决策速查

| 决策 | 选择 | 理由 |
|------|------|------|
| flush 驱动方式 | ClockDriver (独立组件) | 可扩展, 统一时钟, 新数据流一行接入 |
| Accumulator OHLCV | 从 trades 自建 | 单 WS 全功能, 频率不受 OKX candle 频道限制 |
| factor_group 类型 | `str \| list[str]` | 向后兼容, 支持混合因子组 |
| bar_queue 传递给谁 | trades-only: TradesWorker; kline_trades: KlinePublisher | 职责明确, 避免双写 |
| ClockDriver 线程模型 | 独立 daemon thread + asyncio | 与 TradesWorker 解耦, 各自独立生命周期 |
