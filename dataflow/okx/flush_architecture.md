# Flush 架构：Worker ↔ ClockDriver ↔ Engine 的交互机制

## 核心设计原则

1. **Worker 只生产，不推送** — 每个 Worker 是独立的数据源，只在被调用 `flush()` 时返回数据，不知道 `bar_queue` 的存在
2. **ClockDriver 统一调度** — 唯一拥有 `bar_queue` 引用的组件，负责定时拉取、聚合、推送
3. **Worker 互不感知** — KlineWorker 不知道 TradesWorker 的存在，反之亦然；未来新增 OBWorker / FundingRateWorker 同理

## 全景时序图

以 `data_source="kline_trades"`, `freq=5s` 为例：

```
时间轴     okx-kline 线程         okx-trades 线程        clock-driver 线程         factor-infer 线程
─────────────────────────────────────────────────────────────────────────────────────────────────────

 :00.000                                                 ┌─ tick_loop: sleep
                                                         │  到下一个 5s 边界
 :00.1xx    WS candle BTC         WS trade BTC buy       │
 :00.2xx    WS candle ETH         WS trade ETH sell      │
 :00.5xx                          WS trade BTC sell      │
 :01.1xx    WS candle BTC         WS trade ETH buy       │
   ...       (持续接收)            (持续累加)              │
 :04.9xx    WS candle ETH                                │
                                                         │
 :05.000                                                 ├─ 边界到达! tick_count++
                                                         │
                                                         │  ① kline_worker.flush()
            ┌──────────────────┐                         │     └── publisher.flush_for_clock()
            │ 返回 ready_round │ ◄───────────────────────┤         取 _ready_round (有锁)
            │ {BTC: bar, ETH: bar}                       │         或 pending + forward-fill
            └──────────────────┘                         │
                                                         │
                                                         │  ② trades_worker.flush()
                                  ┌────────────────────┐ │     └── 遍历每个 symbol:
                                  │ 返回 (bars, trades)│ ◄┤         accumulator.snapshot_and_reset()
                                  │ BTC: {vwap, buy..} │  │         (有锁, 原子读+清零)
                                  │ ETH: {vwap, buy..} │  │
                                  └────────────────────┘  │
                                                         │
                                                         │  ③ _aggregate(raw)
                                                         │     kline bars 优先
                                                         │     trades bars 补缺
                                                         │     trades dict 全量保留
                                                         │
                                                         │  ④ bar_queue.put(
                                                         │       (merged_bars, merged_trades)
                                                         │     )
                                                         │                          ┌────────────────
                                                         └─ sleep 到 :10.000       │ bar_queue.get()
                                                                                    │ 构建 BarData batch
                                                                                    │ push_bars(batch)
                                                                                    │   └── C++ 并行推理
                                                                                    │ signal_deque.append()
                                                                                    └────────────────
```

## 三种 data_source 模式的 flush 差异

### `data_source="kline"`

```
Engine._init_live()
  └── 创建 KlineWorker
  └── 创建 ClockDriver
        └── register("kline", kline_worker)
        └── 不注册 trades

ClockDriver tick:
  kline_worker.flush() → dict[str, ndarray] | None
  _aggregate: merged_bars = kline_data, merged_trades = {}
  bar_queue.put( (merged_bars, None) )
```

最简单的情况。`round_trades` 始终为 `None`，`_runtime_loop` 中 vwap/buy_vol/sell_vol 填 NaN。

### `data_source="trades"`

```
Engine._init_live()
  └── 创建 TradesWorker
  └── 创建 ClockDriver
        └── register("trades", trades_worker)
        └── 不注册 kline

ClockDriver tick:
  trades_worker.flush() → (dict[str, ndarray], dict[str, dict])
  _aggregate: merged_bars = trades_bars, merged_trades = trades_snaps
  bar_queue.put( (merged_bars, merged_trades) )
```

没有 kline 数据源，OHLCV 全部来自 trades 自建（accumulator 追踪的 open/high/low/close/volume）。

### `data_source="kline_trades"`

```
Engine._init_live()
  └── 创建 KlineWorker
  └── 创建 TradesWorker
  └── 创建 ClockDriver
        └── register("kline", kline_worker)
        └── register("trades", trades_worker)

ClockDriver tick:
  kline_worker.flush()  → dict[str, ndarray] | None
  trades_worker.flush() → (dict[str, ndarray], dict[str, dict])
  _aggregate:
    1. merged_bars = kline_data   (kline 优先)
    2. trades_bars → 只补充 kline 中缺失的 symbol
    3. merged_trades = trades_snaps (全量保留)
  bar_queue.put( (merged_bars, merged_trades) )
```

最完整的模式。kline 提供可靠的 OHLCV，trades 提供附加特征（vwap, buy_vol, sell_vol）并兜底缺失的 symbol。

## FlushableWorker 协议

```python
@runtime_checkable
class FlushableWorker(Protocol):
    def flush(self) -> Any: ...
```

任何实现了 `flush()` 的对象都可以注册到 ClockDriver。不同 Worker 的返回类型不同：

| Worker | `flush()` 返回类型 | 含义 |
|--------|-------------------|------|
| KlineWorker | `dict[str, ndarray] \| None` | symbol → BarPoint（8 维），None 表示无数据 |
| TradesWorker | `tuple[dict[str, ndarray], dict[str, dict]]` | (bars, trades_snapshots) |
| FundingRateWorker (未来) | `dict[str, dict] \| None` | symbol → funding rate 快照 |

ClockDriver 的 `_aggregate()` 通过 worker 注册名（`"kline"`, `"trades"`, ...）识别返回类型并分别处理。

## _aggregate() 合并规则

```python
def _aggregate(self, raw: dict[str, Any]):
    merged_bars = {}
    merged_trades = {}

    # ① kline 优先写入 bars
    kline_data = raw.get("kline")
    if kline_data is not None:
        merged_bars.update(kline_data)

    # ② trades: trades_snaps 全量保留; trades_bars 只补缺
    trades_data = raw.get("trades")
    if trades_data is not None:
        trades_bars, trades_snaps = trades_data
        merged_trades.update(trades_snaps)
        for sym, bar in trades_bars.items():
            if sym not in merged_bars:   # kline 已有的不覆盖
                merged_bars[sym] = bar

    return merged_bars, merged_trades
```

**为什么 kline 优先？** 交易所确认的 OHLCV 比从 trades 自建的更可靠（trades 可能有数据丢失、WS 重连间隙等）。trades-derived bar 只在 kline 缺失时作为兜底。

## 数据在 bar_queue 中的格式

```python
payload = (merged_bars, merged_trades | None)

# merged_bars: dict[str, np.ndarray]
# 每个 ndarray 是 8 维: [ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote]

# merged_trades: dict[str, dict] | None
# 每个 dict: {"vwap": float, "buy_vol": float, "sell_vol": float,
#              "trade_count": int, "open": float, ...}
```

## Engine._runtime_loop 如何消费

```
bar_queue.get()
    │
    ▼
解包 (round_bars, round_trades)
    │
    ├── 遍历 round_bars:
    │     sym → bar ndarray
    │     提取 open, high, low, close, volume
    │     计算 ret = close / prev_close - 1
    │
    ├── 如果有 round_trades:
    │     sym → trades dict
    │     提取 vwap, buy_vol, sell_vol
    │
    ├── 组装 C++ BarData:
    │     BarData(close, volume, open, high, low, ret, vwap, buy_vol, sell_vol)
    │
    └── push_bars(batch)  →  C++ InferenceEngine 并行推理
         │
         └── signal_deque.append({ts, bar_index, factors})
```

## 线程边界与锁

整个 flush 链路跨越 4 个线程，下表总结各线程边界处的同步机制：

| 边界 | 写线程 | 读线程 | 同步方式 |
|------|--------|--------|----------|
| Accumulator 内部状态 | okx-trades (WS) | clock-driver | `threading.Lock` (per accumulator) |
| Publisher `_ready_round` | okx-kline (WS) | clock-driver | `threading.Lock` (`_ready_lock`) |
| Publisher `_pending_round` / `_last_round` | okx-kline (WS) | clock-driver | CPython GIL（dict/float 原子读写） |
| `bar_queue` | clock-driver | factor-infer | `queue.Queue`（内置锁） |
| `_trade_count` / `msg_count` | okx-trades (WS) | main thread (stats) | CPython GIL（int 原子读写，仅监控用） |

## 时间对齐保证

三个地方独立实现了 wall-clock 对齐，保证输出时间戳一致：

| 组件 | 对齐逻辑 | 作用 |
|------|----------|------|
| **ClockDriver** tick | `next_boundary = now - (now % freq_ms) + freq_ms` | 保证 flush 发生在 :00, :05, :10... |
| **KlineAdapter** 聚合窗口 | `window_start = ts - (ts % freq_ms)` | 保证 kline bar 的时间戳对齐到 5s/10s 边界 |
| **TradesWorker** flush | `ts_ms = ts_ms - (ts_ms % freq_ms)` | 保证 trades bar 的时间戳与 kline 一致 |

## 新 Worker 接入指南

要接入一种新数据源（如 funding rate），只需：

```python
# 1. 实现 FlushableWorker 协议
class FundingRateWorker:
    def flush(self) -> dict[str, dict] | None:
        """返回各 symbol 的 funding rate 快照"""
        ...

    def start(self): ...
    def stop(self): ...

# 2. 在 Engine._init_live() 中按 data_source 创建并注册
funding_worker = FundingRateWorker(symbols=symbols)
self._clock_driver.register("funding", funding_worker)

# 3. 在 ClockDriver._aggregate() 中添加处理逻辑
funding_data = raw.get("funding")
if funding_data is not None:
    merged_funding.update(funding_data)

# 4. 扩展 bar_queue payload 格式
payload = (merged_bars, merged_trades, merged_funding)
```

不需要修改任何已有 Worker 的代码 — 这就是 ClockDriver 统一调度的扩展性优势。
