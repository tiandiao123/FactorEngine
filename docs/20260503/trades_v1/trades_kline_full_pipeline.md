# OKX Dataflow → FactorEngine 全流程讲解 (kline_trades 模式)

> 日期: 2026-05-03
> 版本: Phase 1 完成后
> 适用代码: `dataflow/okx/`, `factorengine/`, `native/`

---

## 1. 架构总览

```text
                              OKX WebSocket
                     ┌────────────┴────────────┐
                     │                         │
              business WS                public WS
              (candle1s)                 (trades)
                     │                         │
          ┌──────────▼──────────┐    ┌─────────▼──────────┐
          │   OKXKlineCollector │    │  OXKTradesCollector │
          │  (asyncio, aiohttp) │    │  (asyncio, aiohttp) │
          └──────────┬──────────┘    └─────────┬──────────┘
                     │                         │
              on_candle()                on_trade()
                     │                         │
          ┌──────────▼──────────┐    ┌─────────▼──────────┐
          │    KlineAdapter     │    │  TradesAccumulator  │
          │  (1s→Ns 聚合对齐)   │    │  (per-symbol, O(1)) │
          └──────────┬──────────┘    └─────────┬──────────┘
                     │                         │
              aggregated bar              accumulators dict
                     │                         │
          ┌──────────▼─────────────────────────┘
          │       KlinePublisher
          │    _flush_round():
          │      1. 攒批 round_bars
          │      2. snapshot_and_reset() accumulators
          │      3. bar_queue.put((round_bars, round_trades))
          └──────────┬──────────────────────────
                     │
              bar_queue (Queue, size=16)
                     │
          ┌──────────▼──────────┐
          │  Engine._runtime_loop │   ← factor-infer 线程
          │   unpack tuple        │
          │   build BarData       │
          │   push_bars (C++)     │
          └──────────┬──────────┘
                     │
          ┌──────────▼──────────┐
          │  InferenceEngine    │   ← C++ thread pool
          │   per-symbol并行    │
          │   FactorGraph.push_bar()
          └──────────┬──────────┘
                     │
              signal_deque (maxlen=3)
                     │
          ┌──────────▼──────────┐
          │    策略 / 主线程     │
          │  get_factor_outputs()│   ← O(1), 无计算
          └─────────────────────┘
```

### 线程模型 (kline_trades 模式, 4 线程)

| 线程名 | 职责 | 关键组件 |
|--------|------|----------|
| `okx-kline` | kline WS 收集 + 聚合 + 攒批 | KlineCollector → KlineAdapter → KlinePublisher |
| `okx-trades` | trades WS 收集 + 累加 | OXKTradesCollector → TradesAccumulator |
| `factor-infer` | 拆包 → C++ push_bars → 写 signal_deque | Engine._runtime_loop |
| main thread | 策略消费 | get_factor_outputs() |

---

## 2. 数据流逐层详解

### 2.1 第一层: OKX WebSocket 收集

#### Kline 收集器

```python
# dataflow/okx/kline/collector.py
class OKXKlineCollector:
    async def _connect_and_listen(self, session, symbols):
        async with session.ws_connect(OKX_WS_BUSINESS, heartbeat=20) as ws:
            # 订阅 candle1s 频道
            args = [{"channel": "candle1s", "instId": sym} for sym in symbols]
            for batch in chunk(args, SUBS_BATCH_SIZE):
                await ws.send_json({"op": "subscribe", "args": batch})
            # 每条消息 → on_candle(symbol, candle_list)
```

OKX 每秒推送一条 1s candle, 格式: `[ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote, confirm]`。
只有 `confirm="1"` 的才是完成的 candle, 未确认的被丢弃。

#### Trades 收集器

```python
# dataflow/okx/trades/collector.py
class OXKTradesCollector:
    async def _connect_and_listen(self, session, symbols):
        async with session.ws_connect(OKX_WS_PUBLIC, heartbeat=20) as ws:
            args = [{"channel": "trades", "instId": sym} for sym in symbols]
            # 每条消息可能包含多笔 trade → 逐笔回调 on_trade(symbol, px, sz, side, ts_ms)
```

trades 频道使用 **public** WS (不同于 kline 的 business WS), 每笔成交实时推送。

**两者的关键区别:**
- kline 有 confirm 概念 (1s 后确认), trades 没有 (每笔即确认)
- kline 数据量固定 (每秒每 symbol 一条), trades 不确定 (BTC 高峰期每秒 20+ 笔)

### 2.2 第二层: 数据聚合

#### KlineAdapter — 时间对齐聚合

当 `data_freq > 1s` 时, 需要将连续的 1s candle 聚合成 N 秒 bar:

```python
# dataflow/okx/kline/adapter.py
def _aggregate(self, symbol, bar):
    ts_ms = int(bar[0])
    win_start = ts_ms - (ts_ms % self._freq_ms)   # 对齐到窗口边界
    # ...
    buf.append(bar)
    if len(buf) < self._freq:      # 攒满 N 根
        return None
    merged = self._merge(buf, win_start)
    buf.clear()
    return merged                   # 输出一根 N 秒 bar

def _merge(buf, aligned_ts):
    return np.array([
        aligned_ts,                 # ts   = 窗口起始时间 (对齐)
        buf[0][1],                  # open = 第一根 candle 的 open
        max(b[2] for b in buf),     # high = 所有 candle 的 max
        min(b[3] for b in buf),     # low  = 所有 candle 的 min
        buf[-1][4],                 # close = 最后一根的 close
        sum(b[5] for b in buf),     # vol  = 累加
        sum(b[6] for b in buf),     # vol_ccy
        sum(b[7] for b in buf),     # vol_ccy_quote
    ])
```

**为什么要对齐?** 如果 BTC 从 ts=0 开始攒, ETH 从 ts=1 开始攒, 5 秒后 BTC 输出 ts=0 的 bar, ETH 输出 ts=1 的 bar — 时间戳不同, `KlinePublisher` 无法将它们放入同一轮。对齐到 `ts - (ts % freq_ms)` 保证所有 symbol 的 N 秒 bar 共享相同的窗口起始时间。

#### TradesAccumulator — 流式 O(1) 聚合

```python
# dataflow/okx/trades/accumulator.py
class TradesAccumulator:
    def update(self, px, sz, side):           # 热路径: trades WS 线程
        with self._lock:                      # 极短临界区
            self._sum_pv += px * sz           # Σ(price × size) → 算 VWAP
            self._sum_v += sz                 # Σ(size)
            if side == "buy":
                self._buy_vol += sz
            elif side == "sell":
                self._sell_vol += sz
            self._count += 1

    def snapshot_and_reset(self):              # 冷路径: flush 线程
        with self._lock:                      # 原子读取 + 清零
            vwap = self._sum_pv / self._sum_v if self._sum_v > 0 else NaN
            result = {"vwap": vwap, "buy_vol": ..., "sell_vol": ..., "trade_count": ...}
            self._sum_pv = 0.0                # 重置, 开始下一个桶
            # ... 所有字段清零
            return result
```

**线程安全设计:** `update()` 和 `snapshot_and_reset()` 都在 `self._lock` 下执行。`update()` 每次只做几次加法, 临界区极短 (< 1μs)。`snapshot_and_reset()` 每个 bar 周期只调一次。两者竞争几乎为零。

**为什么用 `snapshot_and_reset()` 而不是分开的 `snapshot()` + `reset()`?**
如果分开调用, 在 `snapshot()` 和 `reset()` 之间会有新的 trades 到达并被累加, `reset()` 会把这些新数据一起清掉 — 数据丢失。原子操作保证了完整性。

### 2.3 第三层: TradesWorker — 线程管理

```python
# dataflow/okx/trades/worker.py
class TradesWorker:
    def __init__(self, symbols):
        # 为每个 symbol 预创建一个 accumulator
        self._accumulators = {sym: TradesAccumulator() for sym in symbols}

    def _on_trade(self, symbol, px, sz, side, ts_ms):
        acc = self._accumulators.get(symbol)
        if acc is not None:
            acc.update(px, sz, side)      # 路由到对应 symbol 的 accumulator

    def start(self):
        # 在独立的 daemon thread 里跑 asyncio event loop
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="okx-trades")
        self._thread.start()

    async def _run(self):
        collector = OXKTradesCollector(symbols=self.symbols, on_trade=self._on_trade)
        await collector.run()             # 阻塞, 持续收集
```

**关键:** TradesWorker **不负责 flush**。它只负责:
1. 启动 `okx-trades` 线程
2. 运行 `OXKTradesCollector`
3. 把每笔 trade 路由到对应 symbol 的 `TradesAccumulator`

flush 的决定权在 `KlinePublisher` (kline_trades 模式) 或 `ClockDriver` (trades-only 模式, Phase 2)。

### 2.4 第四层: KlinePublisher — 撮合点 (核心改动)

这是 kline 数据和 trades 数据的**汇合点**:

```python
# dataflow/okx/kline/publisher.py
class KlinePublisher:
    def __init__(self, ..., accumulators=None):
        self._accumulators = accumulators     # Engine 传入的 {symbol: TradesAccumulator}

    def _flush_round(self):
        # 1. 攒批 kline bars (forward-fill 缺失 symbol)
        round_bars = dict(self._pending_round)
        if self._last_round is not None:
            for sym in self._expected_symbols:
                if sym not in round_bars:
                    round_bars[sym] = self._last_round[sym].copy()  # forward-fill

        # 2. 快照 trades accumulators (如果有的话)
        round_trades = None
        if self._accumulators:
            round_trades = {}
            for sym in round_bars:
                acc = self._accumulators.get(sym)
                if acc is not None:
                    round_trades[sym] = acc.snapshot_and_reset()
                    # ↑ 原子读取 vwap/buy_vol/sell_vol + 清零

        # 3. 推入队列: tuple(round_bars, round_trades | None)
        payload = (round_bars, round_trades)
        self._bar_queue.put(payload, block=False)
```

**时间同步保证:** trades accumulator 从上一次 flush 到这一次 flush 之间累积的所有 trades, 正好对应这一根 kline bar 的时间窗口。不需要做时间戳匹配 — **flush 时间点本身就是对齐的边界**。

**payload 格式变更:**
```text
旧: bar_queue → dict[str, np.ndarray]                    # round_bars
新: bar_queue → tuple(dict[str, np.ndarray], dict | None) # (round_bars, round_trades)
```

向后兼容: 纯 kline 模式下 `round_trades` 为 `None`。

### 2.5 第五层: Engine._runtime_loop — 拆包与构造 BarData

```python
# factorengine/engine.py
def _runtime_loop(self):
    while not self._stop_event.is_set():
        payload = self._bar_queue.get(timeout=0.5)

        # 兼容新旧两种格式
        if isinstance(payload, tuple):
            round_bars, round_trades = payload
        else:
            round_bars, round_trades = payload, None     # 旧格式兼容

        batch = {}
        for sym, bar in round_bars.items():
            close  = float(bar[4])
            volume = float(bar[5])
            open_  = float(bar[1])
            high   = float(bar[2])
            low    = float(bar[3])
            ret    = close / prev_close - 1.0

            # 从 trades snapshot 提取特征
            vwap, buy_vol, sell_vol = NaN, NaN, NaN
            if round_trades:
                trades = round_trades.get(sym)
                if trades:
                    vwap     = trades.get("vwap", NaN)
                    buy_vol  = trades.get("buy_vol", NaN)
                    sell_vol = trades.get("sell_vol", NaN)

            # 构造 BarData (9 个字段)
            batch[sym] = rt.BarData(close, volume, open_, high, low, ret,
                                    vwap, buy_vol, sell_vol)

        self._inference.push_bars(batch)     # → C++ thread pool 并行推
```

**数据在这一步的转换:**

```text
Python ndarray [ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote]
     ↓ + trades snapshot {"vwap": float, "buy_vol": float, "sell_vol": float}
     ↓
C++ BarData {close, volume, open, high, low, ret, vwap, buy_vol, sell_vol}
```

### 2.6 第六层: C++ FactorGraph — DAG 推理

BarData 进入 C++ 层后, `InferenceEngine::push_bars()` 使用线程池并行处理每个 symbol:

```cpp
// native/include/fe/runtime/inference_engine.hpp
struct BarData {
    FeFloat close, volume, open, high, low, ret;
    FeFloat vwap     = NaN;    // ← 新增
    FeFloat buy_vol  = NaN;    // ← 新增
    FeFloat sell_vol = NaN;    // ← 新增
};

void push_bars(const unordered_map<string, BarData>& bars) {
    for (auto& [sym, bar] : bars) {
        pool_->submit([runner, b=bar, barrier] {
            runner->push_bar(b.close, b.volume, b.open, b.high, b.low, b.ret,
                             b.vwap, b.buy_vol, b.sell_vol);
            // ↑ 9 个字段全部透传到 FactorGraph
        });
    }
    // barrier wait...
}
```

在 `FactorGraph::push_bar()` 内部, 每个 Op 节点按拓扑序执行:

```cpp
// native/include/fe/runtime/factor_graph.hpp
void push_bar(FeFloat close, ..., FeFloat vwap, FeFloat buy_vol, FeFloat sell_vol) {
    for (int i = 0; i < n; ++i) {
        switch (nd.op) {
        case Op::INPUT_CLOSE:    values_[i] = close;     break;
        case Op::INPUT_VOLUME:   values_[i] = volume;    break;
        // ...
        case Op::INPUT_VWAP:     values_[i] = vwap;      break;  // ← 新增
        case Op::INPUT_BUY_VOL:  values_[i] = buy_vol;   break;  // ← 新增
        case Op::INPUT_SELL_VOL: values_[i] = sell_vol;   break;  // ← 新增

        case Op::MA:  { /* rolling mean kernel push + output */ break; }
        case Op::SUB: { values_[i] = a - b; break; }
        // ... 30+ ops
        }
    }
}
```

**关键特性:** 每个 DAG 节点独立。如果一个因子只用 `close` 和 `volume`, `vwap`/`buy_vol`/`sell_vol` 传入 NaN 也不会影响它 — NaN 值只存在于 `values_[]` 的对应位置, 不使用这些位置的节点完全不受影响。

### 2.7 第七层: 因子注册与使用

#### 传统 kline 因子 (okx_perp)

```python
# factorengine/factors/okx_perp/factor_bank.py
@register_factor("okx_perp", "0001")
def build_factor_0001():
    g = rt.FactorGraph()
    c = g.add_input("close")                 # 只用 kline 输入
    ma120 = g.add_rolling(Op.MA, c, 120)
    dev = g.add_binary(Op.SUB, c, ma120)
    vol = g.add_rolling(Op.TS_STD, c, 60)
    g.add_binary(Op.DIV, dev, vol)
    g.compile()
    return g
```

#### 新增 trades 因子 (okx_perp_trades)

```python
# factorengine/factors/okx_perp_trades/factor_bank.py
@register_factor("okx_perp_trades", "T0001")
def build_trade_imbalance():
    """买卖力量不平衡度 z-score"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")              # ← trades 输入
    sv = g.add_input("sell_vol")             # ← trades 输入
    diff = g.add_binary(Op.SUB, bv, sv)
    total = g.add_binary(Op.ADD, bv, sv)
    imbalance = g.add_binary(Op.DIV, diff, total)
    g.add_rolling(Op.TS_ZSCORE, imbalance, 120)
    g.compile()
    return g
```

#### 混合因子 (同时用 kline + trades)

```python
@register_factor("okx_perp_trades", "T0004")
def build_vwap_close_divergence():
    """VWAP 相对 close 的溢价 z-score"""
    g = rt.FactorGraph()
    vw = g.add_input("vwap")                 # ← trades 输入
    c = g.add_input("close")                 # ← kline 输入
    premium = g.add_binary(Op.DIV, g.add_binary(Op.SUB, vw, c), c)
    g.add_rolling(Op.TS_ZSCORE, premium, 60)
    g.compile()
    return g
```

当前已注册的 6 个 trades 因子:

| ID | 名称 | 输入 | 公式 |
|----|------|------|------|
| T0001 | Trade Imbalance | buy_vol, sell_vol | `TsZscore((bv-sv)/(bv+sv), 120)` |
| T0002 | VWAP Momentum | vwap | `(vwap - MA(vwap,60)) / TsStd(vwap,60)` |
| T0003 | Buy Volume Surge | buy_vol | `TsZscore(SLog1p(buy_vol), 120)` |
| T0004 | VWAP-Close Divergence | vwap, close | `TsZscore((vwap-close)/close, 60)` |
| T0005 | Net Volume Trend | buy_vol, sell_vol | `TsRank(EMA(bv-sv, 30), 120)` |
| T0006 | VWAP Mean Reversion | vwap | `Neg((EMA_fast - EMA_slow) / TsStd)` |

---

## 3. Engine 初始化与启动流程

### 3.1 初始化 (`Engine.__init__`)

```python
engine = Engine(
    symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    mode="live",
    data_freq="5s",
    factor_group="okx_perp_trades",
    data_source="kline_trades",        # ← 关键参数
)
```

执行顺序:

```text
1. parse_freq("5s") → 5
2. _init_inference("okx_perp_trades", 4)
   → FactorRegistry.load_group("okx_perp_trades")
   → 为每个 symbol 注册 T0001-T0006 的 FactorGraph
   → InferenceEngine(num_threads=4)
3. bar_queue = Queue(maxsize=16)
4. data_source="kline_trades" →
   → TradesWorker(symbols)            # 创建, 不启动
   → accumulators = trades_worker.accumulators
   → KlineWorker(symbols, bar_queue, freq=5, accumulators=accumulators)
     ↑ accumulators 注入到 KlinePublisher
```

### 3.2 启动 (`Engine.start()`)

```text
1. factor-infer 线程启动 → _runtime_loop() 开始 bar_queue.get() 循环
2. trades_worker.start()  → okx-trades 线程启动, WS 连接, 开始收集
3. kline_worker.start()   → okx-kline 线程启动, WS 连接, 开始收集
```

### 3.3 稳态运行

每 5 秒一个周期:

```text
t=0s   第 1 根 1s candle 到达, KlineAdapter 缓存
t=1s   第 2 根 1s candle
t=2s   第 3 根 1s candle
t=3s   第 4 根 1s candle
t=4s   第 5 根 1s candle → KlineAdapter 输出一根 5s bar
       ↓
       KlinePublisher.publish(sym, bar_5s)
       ↓
       所有 symbol 的 5s bar 到齐 (或超时)
       ↓
       _flush_round():
         round_bars = {sym: ndarray, ...}         ← kline 数据
         round_trades = {                          ← trades 数据
           sym: acc.snapshot_and_reset()            (这 5 秒内所有 trades 的聚合)
         }
         bar_queue.put((round_bars, round_trades))
       ↓
       factor-infer 线程:
         payload = bar_queue.get()
         → unpack → build BarData(9 字段) → push_bars() → C++ 并行推理
         → signal_deque.append(result)
       ↓
       主线程:
         engine.get_factor_outputs() → signal_deque[-1]
```

---

## 4. 数据在各层的格式变化

```text
层级                格式                                         示例
─────────────────────────────────────────────────────────────────────────
OKX WS (kline)     JSON list [ts,o,h,l,c,vol,vc,vcq,confirm]   ["1714...","100","102",...]
                        ↓ parse_confirmed_bar()
KlineAdapter       np.ndarray shape=(8,) dtype=float64          [1714..., 100, 102, 99, 101, 50, ...]
                        ↓ _merge(buf, aligned_ts)
KlinePublisher     dict[sym → ndarray]                          {"BTC": array([0, 100, 102, ...])}
─────────────────────────────────────────────────────────────────────────
OKX WS (trades)    JSON {"px":"100.5","sz":"0.1","side":"buy"}
                        ↓ on_trade()
TradesAccumulator  内部状态: sum_pv, sum_v, buy_vol, sell_vol
                        ↓ snapshot_and_reset()
round_trades       dict[sym → dict]                             {"BTC": {"vwap": 100.3, ...}}
─────────────────────────────────────────────────────────────────────────
bar_queue          tuple(round_bars, round_trades)
                        ↓ _runtime_loop unpack
BarData (C++)      struct {close, volume, open, high, low,      BarData{101, 50, 100, 102, 99,
                          ret, vwap, buy_vol, sell_vol}                  0.01, 100.3, 30, 20}
                        ↓ FactorGraph.push_bar()
Factor output      float                                        0.4523
                        ↓ get_all_outputs()
signal_deque       dict {ts, bar_index, factors:{sym:{fid:val}}}
```

---

## 5. 关键设计决策

### 5.1 为什么 trades 不自己 flush?

TradesWorker 只负责收集和累加, 不负责决定"什么时候算一个 bar"。原因:

1. **时间同步**: kline bar 的时间边界由 OKX 服务端定义 (confirm=1), 是权威时间源。trades 按同一边界切分才能保证 kline 和 trades 数据对应同一个时间窗口。
2. **单点 flush**: 如果 trades 和 kline 各自 flush, 需要在下游再做一次时间对齐和合并 — 增加复杂度。
3. **扩展性**: 未来 `trades`-only 模式下, flush 由 `ClockDriver` 统一驱动, TradesWorker 的职责不变。

### 5.2 为什么 BarData 默认 NaN?

```cpp
struct BarData {
    FeFloat vwap     = NaN;    // 不是 0, 是 NaN
    FeFloat buy_vol  = NaN;
    FeFloat sell_vol = NaN;
};
```

- 如果默认 0, 纯 kline 因子中 `add_input("volume")` 返回正常值, 但 `add_input("buy_vol")` 返回 0 — 0 是一个有意义的值 (表示零成交), 会产生误导。
- NaN 让因子明确知道"这个数据不存在"。C++ DAG 中 NaN 会正确传播 (NaN + x = NaN), 不会产生错误信号。

### 5.3 向后兼容保证

| 场景 | 行为 |
|------|------|
| 旧代码不传 `data_source` | 默认 `"kline"`, 不创建 TradesWorker, 不传 accumulators |
| `round_trades` 为 None | vwap/buy_vol/sell_vol 全部填 NaN |
| 纯 kline 因子收到 NaN trades 字段 | 无影响 — 因子不引用 `INPUT_VWAP` 等节点 |
| C++ `add_input("vwap")` 在旧版 .so 上 | 抛 `std::invalid_argument` (之前的 fix) |

---

## 6. 文件索引

| 文件路径 | 职责 |
|----------|------|
| `dataflow/okx/trades/accumulator.py` | 单 symbol 流式聚合 (vwap, buy_vol, sell_vol) |
| `dataflow/okx/trades/collector.py` | OKX trades WS 收集器 |
| `dataflow/okx/trades/worker.py` | 线程管理, 路由 trades → accumulators |
| `dataflow/okx/kline/publisher.py` | 攒批 + 快照 accumulators + 推 bar_queue |
| `dataflow/okx/kline/worker.py` | 组装 collector + adapter + publisher |
| `dataflow/okx/kline/adapter.py` | 1s → Ns 时间对齐聚合 |
| `dataflow/okx/kline/collector.py` | OKX kline WS 收集器 |
| `factorengine/engine.py` | 顶层入口, 管理所有线程, 拆包构造 BarData |
| `factorengine/factors/okx_perp_trades/factor_bank.py` | 6 个 trades 因子定义 |
| `native/include/fe/runtime/inference_engine.hpp` | C++ BarData 定义 + push_bars |
| `native/include/fe/runtime/factor_graph.hpp` | C++ DAG 推理, Op 枚举, push_bar |
| `native/include/fe/runtime/symbol_runner.hpp` | 单 symbol 多因子 push_bar 透传 |
| `native/pybind/fe_runtime_bind.cpp` | pybind11 Python↔C++ 绑定 |

---

## 7. 测试覆盖

Phase 1 共 **123 个测试**, 按模块分布:

| 模块 | 测试文件 | 数量 |
|------|----------|------|
| kline adapter | `tests/dataflow/test_okx_kline_adapter.py` | 21 |
| kline publisher | `tests/dataflow/test_okx_kline_publisher.py` | 27 |
| kline worker | `tests/dataflow/test_okx_kline_worker.py` | 7 |
| kline e2e | `tests/dataflow/test_okx_kline_e2e.py` | 6 |
| **以上小计** | | **64** (含 3 个 accumulator 集成测试) |
| trades accumulator | `tests/trades/test_accumulator.py` | 17 |
| trades collector | `tests/trades/test_collector.py` | 6 |
| trades worker | `tests/trades/test_worker.py` | 6 |
| trades factors | `tests/trades/test_trades_factors.py` | 10 |
| **trades 小计** | | **39** |
| engine integration | `tests/runtime_engine/test_engine_integration.py` | 10 |
| engine trades | `tests/runtime_engine/test_engine_trades_integration.py` | 10 |
| **engine 小计** | | **20** |
