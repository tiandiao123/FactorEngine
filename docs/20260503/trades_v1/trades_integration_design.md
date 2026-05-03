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

---

## 附录 D: 数据源模式 (`data_source`) 设计方向

### 动机

高频策略可能完全不依赖 K 线, 只用 trades 聚合特征 (vwap, buy/sell volume
imbalance 等) 构造因子。此时 kline 数据流是不必要的 CPU 开销。

应提供 **数据源模式** 参数, 让 Engine 按需只启动所需的 Worker:

```python
engine = Engine(
    symbols=symbols,
    data_freq="1s",
    data_source="trades",        # ← 新增参数
    factor_group="okx_perp_hf",
)
```

### 三种模式

| `data_source` | 启动的 Worker | bar_queue 载荷 | 适用场景 |
|---------------|--------------|----------------|----------|
| `"kline"` | KlineWorker only | `(round_bars, None)` | K 线因子 (当前默认) |
| `"trades"` | TradesWorker only | `(None, round_trades)` | 高频 trades-only 因子 |
| `"kline_trades"` | 两者都启动 | `(round_bars, round_trades)` | 混合因子 |

### trades-only 模式的关键问题: 谁驱动 round flush?

在 `kline_trades` 模式下, kline 的 bar 边界自然驱动 flush — accumulator
搭便车。但 `trades`-only 模式下没有 kline, 需要独立的 flush 机制。

#### 方案 A: TradesWorker 内置定时 flush

```text
TradesWorker:
  trades WS → accumulator.update()       (每笔 trade, O(1))
  timer(freq_seconds) → for sym in symbols:
                           snap = acc[sym].snapshot_and_reset()
                         → bar_queue.put((None, round_trades))
```

优点: 简单, TradesWorker 自包含, 不依赖外部组件。
缺点:
- timer 精度受 asyncio 事件循环影响 (协作式调度, sleep 只保证"至少等 N 秒",
  实际延迟取决于事件循环是否空闲, 1s 级别误差约 1-5ms, 可接受)
- 每新增一种数据流 (orderbook, funding_rate, liquidation 等) 都要各自实现 timer,
  多个独立 timer 之间存在微小时间偏移, 且代码分散难维护

#### 方案 B: 独立 ClockDriver 组件 (推荐)

**统一时钟源, 所有数据流在同一个节拍上 flush。**

```text
ClockDriver(freq_seconds):
  每 freq_seconds 触发回调 → 注册的所有 worker flush
```

架构全景:

```text
┌──────────────────────────────────────────────────┐
│ ClockDriver(freq=5s)                             │
│   每 5s → flush_callbacks:                       │
│           → trades_worker.flush()                │
│           → orderbook_worker.flush()  (未来)     │
│           → funding_worker.flush()    (未来)     │
│   所有数据流在同一时刻 flush, 时间精确同步        │
└──────────────────────────────────────────────────┘
          ↕ flush()                ↕ update()
┌──────────────────┐    ┌──────────────────────────┐
│ TradesWorker     │    │ OKX trades WS            │
│  accumulators    │←───│  collector → update()     │
└──────────────────┘    └──────────────────────────┘
```

参考实现:

```python
class ClockDriver:
    """Unified clock that drives periodic flush for all data streams.

    All registered callbacks are invoked at the same wall-clock boundary,
    ensuring time alignment across heterogeneous data sources.
    """

    def __init__(self, freq_seconds: float):
        self._freq = freq_seconds
        self._callbacks: list[Callable[[], None]] = []
        self._running = False

    def register(self, callback: Callable[[], None]):
        """Register a flush callback (e.g. worker.flush)."""
        self._callbacks.append(callback)

    async def run(self):
        """Block forever, firing all callbacks every freq_seconds.

        Aligns to wall-clock boundaries: if freq=5s, fires at
        :00, :05, :10, ... regardless of when run() was called.
        """
        self._running = True
        freq_ms = self._freq * 1000
        while self._running:
            now_ms = time.time() * 1000
            next_boundary = now_ms - (now_ms % freq_ms) + freq_ms
            sleep_s = (next_boundary - now_ms) / 1000
            await asyncio.sleep(max(sleep_s, 0.001))

            for cb in self._callbacks:
                try:
                    cb()
                except Exception:
                    logger.exception("ClockDriver callback error")

    def stop(self):
        self._running = False
```

关键设计点:

1. **Wall-clock 对齐**: 不是简单 `sleep(freq)`, 而是对齐到整数时间边界
   (与 `KlineAdapter` 的窗口对齐策略一致), 保证 flush 时间可预测
2. **单点控制**: 所有数据流的 flush 频率、时间对齐由一个组件管理
3. **注册式扩展**: 新数据流只需 `clock.register(new_worker.flush)`, 零侵入
4. **timer 精度**: asyncio 事件循环的协作式调度会引入 1-5ms 延迟,
   对 1s+ 级别的 bar 频率完全可接受

为什么选方案 B 而不是 A:

| 维度 | 方案 A (各自 timer) | 方案 B (ClockDriver) |
|------|-------------------|---------------------|
| 新数据流接入 | 每个 Worker 自建 timer | `clock.register(worker.flush)` 一行 |
| 多数据流时间同步 | 各自计时, 有偏移 | 统一节拍, 精确同步 |
| flush 频率调整 | 改每个 Worker | 改一处 `ClockDriver(freq)` |
| 代码维护 | timer 逻辑分散在各 Worker | timer 逻辑集中在 ClockDriver |
| 调试 | 需要对比各 Worker 的 flush 时间 | 看 ClockDriver 日志即可 |

后续数据流接入路径 (均只需 `clock.register`):

```text
Phase 2: clock.register(trades_worker.flush)
Phase 3: clock.register(orderbook_worker.flush)
Phase 4: clock.register(funding_worker.flush)
         clock.register(liquidation_worker.flush)
```

### `kline_trades` 模式下 ClockDriver 与 KlinePublisher 的关系

在 `kline_trades` 模式下, kline 的 bar 边界仍然由 `KlinePublisher` 自行管理
(因为 kline 到达时间由 OKX 服务端推送决定)。ClockDriver 只驱动**非 kline 数据流**
的 flush。两者通过 `bar_queue` 汇合:

```text
kline_trades 模式:
  KlinePublisher._flush_round() → 读 accumulator snapshot → bar_queue
  (kline 到达驱动, 不经过 ClockDriver)

trades-only 模式:
  ClockDriver → trades_worker.flush() → accumulator snapshot → bar_queue
  (ClockDriver 驱动)
```

### Engine 层变更

```python
class Engine:
    def __init__(self, ..., data_source: str = "kline"):
        self._data_source = data_source
        self._clock_driver: ClockDriver | None = None

    def start(self):
        if self._data_source in ("kline", "kline_trades"):
            self._kline_worker.start()
        if self._data_source in ("trades", "kline_trades"):
            self._trades_worker.start()
        # trades-only 或未来多数据流模式: 启动 ClockDriver
        if self._data_source != "kline":
            self._clock_driver = ClockDriver(self._freq_seconds)
            if self._data_source == "trades":
                self._clock_driver.register(self._trades_worker.flush)
            # kline_trades 模式: kline 驱动 flush, ClockDriver 不注册 trades
            # (未来 orderbook 等可在此注册)
            self._start_clock_thread()

    def stop(self):
        if self._clock_driver:
            self._clock_driver.stop()
        if self._kline_worker:
            self._kline_worker.stop()
        if self._trades_worker:
            self._trades_worker.stop()
```

### `_runtime_loop` 根据模式构造 BarData

```python
for sym in round_syms:
    bar = round_bars.get(sym) if round_bars else None
    trades = round_trades.get(sym) if round_trades else {}

    close  = float(bar[4]) if bar is not None else float('nan')
    volume = float(bar[5]) if bar is not None else float('nan')
    ...
    vwap     = trades.get("vwap", float('nan'))
    buy_vol  = trades.get("buy_vol", float('nan'))
    sell_vol = trades.get("sell_vol", float('nan'))

    batch[sym] = rt.BarData(close, volume, open_, high, low, ret,
                            vwap, buy_vol, sell_vol)
```

trades-only 模式下 OHLCV 全部为 NaN, 因子只使用 trades 输入:

```python
@register_factor("okx_perp_hf", "HF001")
def build_trade_imbalance() -> rt.FactorGraph:
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    diff = g.add_binary(Op.SUB, bv, sv)
    total = g.add_binary(Op.ADD, bv, sv)
    imbalance = g.add_binary(Op.DIV, diff, total)
    g.add_rolling(Op.TS_ZSCORE, imbalance, 120)
    g.compile()
    return g
```

C++ DAG 各节点独立, NaN 输入不影响不使用它的节点, **无需特殊处理**。

### 从 trades 自建 K 线 (trades-only 模式的 OHLCV)

K 线本质上就是 trades 在固定时间窗口内的聚合:

```text
open  = 窗口内第一笔 trade 的 px
high  = max(所有 trade 的 px)
low   = min(所有 trade 的 px)
close = 窗口内最后一笔 trade 的 px
vol   = Σ(sz)
```

因此在 `trades`-only 模式下, `TradesAccumulator` 可以**同时维护 OHLCV**,
不再需要额外的 kline WS 连接:

```python
def update(self, px, sz, side):
    with self._lock:
        if self._count == 0:
            self._open = px
            self._high = px
            self._low = px
        else:
            self._high = max(self._high, px)
            self._low = min(self._low, px)
        self._close = px
        self._vol += sz
        # ... 原有的 vwap, buy_vol, sell_vol 逻辑

def snapshot_and_reset(self) -> dict:
    with self._lock:
        result = {
            "open": self._open,
            "high": self._high,
            "low": self._low,
            "close": self._close,
            "vol": self._vol,
            "vwap": self._sum_pv / self._sum_v if self._sum_v > 0 else float('nan'),
            "buy_vol": self._buy_vol,
            "sell_vol": self._sell_vol,
            "trade_count": self._count,
        }
        # reset all fields ...
        return result
```

这样 `BarData` 的所有字段都有值, 因子可以自由混用:

```text
data_source="trades" 时:
  单条 WS 连接 (public) → trades 流
  TradesAccumulator 同时产出:
    → open, high, low, close, vol      (从 trades 自建的 K 线)
    → vwap, buy_vol, sell_vol           (trades 聚合特征)
    → ret = close / prev_close - 1      (Engine 侧计算, 有 close 即可)

  BarData 所有字段都有意义
  因子可以混用 close, volume, vwap, buy_vol 等任意输入
```

**优势**:

| 项目 | trades-only (自建 OHLCV) | kline_trades (双 WS) |
|------|------------------------|---------------------|
| WS 连接 | 1 条 (public) | 2 条 (public + business) |
| 频率灵活性 | 任意 (3s, 7s, 13s 等) | 受限于 OKX candle 频道 |
| 数据一致性 | OHLCV 和 trades 来自同一数据源 | 两个独立推送, 可能微小不一致 |
| CPU 开销 | 稍高 (自行聚合 OHLC) | 稍低 (OKX 服务端聚合) |
| 可靠性 | WS 断线期间丢失 trades → OHLC 不完整 | candle 是 OKX 权威数据 |

**建议**: Phase 2 实现 trades-only 模式时直接在 accumulator 里加入 OHLCV
维护 (几行代码), 让 trades-only 模式成为真正的 **单数据源全功能方案**。

### 关于 `ret` 字段

trades-only 模式下, 如果 accumulator 自建了 OHLCV, `close` 可用,
`Engine._runtime_loop` 可以正常计算 `ret = close / prev_close - 1`。

如果只用 trades 原生特征, 也可以在 DAG 里用 vwap 替代:

```python
v = g.add_input("vwap")
vwap_ret = g.add_rolling(Op.TS_PCT, v, 1)   # vwap / prev_vwap - 1
```

两种方式都可行, **推荐自建 OHLCV** 方案 — 让所有因子输入都有值, 更通用。

### 实施节奏

#### Phase 总览

| 阶段 | 内容 | 前置 |
|------|------|------|
| **Phase 1** (当前文档) | `kline_trades` 模式: trades accumulator + kline flush 驱动 | 无 |
| **Phase 2** | `ClockDriver` + `data_source` 参数, accumulator 自建 OHLCV, 支持 `trades`-only | Phase 1 |
| **Phase 3** | 新数据流接入 (orderbook, funding_rate 等), 通过 `clock.register` 挂载 | Phase 2 |
| **Phase 4** | 性能优化 / 多尺度 / 更多聚合量 | Phase 3 |

每一步都是增量的:
- Phase 1 完成后已经可以验证 trades 因子
- Phase 2 引入 ClockDriver 作为统一时钟源, trades-only 高频策略可独立运行
- Phase 3 新数据流只需实现 Worker + `clock.register`, 零侵入扩展

---

#### Phase 1 详细实施计划

Phase 1 目标: **`kline_trades` 模式跑通, trades 因子产出非 NaN 信号值。**

拆分为 7 个 Step, 每步都可独立测试:

##### Step 1: `TradesAccumulator` — 核心聚合器

```
新建: dataflow/okx/trades/__init__.py
新建: dataflow/okx/trades/accumulator.py
新建: tests/trades/test_accumulator.py
```

- 实现 `TradesAccumulator` 类 (本文档 §4.2 设计)
- `update(px, sz, side)` — O(1) 增量累加
- `snapshot_and_reset()` — 原子读取 + 清零
- `threading.Lock` 保证线程安全

验证:
- 单元测试: 多次 `update()` 后 `snapshot_and_reset()` 返回正确的 vwap/buy_vol/sell_vol
- 边界: 空 accumulator snapshot 返回 NaN/0
- 线程安全: 多线程并发 update + snapshot 无竞态

**无任何外部依赖, 第一个实现。**

##### Step 2: `OXKTradesCollector` — WS 连接层

```
新建: dataflow/okx/trades/collector.py
```

- 仿照 `OXKKlineCollector` (`dataflow/okx/kline/collector.py`)
- 连接 OKX 公共 WS (`wss://ws.okx.com:8443/ws/v5/public`)
- 订阅 `trades` 频道
- 回调: `on_trade(symbol, px, sz, side, ts_ms)`
- 自动重连逻辑复用 kline collector 的 `_ws_loop` 模式

与 kline collector 的区别:
- 公共 WS (不是 business WS)
- 无 confirm 概念, 每条消息都是已成交
- JSON 结构不同 (`data[].px/sz/side` vs `data[].candle_array`)

验证:
- 复用 `tests/trades/websocket/bench_trades_throughput.py` 的连接逻辑
- 启动后能收到 trades 数据并正确解析

##### Step 3: `TradesWorker` — 线程管理

```
新建: dataflow/okx/trades/worker.py
```

- 仿照 `KlineWorker` (`dataflow/okx/kline/worker.py`)
- 启一个 daemon 线程运行 asyncio 事件循环
- 回调中调用对应 symbol 的 `accumulator.update()`
- 暴露 `accumulators: dict[str, TradesAccumulator]` 给外部读取
- **不自行触发 round flush** — Phase 1 由 KlinePublisher 驱动
- `start()` / `stop()` 生命周期管理 + 统计日志

验证:
- 启动 TradesWorker, 10 秒后 stop, 检查各 symbol accumulator 计数 > 0

##### Step 4: `KlinePublisher` 改造 — 撮合点

```
改: dataflow/okx/kline/publisher.py   — _flush_round() 中读 accumulator snapshot
改: dataflow/okx/kline/worker.py      — 接收 accumulators 参数并传给 publisher
改: tests/dataflow/test_okx_kline_worker.py — 更新集成测试
```

核心改动 (`_flush_round`):

```python
def _flush_round(self) -> bool:
    round_bars = dict(self._pending_round)
    # ... 现有的 forward-fill 逻辑 ...

    # ── 新增: 读取 trades accumulator snapshot ──
    round_trades = None
    if self._accumulators:
        round_trades = {}
        for sym in round_bars:
            acc = self._accumulators.get(sym)
            if acc:
                round_trades[sym] = acc.snapshot_and_reset()

    # bar_queue 载荷从 dict 变为 tuple
    payload = (round_bars, round_trades)
    self._bar_queue.put(payload, block=False)
```

关键约束:
- `accumulators` 可选 (None = 纯 kline 模式, 向后兼容)
- 现有 kline-only 路径不受影响
- bar_queue 载荷类型: `tuple[dict, dict | None]`

验证:
- 现有 kline 测试不 break (accumulators=None)
- 集成测试: mock accumulator, 验证 flush 时 snapshot_and_reset 被调用

##### Step 5: C++ 扩展 — BarData + FactorGraph + pybind

```
改: native/include/fe/runtime/inference_engine.hpp   — BarData 加 vwap/buy_vol/sell_vol
改: native/include/fe/runtime/factor_graph.hpp       — Op 枚举 + add_input + push_bar
改: native/pybind/fe_runtime_bind.cpp               — pybind11 绑定
重编译: cd native && mkdir -p build && cd build && cmake .. && make -j
```

BarData 扩展:
```cpp
struct BarData {
    FeFloat close = 0.0f, volume = NaN, open = NaN, high = NaN, low = NaN, ret = NaN;
    FeFloat vwap = NaN, buy_vol = NaN, sell_vol = NaN;   // ← 新增
};
```

Op 枚举扩展:
```cpp
INPUT_VWAP, INPUT_BUY_VOL, INPUT_SELL_VOL   // ← 新增
```

`add_input()` 扩展 (§5.2 前置修复已完成, 未知 feature 会 throw):
```cpp
else if (feature == "vwap")     node.op = Op::INPUT_VWAP;
else if (feature == "buy_vol")  node.op = Op::INPUT_BUY_VOL;
else if (feature == "sell_vol") node.op = Op::INPUT_SELL_VOL;
```

`push_bars` lambda 同步更新 (§5.3):
```cpp
runner->push_bar(b.close, b.volume, b.open, b.high, b.low, b.ret,
                 b.vwap, b.buy_vol, b.sell_vol);
```

验证:
- `python -c "import fe_runtime as rt; g = rt.FactorGraph(); g.add_input('vwap')"` 不报错
- `g.add_input('unknown_field')` 抛 `ValueError`

##### Step 6: 注册 trades 因子

```
改: factorengine/factors/okx_perp/factor_bank.py   — 添加 T001-T004
```

4 个因子覆盖三种场景:

```python
# T001: Trade Imbalance — 纯 trades 输入
@register_factor("okx_perp", "T001")
def build_trade_imbalance():
    g = rt.FactorGraph()
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    diff = g.add_binary(Op.SUB, bv, sv)
    total = g.add_binary(Op.ADD, bv, sv)
    imbalance = g.add_binary(Op.DIV, diff, total)
    g.add_rolling(Op.TS_ZSCORE, imbalance, 120)
    g.compile()
    return g

# T002: VWAP-Close Divergence — 混合输入 (kline + trades)
@register_factor("okx_perp", "T002")
def build_vwap_close_divergence():
    g = rt.FactorGraph()
    c = g.add_input("close")
    vw = g.add_input("vwap")
    dev = g.add_binary(Op.SUB, vw, c)
    norm = g.add_binary(Op.DIV, dev, c)
    g.add_rolling(Op.TS_ZSCORE, norm, 60)
    g.compile()
    return g

# T003: VWAP Momentum — vwap 替代 close 做动量
@register_factor("okx_perp", "T003")
def build_vwap_momentum():
    g = rt.FactorGraph()
    vw = g.add_input("vwap")
    log_vw = g.add_unary(Op.LOG, vw)
    mom = g.add_rolling(Op.TS_DIFF, log_vw, 10)
    vol = g.add_rolling(Op.TS_STD, g.add_rolling(Op.TS_DIFF, log_vw, 1), 60)
    g.add_binary(Op.DIV, mom, vol)
    g.compile()
    return g

# T004: Imbalance × Momentum — 混合因子
@register_factor("okx_perp", "T004")
def build_imbalance_momentum():
    g = rt.FactorGraph()
    c = g.add_input("close")
    bv = g.add_input("buy_vol")
    sv = g.add_input("sell_vol")
    imb = g.add_binary(Op.DIV, g.add_binary(Op.SUB, bv, sv),
                       g.add_binary(Op.ADD, bv, sv))
    logc = g.add_unary(Op.LOG, c)
    ret = g.add_rolling(Op.TS_DIFF, logc, 5)
    g.add_rolling(Op.TS_ZSCORE, g.add_binary(Op.MUL, imb, ret), 120)
    g.compile()
    return g
```

因子场景覆盖:

| 因子 | 输入 | 验证目标 |
|------|------|---------|
| T001 | buy_vol, sell_vol | 纯 trades 因子, trades-only 可用 |
| T002 | close, vwap | kline + trades 混用 |
| T003 | vwap | 用 vwap 替代 close 的动量因子 |
| T004 | close, buy_vol, sell_vol | 混合 kline + trades 的复合因子 |

验证:
- `FactorRegistry().load_group("okx_perp")` 包含 T001-T004
- 每个因子 `g.compile()` 无报错

##### Step 7: Engine 集成 + 端到端验证

```
改: factorengine/engine.py — 启动 TradesWorker, _runtime_loop 解包 tuple, 构造完整 BarData
新建/改: scripts/engine_rt_live.py 或类似入口 — 用 kline_trades 模式跑
```

Engine 改动:

```python
class Engine:
    def __init__(self, ..., data_source: str = "kline"):
        self._data_source = data_source
        # kline_trades 或 trades 模式下创建 TradesWorker
        if data_source in ("trades", "kline_trades"):
            self._trades_worker = TradesWorker(symbols=..., ...)

    def start(self):
        if self._data_source in ("kline", "kline_trades"):
            # 传 accumulators 给 KlineWorker/Publisher
            self._kline_worker.start(accumulators=self._trades_worker.accumulators)
        if self._data_source in ("trades", "kline_trades"):
            self._trades_worker.start()

    def _runtime_loop(self):
        while True:
            payload = self._bar_queue.get()
            round_bars, round_trades = payload  # 解包 tuple
            for sym in ...:
                bar = round_bars[sym] if round_bars else None
                trades = (round_trades or {}).get(sym, {})
                batch[sym] = rt.BarData(
                    close=..., volume=..., open_=..., high=..., low=..., ret=...,
                    vwap=trades.get("vwap", float('nan')),
                    buy_vol=trades.get("buy_vol", float('nan')),
                    sell_vol=trades.get("sell_vol", float('nan')),
                )
```

端到端验证 checklist:

- [ ] `engine_rt_live.py` 以 `data_source="kline_trades"` 启动
- [ ] 日志显示 KlineWorker 和 TradesWorker 均正常连接
- [ ] signal_deque 输出中 T001-T004 因子值为非 NaN (warmup 后)
- [ ] 现有 kline 因子 (0001-0065) 不受影响, 值与之前一致
- [ ] `data_source="kline"` 模式 (默认) 行为完全不变
- [ ] TradesWorker stop 时日志显示 trade_count > 0

---

#### 实施依赖关系

```
Step 1 (Accumulator) ─────┐
                          │
Step 2 (Collector)  ──────┼──→ Step 3 (Worker) ──→ Step 4 (Publisher 改造)
                          │                                    │
                          │                                    ↓
                          │              Step 5 (C++) ──→ Step 6 (因子注册)
                          │                                    │
                          │                                    ↓
                          └────────────────────────────→ Step 7 (Engine 集成)
```

Step 1 和 Step 2 可并行; Step 5 和 Step 4 可并行; Step 7 依赖所有前置。

### 300 symbols 资源估算

| 资源 | kline 模式 | trades 模式 | kline_trades 模式 |
|------|-----------|-------------|-------------------|
| WS 连接数 | 2 (business) | 2 (public) | 4 (2+2) |
| CPU (JSON 解析) | ~300 msg/s | ~9,000 msg/s | ~9,300 msg/s |
| 内存 (额外) | ~2 MB | ~15 MB | ~17 MB |
| 网络带宽 | ~75 KB/s | ~2.2 MB/s | ~2.3 MB/s |

trades 模式的 CPU 负载约为 kline 的 30 倍, 但仍在单线程 asyncio
可处理范围内 (Python `json.loads` 极限约 50K-100K msg/s)。

### 吞吐量实测 (2026-05-03)

基准测试脚本: `tests/trades/websocket/bench_trades_throughput.py`

BTC 单品种 (30s, 非极端行情):
- 平均 ~11 trades/s, 峰值 50, json.loads ~17μs/msg

10 个主流品种 (30s):
- 合计 ~100 trades/s, BTC peak=650, json.loads ~13μs/msg

300 symbols 外推: 常态 ~500-1,000 trades/s, 仅占单线程 ~1% CPU。
详见 `tests/trades/websocket/benchmark_report.md`。
