# TradesWorker 工作流

## 概览

TradesWorker 是 OKX 实时逐笔成交数据的采集和聚合模块，运行在一个独立的 daemon 线程中。它负责从 OKX 公共 WebSocket 接收每一笔成交，实时更新各 symbol 的流式统计量（VWAP、买卖量、OHLCV），并在 ClockDriver 调用 `flush()` 时原子地快照并重置所有累加器。

**TradesWorker 不知道 kline、orderbook 等其他数据源的存在，也不直接推送数据到 `bar_queue`。**

## 架构位置

```
Engine._init_live()
  │
  ├── KlineWorker
  │     └── flush() ──→ ClockDriver
  │
  ├── TradesWorker ◄── 本文档
  │     └── flush() ──→ ClockDriver
  │
  └── ClockDriver
        └── _aggregate() → bar_queue → factor-infer 线程
```

## 内部组件

TradesWorker 内部串联了两个组件：

```
OKX WebSocket (trades 频道)
    │
    v
┌─────────────────────────────────────────────────────┐
│  OKXTradesCollector  (collector.py)                   │
│  - 连接 OKX Public WebSocket                          │
│  - 订阅 trades 频道，每个连接最多 200 个 symbol        │
│  - JSON 解析，对每笔成交调用                           │
│    on_trade(symbol, px, sz, side, ts_ms)              │
│  - 自动重连                                           │
└──────────────────────────┬────────────────────────────┘
                           │ on_trade(symbol, px, sz, side, ts_ms)
                           v
┌─────────────────────────────────────────────────────┐
│  TradesAccumulator  (accumulator.py)  × N 个 symbol   │
│  - 每个 symbol 一个独立实例                            │
│  - O(1) 流式更新：                                    │
│    · VWAP:  Σ(px × sz) / Σ(sz)                       │
│    · buy_vol:  Σ(sz) where side="buy"                │
│    · sell_vol: Σ(sz) where side="sell"               │
│    · OHLCV: open/high/low/close/volume               │
│    · trade_count                                     │
│  - snapshot_and_reset(): 原子读取 + 清零               │
│  - threading.Lock 保证线程安全                         │
└─────────────────────────────────────────────────────┘
```

### 与 KlineWorker 的关键区别

| 维度 | KlineWorker | TradesWorker |
|------|-------------|--------------|
| 数据源 | OKX Business WS (`candle*` 频道) | OKX Public WS (`trades` 频道) |
| 数据频率 | 每秒/每分钟推一条 candle | 每笔成交实时推送（BTC 高峰 ~50 tps） |
| 内部缓冲 | Adapter 聚合 + Publisher round 批次 | 无缓冲，逐笔累加到 Accumulator |
| flush 输出 | `dict[str, ndarray]` (仅 bars) | `(dict[str, ndarray], dict[str, dict])` (bars + trades 详情) |
| 确认机制 | 只处理 `confirm="1"` 的 candle | 每条消息都是已完成的成交，无确认概念 |

## 数据格式

每一步处理的数据格式：

| 阶段 | 格式 | 示例 |
|------|------|------|
| OKX 原始 | JSON dict | `{"instId": "BTC-USDT-SWAP", "px": "67050.0", "sz": "0.5", "side": "buy", "ts": "1714704000123"}` |
| Collector 回调 | 5 个参数 | `on_trade("BTC-USDT-SWAP", 67050.0, 0.5, "buy", 1714704000123)` |
| Accumulator 状态 | 内部 float 累加器 | `_sum_pv=33525.0, _sum_v=0.5, _buy_vol=0.5, _open=67050.0, ...` |
| flush() 输出 bar | `np.ndarray(8,)` float64 | `[ts, open, high, low, close, volume, NaN, NaN]` |
| flush() 输出 trades | `dict` | `{"vwap": 67050.0, "buy_vol": 0.5, "sell_vol": 0.0, "trade_count": 1, "open": 67050.0, ...}` |

## 文件说明

### `collector.py` — OXKTradesCollector

WebSocket 客户端，职责单一：

- 连接 `wss://ws.okx.com:8443/ws/v5/public`（注意：是 public 端点，不是 business）
- 订阅 `trades` 频道，每个连接最多 200 个 symbol
- 收到 JSON 推送后解析，对每条成交记录调用 `on_trade(symbol, px, sz, side, ts_ms)`
- 自动处理断线重连（3s 间隔重试）
- 防御性解析：格式错误的记录仅 debug log，不会崩溃

Collector 不做任何聚合运算，这些都交给 Accumulator。

### `accumulator.py` — TradesAccumulator

每个 symbol 一个实例的 O(1) 流式聚合器：

**热路径** — `update(px, sz, side)`（WS 线程调用，每笔成交一次）：
- VWAP 累加：`_sum_pv += px * sz`，`_sum_v += sz`
- 买卖分量：根据 `side` 累加到 `_buy_vol` 或 `_sell_vol`
- OHLCV 追踪：
  - `open` = 第一笔成交的价格（`_count == 0` 时设置）
  - `high` = 所有价格的 max
  - `low` = 所有价格的 min
  - `close` = 最后一笔成交的价格
  - `volume` = `_sum_v`（总成交量）

**冷路径** — `snapshot_and_reset()`（ClockDriver 线程调用，每 N 秒一次）：
- 持锁读取所有字段，构建 dict 返回
- 立即重置所有字段为初始值（NaN / 0 / ±inf）
- 一次操作同时完成"读取"和"清零"，**没有数据丢失窗口**

**空桶 forward-fill**（见下方专题说明）：
- 如果本桶内没有任何成交（`trade_count == 0`），但之前有过成交，
  OHLC 和 vwap 全部 forward-fill 为 `_last_close`，volume / buy_vol / sell_vol = 0
- 如果从未有过成交（首次 snapshot），所有价格字段返回 NaN

**线程安全**：`threading.Lock` 保护所有状态修改。`update()` 和 `snapshot_and_reset()` 互斥访问，但锁的持有时间极短（几个 float 加法），对 WS 线程的延迟影响可忽略。

### `worker.py` — TradesWorker

编排 Collector 和 Accumulator 的胶水层：

- `__init__()` → 为每个 symbol 创建一个 `TradesAccumulator`
- `start()` → 创建 daemon 线程，启动 asyncio event loop 运行 Collector
- `_on_trade()` → Collector 的回调：根据 symbol 路由到对应的 Accumulator
- `flush()` → 遍历所有 symbol，逐个 `snapshot_and_reset()`，构建 `(round_bars, round_trades)` 返回
- `stop()` → 停止 Collector、取消 asyncio tasks、join 线程

**flush() 的 bar 构建**：从 trades 数据自建 OHLCV，生成与 kline 格式相同的 `BarPoint ndarray(8,)`：
```
[wall_clock_ts, open, high, low, close, volume, NaN, NaN]
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                全部来自 accumulator 的 trades 数据
```

最后两个字段（vol_ccy, vol_ccy_quote）填 NaN，因为 trades 数据无法提供这些信息。

## 线程模型

```
okx-trades 线程 (daemon)
  │
  │  asyncio event loop
  │    └── OKXTradesCollector.run()
  │          └── on_trade 回调 (同步)
  │                └── accumulator.update(px, sz, side)
  │                      └── 持 _lock，O(1) 累加
  │
  ╎ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
  │
clock-driver 线程 (每 N 秒一次)
  │
  └── trades_worker.flush()
        └── 遍历所有 symbol:
              accumulator.snapshot_and_reset()
              └── 持 _lock，读取 + 清零
        └── 构建 (round_bars, round_trades) 返回
```

两个线程共享 Accumulator 状态，通过 `threading.Lock` 互斥：
- WS 线程：高频写入（每笔成交调一次 `update()`）
- ClockDriver 线程：低频读取（每 N 秒调一次 `snapshot_and_reset()`）

## flush() 输出与 ClockDriver 的交互

`flush()` 返回一个元组 `(round_bars, round_trades)`：

```python
round_bars = {
    "BTC-USDT-SWAP": ndarray([ts, open, high, low, close, vol, NaN, NaN]),
    "ETH-USDT-SWAP": ndarray([ts, open, high, low, close, vol, NaN, NaN]),
}
round_trades = {
    "BTC-USDT-SWAP": {"vwap": 67050.5, "buy_vol": 12.3, "sell_vol": 8.7, "trade_count": 142, ...},
    "ETH-USDT-SWAP": {"vwap": 3520.1,  "buy_vol": 45.0, "sell_vol": 38.2, "trade_count": 89, ...},
}
```

ClockDriver 拿到后在 `_aggregate()` 中处理：
- **`kline_trades` 模式**：kline bar 优先（交易所确认的 OHLCV），trades bar 只补充 kline 缺失的 symbol；`round_trades` 的 vwap/buy_vol/sell_vol 合入所有 symbol
- **`trades` 模式**：trades bar 就是唯一的 bar 数据源

## 时间戳对齐

`flush()` 中的时间戳按 wall-clock 边界对齐：

```python
ts_ms = time.time() * 1000
if self._freq_ms > 0:
    ts_ms = ts_ms - (ts_ms % self._freq_ms)
```

例如 `freq=5s` 时，flush 在 `12:00:05.023` 发生，时间戳对齐为 `12:00:05.000`。这保证 trades 和 kline 的 bar 有一致的时间基准。

## 空桶 Forward-Fill 机制

### 问题

在高频场景（如 `data_freq="1s"`），流动性较低的 symbol（如 SOL）可能在某些秒内**没有任何成交**。
如果 `snapshot_and_reset()` 直接返回 NaN，会导致：

1. **NaN 进入 C++ 推理引擎** — OHLCV 全是 NaN，因子的滚动窗口被污染
2. **ret 计算毒化** — `Engine._runtime_loop` 中 `prev_close = NaN` → 后续 bar 的 `ret` 也是 NaN
3. **因子长期为零** — `TS_ZSCORE`/`MA`/`TS_STD` 等 rolling 算子遇到 NaN 后输出异常，表现为因子值全为 0

实际观测：1s 粒度下 SOL 的 T0002/T0004/T0006（基于 vwap）长期为 0.0000，就是因为 vwap = NaN 污染了 60 窗口的滚动统计。

### 解决方案

`TradesAccumulator` 维护一个 `_last_close` 字段，记录最近一次有成交的 close 价格。
当 `trade_count == 0` 且 `_last_close` 已知时，forward-fill 所有价格字段：

```
有成交的桶:
  snapshot_and_reset() → 正常返回实际的 OHLCV + vwap
  _last_close ← close  (记住这个价格)

空桶 (trade_count == 0, 有历史):
  open  = _last_close   (价格不变)
  high  = _last_close
  low   = _last_close
  close = _last_close
  vwap  = _last_close   (无成交 = 价格不变 = VWAP 不变)
  volume   = 0
  buy_vol  = 0
  sell_vol = 0

首次空桶 (从未有过成交):
  所有价格字段 = NaN    (无法 forward-fill)
```

### 语义正确性

| 字段 | Forward-fill 值 | 为什么合理 |
|------|-----------------|-----------|
| OHLC | last_close | 没成交 = 价格不变，O=H=L=C=上次收盘价 |
| vwap | last_close | 没有新成交，volume-weighted 价格维持上次水平 |
| volume | 0 | 确实没有成交量 |
| buy/sell_vol | 0 | 确实没有买卖量 |
| trade_count | 0 | 下游可以用此字段区分"真实数据"和"forward-fill" |

### 对下游因子的影响

| 因子类型 | 空桶时的表现 |
|----------|------------|
| vwap 因子 (T0002/T0004/T0006) | vwap = last_close → rolling 窗口正常，不被 NaN 污染 |
| buy/sell 因子 (T0001/T0003/T0005) | buy_vol=0, sell_vol=0 → T0001 imbalance=0/0 → C++ 保护返回 0；T0003 slog1p(0)=0 正常 |
| ret (收益率) | close 不变 → ret=0，符合"无成交=无价格变动"语义 |

### Engine 侧的防御

除了 Accumulator 的 forward-fill，`Engine._runtime_loop` 也做了 NaN 守护：
- `close` 或 `prev_close` 为 NaN 时，`ret` 强制为 0.0
- 只在 `close` 非 NaN 时更新 `_prev_closes`，防止 NaN 毒化后续 bar

## 监控指标

通过 `worker.stats()` 获取：

| 指标 | 含义 |
|------|------|
| `trade_count` | 收到并处理的成交总数 |
| `flush_count` | ClockDriver 调用 flush() 的次数 |
| `ws_msg_count` | WebSocket 消息总数（一条消息可能包含多笔成交） |
| `symbols` | 订阅的 symbol 数量 |

`trade_count` 持续增长说明 WS 连接正常；`flush_count` 应等于 ClockDriver 的 `tick_count`。

## 与 KlineWorker 的设计对比

```
KlineWorker:
  WS → Collector → Adapter(聚合) → Publisher(round批次) → buffer
                   ~~~~~~~~~~~~~~   ~~~~~~~~~~~~~~~~~~~~
                   需要聚合1s→Ns      需要等所有symbol到齐
  flush(): 返回一个 ready round

TradesWorker:
  WS → Collector → Accumulator(逐笔累加)
                   ~~~~~~~~~~~~~~~~~~~~~~
                   没有聚合步骤，没有 round 概念
                   每笔 trade 直接更新累加器
  flush(): 快照所有累加器，自建 BarPoint
```

KlineWorker 复杂在于**数据到达是离散的、需要等批次完成**；TradesWorker 简单在于**数据是连续的、只需累加和快照**。
