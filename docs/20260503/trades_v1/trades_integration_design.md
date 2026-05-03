# Trades 数据集成设计文档 (v1)

> 状态: 设计中 (2026-05-03)
> 范围: `dataflow/okx/trades/` 新建 + `BarData` / `push_bar` 扩展 + Engine 接入

---

## 0. 背景与动机

当前 FactorEngine 的在线推理管线是 **纯 K 线驱动**:

```
OKX candle WS → collector → adapter → publisher → bar_queue
                                                      ↓
                            Engine._runtime_loop → push_bars(BarData{close,volume,open,high,low,ret})
                                                      ↓
                                               signal_deque (factor values)
```

`BarData`（`native/include/fe/runtime/inference_engine.hpp`）只有 6 个字段：
`close, volume, open, high, low, ret`。

因子库（`factorengine/factors/okx_perp/factor_bank.py`）目前只使用 `close`, `volume`,
`open`, `high`, `low` 作为 `add_input`。

**问题**: 做市和高频策略需要 trades 维度的微观结构信号（买卖量不对称、成交加权价、
realized volatility 等），现有管线无法表达。

**本文目标**: 把 **VWAP、buy_vol、sell_vol** 三个量从 trades 流聚合后，接入现有
factor DAG 推理路径，**最小侵入、分步实施**。

---

## 1. OKX Trades 数据源

### 1.1 WebSocket 频道

OKX 公共频道 `trades`（`wss://ws.okx.com:8443/ws/v5/public`）:

```json
{
  "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
  "data": [
    {
      "instId": "BTC-USDT-SWAP",
      "tradeId": "1234567",
      "px": "67123.4",
      "sz": "0.5",
      "side": "buy",
      "ts": "1714704000123"
    }
  ]
}
```

关键字段:

| 字段 | 类型 | 说明 |
|------|------|------|
| `px` | string (float) | 成交价 |
| `sz` | string (float) | 成交量 (张数) |
| `side` | `"buy"` / `"sell"` | **主动方** — OKX 直接给出, 不需要 tick rule |
| `ts` | string (epoch ms) | 成交时间 |

**重要**: OKX 在 `side` 字段里直接标明了 aggressor side, 不需要自行推导。

### 1.2 吞吐估算

高波动时 BTC-USDT-SWAP 单品种可达 **200–500 笔/秒**, 多品种 (10 个 SWAP)
峰值约 **2000–5000 笔/秒**。

---

## 2. 要聚合的数据项 (Phase 1)

Phase 1 只做三个量, 与 K 线同一时间桶对齐:

| 名称 | 定义 | 可增量? |
|------|------|---------|
| **`vwap`** | `Σ(px × sz) / Σ(sz)` | ✅ 维护 `sum_pv`, `sum_v` |
| **`buy_vol`** | `Σ sz  where side == "buy"` | ✅ 单计数器 |
| **`sell_vol`** | `Σ sz  where side == "sell"` | ✅ 单计数器 |

三个量全部支持 **O(1) 增量更新**, 适合流式聚合。

### 2.1 和 K 线的时间对齐

trades 聚合桶 **与 `data_freq` 的 K 线桶完全一致**: 桶边界由
`ts_ms % (freq_seconds * 1000) == 0` 定义（与 `KlineAdapter` 的 v2 对齐策略相同）。

例如 `data_freq="5s"`:
- K 线桶: `[10:00:00, 10:00:05)`, `[10:00:05, 10:00:10)`, ...
- trades 聚合桶: 同上

这样 **同一根 bar 的 OHLCV 和 trades 聚合量天然对齐**, 无需额外 join。

### 2.2 关于不同频段 / 多尺度

Phase 1 不做多尺度 (如 30s VWAP 和 1min buy_vol 共存)。
如果后续要做, 正确路径是 **多个聚合器实例 + 各自的频段**, 而非在同一
桶里混合不同时间窗。多尺度设计见 [附录 A](#附录-a-多尺度扩展方向)。

---

## 3. 聚合时机: 流式 vs batch

### 3.1 结论: **流式增量 + 桶边界输出**

```
每笔 trade 到达 (trades WS 线程):
    accumulator.update(px, sz, side)        # O(1), 持锁

当 K 线桶关闭 (kline 线程, KlinePublisher flush round):
    snap = accumulator.snapshot_and_reset() # 原子读取 + 清零, 持锁
    → 把 snap 附到 round_bars 上 → bar_queue
```

**不缓存原始 trades list, 不在 bar 关闭时批量计算**。

理由:
- Phase 1 三个量全部可增量, 没有「必须看完整列表」的量
- 避免内存尖峰 (高波动时单桶 500+ 笔)
- CPU 平滑: 不在 bar 边界集中做大量计算
- 与现有管线节奏一致: bar 边界 = round flush = push_bars

### 3.2 什么时候需要 batch?

如果后续加入 **桶内中位成交价 (median trade price)** 等不可增量统计,
则需要在桶内维护一个 trades 缓冲, 在 bar 关闭时做一次 batch 计算。
此时可以 **在同一个 accumulator 里加一个可选的 `_trade_buffer: list`**,
仅对需要 batch 的量启用, 增量量仍然 O(1) 更新。

---

## 4. 数据流架构

### 4.1 目标结构 (新增文件 `dataflow/okx/trades/`)

```
dataflow/okx/trades/
├── __init__.py
├── collector.py       # OKXTradesCollector — WS 连接 + JSON dispatch
├── accumulator.py     # TradesAccumulator  — 流式增量聚合 (per symbol)
└── worker.py          # TradesWorker       — 线程管理 + 与 KlineWorker 协调
```

### 4.2 组件职责

#### `OKXTradesCollector`

仿照 `OKXKlineCollector` 的模式:
- 连接 OKX 公共 WS (`wss://ws.okx.com:8443/ws/v5/public`)
- 订阅 `trades` 频道
- 回调: `on_trade(symbol: str, px: float, sz: float, side: str, ts_ms: int)`
- 自动重连逻辑复用 `OKXKlineCollector` 的 `_ws_loop` 模式

与 kline collector 的区别:
- 连接公共 WS (不是 business WS)
- 无 confirm 概念, 每条消息都是已成交

#### `TradesAccumulator`

**每个 symbol 一个实例**, 维护当前桶的增量状态:

```python
class TradesAccumulator:
    def __init__(self):
        self._lock = threading.Lock()
        self._sum_pv: float = 0.0     # Σ(px * sz)
        self._sum_v: float = 0.0      # Σ(sz)
        self._buy_vol: float = 0.0    # Σ(sz) where side == "buy"
        self._sell_vol: float = 0.0   # Σ(sz) where side == "sell"
        self._count: int = 0

    def update(self, px: float, sz: float, side: str):
        """每笔 trade 调用一次, O(1). 由 trades WS 线程调用."""
        with self._lock:
            self._sum_pv += px * sz
            self._sum_v += sz
            if side == "buy":
                self._buy_vol += sz
            elif side == "sell":
                self._sell_vol += sz
            # else: ignore unknown side
            self._count += 1

    def snapshot_and_reset(self) -> dict:
        """原子地读取当前聚合结果并清零. 由 kline 线程在 round flush 时调用."""
        with self._lock:
            vwap = self._sum_pv / self._sum_v if self._sum_v > 0 else float('nan')
            result = {
                "vwap": vwap,
                "buy_vol": self._buy_vol,
                "sell_vol": self._sell_vol,
                "trade_count": self._count,
            }
            self._sum_pv = 0.0
            self._sum_v = 0.0
            self._buy_vol = 0.0
            self._sell_vol = 0.0
            self._count = 0
            return result
```

> **设计要点 (修复竞态)**:
> - `update()` **不检测桶边界、不自动 reset** — 只做增量累加
> - 桶切换完全由 kline 线程通过 `snapshot_and_reset()` 驱动
> - 两者都持 `self._lock`, 保证原子性
> - 跨桶的少量 trades (差几十毫秒) 会被算进当前桶, 误差可忽略
> - `side` 校验改为 `elif`, 未知 side 静默忽略而非误算入 `sell_vol`

#### `TradesWorker`

- 管理一个守护线程 (类似 `KlineWorker`)
- 运行 asyncio 事件循环, 启动 `OKXTradesCollector`
- 回调中调用对应 symbol 的 `TradesAccumulator.update`
- **不自行触发 round flush** — 由 `KlinePublisher` / `Engine` 在 bar 边界统一读取

### 4.3 数据流全景 (Phase 1)

```
                  ┌── OKX candle WS ──────────────┐
                  │  collector → adapter            │
                  │                ↓                 │
                  │  KlinePublisher._flush_round()  │
                  │     ↓ 读 accumulator snapshot   │
Thread: okx-kline │     ↓ attach trades fields      │
                  │     ↓                            │
                  │  bar_queue.put(round_bars)       │
                  └─────────────────────────────────┘

                  ┌── OKX trades WS ────────────────┐
Thread: okx-trade │  collector → accumulator.update  │
                  │  (O(1) per trade, no queue push) │
                  └─────────────────────────────────┘

                  ┌── Engine._runtime_loop ──────────┐
Thread: factor    │  bar_queue.get() → build BarData  │
                  │  → InferenceEngine.push_bars()    │
                  │  → signal_deque                   │
                  └──────────────────────────────────┘
```

**关键设计**: trades WS 线程 **只做增量更新 accumulator**,
**不独立推队列**; kline 线程在 round flush 时 **读取 + reset** accumulator,
把 trades 聚合量附到 round_bars 上一起进队列。

这样:
- **时间对齐自动保证** — 同一个 round 的 bar 和 trades 数据同时 flush
- **队列仍然只有一条** — factor-infer 线程不需要合并两个队列
- **线程交互最小** — accumulator 需要简单的锁 (读写互斥), 但竞争低
  (trades 高频写 / kline 低频读, 用 spinlock 或短 mutex 即可)

---

## 5. C++ 侧扩展

### 5.1 `BarData` 扩展

当前 `BarData` (在 `inference_engine.hpp`):

```cpp
struct BarData {
    FeFloat close  = 0.0f;
    FeFloat volume = NaN;
    FeFloat open   = NaN;
    FeFloat high   = NaN;
    FeFloat low    = NaN;
    FeFloat ret    = NaN;
};
```

Phase 1 扩展:

```cpp
struct BarData {
    FeFloat close    = 0.0f;
    FeFloat volume   = NaN;
    FeFloat open     = NaN;
    FeFloat high     = NaN;
    FeFloat low      = NaN;
    FeFloat ret      = NaN;
    // ── trades-derived (Phase 1) ──
    FeFloat vwap     = NaN;
    FeFloat buy_vol  = NaN;
    FeFloat sell_vol = NaN;
};
```

### 5.2 `FactorGraph` 输入扩展

`factor_graph.hpp` 的 `Op` 枚举增加:

```cpp
enum class Op {
    INPUT_CLOSE, INPUT_VOLUME, INPUT_OPEN, INPUT_HIGH, INPUT_LOW, INPUT_RET,
    // Phase 1 trades inputs
    INPUT_VWAP, INPUT_BUY_VOL, INPUT_SELL_VOL,
    ...
};
```

`push_bar` 签名扩展:

```cpp
void push_bar(FeFloat close,
              FeFloat volume = NaN, FeFloat open = NaN,
              FeFloat high = NaN,   FeFloat low = NaN,
              FeFloat ret = NaN,
              FeFloat vwap = NaN,
              FeFloat buy_vol = NaN, FeFloat sell_vol = NaN);
```

`add_input("vwap")` / `add_input("buy_vol")` / `add_input("sell_vol")`
映射到对应的 `INPUT_VWAP` / `INPUT_BUY_VOL` / `INPUT_SELL_VOL`。

> **前置修复 (已完成)**: `add_input()` 对未知 feature 原来会静默降级为
> `INPUT_CLOSE`, 已改为抛出 `std::invalid_argument`. 这确保了在 Step 4
> 添加新 Op 之前, 任何 `add_input("vwap")` 调用会直接报错, 而不是静默
> 使用 close 计算出错误的因子值.

### 5.3 `InferenceEngine::push_bars` lambda 同步更新

⚠️ **关键**: `inference_engine.hpp` 的 `push_bars` 方法中, lambda 目前只转发 6 个字段:

```cpp
pool_->submit([runner, b, barrier] {
    runner->push_bar(b.close, b.volume, b.open, b.high, b.low, b.ret);
    //              ^^^^^^ 只有 6 个字段, 新字段不会传!
});
```

Step 4 扩展 `BarData` 后, **必须同步修改此 lambda**, 否则新字段永远是 NaN:

```cpp
pool_->submit([runner, b, barrier] {
    runner->push_bar(b.close, b.volume, b.open, b.high, b.low, b.ret,
                     b.vwap, b.buy_vol, b.sell_vol);
});
```

同理, `InferenceEngine::push_bar` 单 symbol 版本也需要转发新字段.

### 5.4 pybind11 绑定

`fe_runtime_bind.cpp` 对 `BarData`, `push_bar`, `FactorGraph.push_bar`
的绑定同步扩展, 新字段默认 NaN, **完全向后兼容**。

### 5.5 向后兼容

- 所有新字段默认 NaN → 不传就不影响现有因子
- 现有因子只 `add_input("close")` / `"volume"` 等 → 不碰新字段
- 现有测试不需要改（push_bar 的旧调用形式仍有效）

---

## 6. Engine 层接入

### 6.1 `Engine.__init__` 变更

```python
def __init__(self, ..., enable_trades: bool = False):
    ...
    self._trades_worker = None
    self._accumulators: dict[str, TradesAccumulator] = {}

    if mode == "live" and enable_trades:
        from dataflow.okx.trades.accumulator import TradesAccumulator
        from dataflow.okx.trades.worker import TradesWorker

        for sym in symbols:
            self._accumulators[sym] = TradesAccumulator(freq_seconds=self.data_freq_seconds)

        self._trades_worker = TradesWorker(
            symbols=symbols,
            accumulators=self._accumulators,
        )
```

### 6.2 `KlinePublisher` 改造

`KlinePublisher._flush_round` 增加一个可选的 accumulators 参数:

```python
def _flush_round(self) -> bool:
    round_bars: dict[str, np.ndarray] = dict(self._pending_round)
    ...
    # 如果有 trades accumulators, 原子读取并清零
    round_trades = None
    if self._accumulators:
        round_trades = {}
        for sym in round_bars:
            acc = self._accumulators.get(sym)
            if acc:
                round_trades[sym] = acc.snapshot_and_reset()
    ...
    # 推送 (round_bars, round_trades) 元组
    self._bar_queue.put((round_bars, round_trades), block=False)
```

`bar_queue` 载荷从 `dict[str, np.ndarray]` 变为 `(round_bars, round_trades)` 元组.
`round_trades` 为 `None` 时表示未启用 trades, `Engine._runtime_loop` 按需解包.

> **注意**: accumulators 应通过 `Engine → KlineWorker → KlinePublisher` 链路注入,
> 而不是 Engine 直接传给 Publisher, 以保持层级清晰.

### 6.3 `Engine._runtime_loop` 变更

```python
for sym, bar in round_bars.items():
    ...
    trades = round_trades.get(sym, {})
    vwap = trades.get("vwap", float('nan'))
    buy_vol = trades.get("buy_vol", float('nan'))
    sell_vol = trades.get("sell_vol", float('nan'))
    batch[sym] = rt.BarData(close, volume, open_, high, low, ret,
                            vwap, buy_vol, sell_vol)
```

### 6.4 Simulation mode

`BarGenerator` 可选生成假的 trades 聚合字段:
- `vwap ≈ (open + close) / 2 + noise`
- `buy_vol ≈ volume * (0.5 + noise)`
- `sell_vol = volume - buy_vol`

---

## 7. 新因子示例

有了 `vwap`, `buy_vol`, `sell_vol` 后可以写的因子:

```python
@register_factor("okx_perp", "T001")
def build_trade_imbalance_factor() -> rt.FactorGraph:
    """TsZscore(Div(Sub(buy_vol, sell_vol), Add(buy_vol, sell_vol)), 120)"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    diff = g.add_binary(Op.SUB, bv, sv)
    total = g.add_binary(Op.ADD, bv, sv)
    imbalance = g.add_binary(Op.DIV, diff, total)
    g.add_rolling(Op.TS_ZSCORE, imbalance, 120)
    g.compile()
    return g

@register_factor("okx_perp", "T002")
def build_vwap_deviation_factor() -> rt.FactorGraph:
    """Div(Sub(close, vwap), TsStd(Sub(close, vwap), 60))"""
    g = rt.FactorGraph()
    c = g.add_input("close")
    v = g.add_input("vwap")
    dev = g.add_binary(Op.SUB, c, v)
    vol = g.add_rolling(Op.TS_STD, dev, 60)
    g.add_binary(Op.DIV, dev, vol)
    g.compile()
    return g

@register_factor("okx_perp", "T003")
def build_volume_ratio_factor() -> rt.FactorGraph:
    """TsRank(Div(buy_vol, Add(buy_vol, sell_vol)), 240)"""
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    total = g.add_binary(Op.ADD, bv, sv)
    ratio = g.add_binary(Op.DIV, bv, total)
    g.add_rolling(Op.TS_RANK, ratio, 240)
    g.compile()
    return g
```

---

## 8. 线程安全分析

| 共享资源 | 写者 | 读者 | 策略 |
|----------|------|------|------|
| `TradesAccumulator` 内部状态 | trades WS 线程 (高频) | kline 线程 (低频, bar 边界) | 短 mutex / spinlock |
| `bar_queue` | kline 线程 | factor-infer 线程 | 已有 `queue.Queue` (线程安全) |
| `signal_deque` | factor-infer 线程 | main 线程 | 已有 `deque(maxlen=N)` 策略 |

**主要新增竞争**: `TradesAccumulator` 的 `update` (trades 线程) vs
`snapshot + reset` (kline 线程)。由于 kline 读频率很低 (每 1–60 秒一次),
竞争极少, 用最简单的 `threading.Lock` 即可。

---

## 9. 测试计划

### 9.1 单元测试

| 文件 | 覆盖 |
|------|------|
| `tests/dataflow/test_trades_accumulator.py` | 增量正确性, 桶边界 reset, 空桶处理, vwap 精度 |
| `tests/dataflow/test_trades_collector.py` | JSON 解析, 字段映射, 异常消息过滤 |
| `tests/dataflow/test_trades_worker.py` | 线程启停, accumulator 接入 |

### 9.2 集成测试

| 场景 | 验证 |
|------|------|
| kline + trades 同时运行 | round_bars 包含 trades 字段, 时间对齐 |
| C++ `push_bar` 带 trades 字段 | `add_input("vwap")` 因子输出正确 |
| 因子注册 T001–T003 | 全链路: trades WS → accumulator → bar_queue → InferenceEngine → signal_deque |

### 9.3 Simulation 测试

| 场景 | 验证 |
|------|------|
| `mode="simulation"` + `enable_trades=True` | 合成 trades 数据可用, 因子不报 NaN |

---

## 10. 实施步骤

| 步骤 | 工作量 | 依赖 |
|------|--------|------|
| **Step 1**: `TradesAccumulator` + 单元测试 | 小 | 无 |
| **Step 2**: `OKXTradesCollector` + 单元测试 | 小 | 无 |
| **Step 3**: `TradesWorker` + 集成到 Engine | 中 | Step 1, 2 |
| **Step 4**: C++ `BarData` 扩展 + `INPUT_VWAP/BUY_VOL/SELL_VOL` | 中 | 无 (可并行) |
| **Step 5**: pybind11 绑定更新 | 小 | Step 4 |
| **Step 6**: `KlinePublisher` 读 accumulator + round 载荷扩展 | 中 | Step 1, 3 |
| **Step 7**: `Engine._runtime_loop` 传递新字段 | 小 | Step 5, 6 |
| **Step 8**: 示例因子 T001–T003 + simulation 合成 | 小 | Step 4, 5 |
| **Step 9**: 全链路 e2e 测试 | 中 | Step 7, 8 |

Step 1–2 和 Step 4–5 可以并行 (Python 侧 + C++ 侧)。

---

## 附录 A: 多尺度扩展方向

Phase 1 只做单尺度 (与 `data_freq` 同桶)。如果后续需要:

- **`buy_vol_30s`** 和 **`buy_vol_1min`** 共存
- **`vwap_5s`** 和 **`vwap_1min`** 共存

有两种路径:

### 路径 1: 多 accumulator, 各自桶宽

```python
accumulators_5s  = {sym: TradesAccumulator(freq_seconds=5)  for sym in symbols}
accumulators_60s = {sym: TradesAccumulator(freq_seconds=60) for sym in symbols}
```

每笔 trade 同时更新所有 accumulators。push 到 `bar_queue` 时,
每个桶宽有独立的 flush 频率 → 需要多条队列或在 round 载荷里带多尺度字段。

**优点**: 概念清晰。**缺点**: 队列/载荷复杂度上升。

### 路径 2: C++ 侧 rolling

只推最短桶宽 (如 1s) 的 trades 聚合, 更长窗口的 `buy_vol_30s` 等
**在 C++ DAG 里用 `TsSum(buy_vol, 30)` 实现**:

```python
bv = g.add_input("buy_vol")          # 1s 桶内的 buy_vol
bv_30s = g.add_rolling(Op.TS_SUM, bv, 30)   # rolling 30 bars = 30s
```

**优点**: 数据流侧最简; 复用现有 DAG 算子; 天然支持任意窗口。
**缺点**: 需要 bar 频率足够高 (如 1s), 否则 rolling 窗口精度受限。

**推荐**: Phase 2 走路径 2 — 保持数据流侧简单, 多尺度在 DAG 里表达。

---

## 附录 B: 后续可扩展的 trades 聚合量

Phase 1 之后, 按需逐步加入:

| 量 | 定义 | 增量? | 优先级 |
|----|------|-------|--------|
| `trade_count` | 桶内成交笔数 | ✅ | Phase 1 (已含) |
| `net_vol` | `buy_vol - sell_vol` | ✅ (DAG 可算) | Phase 2 |
| `signed_volume` | `Σ(sz × sign)` | ✅ | Phase 2 |
| `ret_vol` (realized vol) | `Σ(ret_i²)` 或 `std(trade returns)` | 部分 ✅ (Σr² 可增量; std 需 Welford) | Phase 2 |
| `median_trade_price` | 桶内成交价中位数 | ❌ (需排序) | Phase 3 |
| `trade_arrival_rate` | 笔/秒 | ✅ (count / bucket_width) | Phase 2 |
| `kyle_lambda` | `ΔP / signed_volume` | 部分 | Phase 3 |

---

## 附录 C: OKX WS 地址选择

| 用途 | 地址 | 现有引用 |
|------|------|----------|
| kline (candle) | `wss://ws.okx.com:8443/ws/v5/business` | `OKX_WS_BUSINESS` in `common.py` |
| trades | `wss://ws.okx.com:8443/ws/v5/public` | `OKX_WS_PUBLIC` in `common.py` (已定义, 未使用) |

`common.py` 中 `OKX_WS_PUBLIC` 已存在但当前无人使用, trades collector 可以直接引用。
