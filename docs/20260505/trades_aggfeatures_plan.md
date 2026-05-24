# Trades Aggregation Features 扩展计划

> 日期：2026-05-05
> 分支：`feature/trades_aggfeatures`
> 目标：在 `TradesAccumulator` 与 C++ `BarData` 中扩充 high-frequency microstructure features，把因子库从 "5 个变量反复变换" 拓展到 **业界标准的 microstructure 因子谱系**

---

## 1. 现状盘点

### 1.1 当前 `TradesAccumulator` 暴露的字段

```python
{
    "vwap":        Σ(px*sz) / Σ(sz),        # 1
    "buy_vol":     Σ(sz) where side=="buy", # 2
    "sell_vol":    Σ(sz) where side=="sell",# 3
    "trade_count": N,                       # 4 (Python only, 未接入 C++)
    "open / high / low / close / volume":   # 5–9 OHLCV from trades
}
```

### 1.2 C++ `BarData` 暴露的字段

```cpp
struct BarData {
    FeFloat close, volume, open, high, low, ret;   // OHLCV + ret
    FeFloat vwap, buy_vol, sell_vol;               // 3 个 trades-derived
};
```

`FactorGraph::add_input()` 仅接受 9 个 feature name：
`close / volume / open / high / low / ret / vwap / buy_vol / sell_vol`。

### 1.3 当前 53 个 trades 因子的输入分布

| 输入 feature | 使用次数（53 个因子中） |
|--------------|----------------------|
| `vwap` / `buy_vol` / `sell_vol` | **43 次** |
| `volume` / `close` / `ret` | 其余 |

→ **信息源极窄**。T0001~T0030 多数都是 `(buy_vol, sell_vol)` 的不同组合（Z-score、kurtosis、cv、cos、beta…），本质上是同一个 imbalance 信号的反复变换。新增 53 个因子≠新增 53 个独立信号。

### 1.4 行业 high-frequency factor 的标配维度（我们缺什么）

| 维度类别 | 业界常用 features | 我们当前是否支持 |
|---------|------------------|------------|
| **基本聚合** | OHLCV, vwap, buy/sell_vol, trade_count | ✅ 部分（trade_count 没接 C++） |
| **笔数维度** | buy_count, sell_count, avg_trade_size, max_trade_size | ❌ |
| **分层 volume** | large_buy_vol, small_buy_vol, large_sell_vol, small_sell_vol | ❌ |
| **侧别 VWAP** | vwap_buy, vwap_sell, mid_vwap | ❌ |
| **价格行为** | upticks, downticks, vwap_deviation, price_std (intra-bar) | ❌ |
| **Realized 波动** | realized_variance, bipower_variation, log-return sum²  | ❌ |
| **Notional** | buy_notional, sell_notional, max_notional | ❌ |
| **时间分布** | first_ts, last_ts, trade_intensity（笔数/秒） | ❌ |

---

## 2. 设计原则

1. **保持 O(1) 增量更新** — 所有新字段都必须在 `update(px, sz, side)` 里 O(1) 累积，不引入 buffer 也不在 `update()` 里做 sort / search
2. **增量内存可控** — 每个 symbol 的 `TradesAccumulator` 字段总量 < 200 字节（cache-friendly）
3. **Forward-fill 一致** — 新加的 price-like feature（如 `vwap_buy`）在空 bucket 下要 forward-fill；count-like feature（如 `buy_count`）保持 0
4. **C++ 接入分批进行** — 不是所有 feature 都需要 C++ 因子直接消费；优先把"高信息量、组合不出"的几个接入
5. **向后兼容** — 现有 53 个因子在 `BarData` 字段顺序不变的前提下不需修改
6. **每个新 feature 至少配 2 个示例因子** — 验证它确实能产生独立信号

---

## 3. 新增 features 设计（按优先级）

### 3.1 Tier S — 必加（信息量最高，O(1) 增量平凡）

| Feature | 公式 | 增量更新 | 说明 |
|---------|------|---------|------|
| **`buy_count`** | `Σ 1 where side=="buy"` | `if side=="buy": _buy_count += 1` | 主动买的笔数 |
| **`sell_count`** | `Σ 1 where side=="sell"` | 同上 | 主动卖的笔数 |
| **`buy_notional`** | `Σ (px*sz) where side=="buy"` | `_buy_pv += px*sz` | 主动买名义金额 |
| **`sell_notional`** | `Σ (px*sz) where side=="sell"` | 同上 | 主动卖名义金额 |
| **`vwap_buy`** | `buy_notional / buy_vol` | `flush()` 时计算 | 主买的成交均价 |
| **`vwap_sell`** | `sell_notional / sell_vol` | 同上 | 主卖的成交均价 |
| **`max_trade_size`** | `max(sz)` | `if sz > _max: _max = sz` | bucket 内单笔最大 |
| **`price_std`** | `√(Σ(px²)*N − (Σpx)²) / N` (Welford) | Welford 公式 O(1) 二阶矩 | bucket 内成交价波动 |

**累积存储成本**：每 symbol +8 个 float64 = 64 bytes。

**这一层独立解锁的因子类别**：

- **Aggressor strength** — `buy_count / (buy_count + sell_count)` 摆脱 volume 干扰（鲸鱼大单不会扭曲）
- **Toxicity / VPIN proxy** — `|buy_vol − sell_vol| / volume` 与 `buy_count` 差异
- **Buy-Sell spread proxy** — `(vwap_buy − vwap_sell) / mid` 估计 effective spread
- **Whale activity** — `max_trade_size / avg_trade_size` 突变信号
- **Intra-bar volatility** — `price_std × trade_intensity` 检测信息冲击

---

### 3.2 Tier A — 强烈建议（解锁 realized variance & 跳价类因子）

| Feature | 公式 | 增量更新 | 说明 |
|---------|------|---------|------|
| **`realized_variance`** | `Σ (log(px_t/px_{t-1}))²` | 每个 trade 算与上一笔的 log-return 平方累加 | bucket 内已实现方差 |
| **`upticks`** | `Σ 1 where px > prev_px` | 同上 | 上升 tick 数 |
| **`downticks`** | `Σ 1 where px < prev_px` | 同上 | 下降 tick 数 |
| **`max_drawdown_intrabar`** | `max(running_max − px)` | 维护 running max | bucket 内最大回撤 |

**累积存储成本**：每 symbol +5 个 float64 (含 prev_px) = 40 bytes。

**独立解锁的因子类别**：

- **Realized vol regime** — `realized_variance / TsMean(rv, 60)` 实时波动制度
- **Tick imbalance** — `(upticks − downticks) / (upticks + downticks)` 比 vol 更细的方向信号
- **Reversal probability** — `max_drawdown / range` 与回归预期相关

---

### 3.3 Tier B — 可选（分层 volume，需要 EMA 阈值）

| Feature | 公式 | 增量更新 | 说明 |
|---------|------|---------|------|
| **`large_buy_vol`** | `Σ sz where side=="buy" and sz > thresh` | 阈值 EMA 维护 | 大单买量 |
| **`large_sell_vol`** | 同上 with side=="sell" | 同上 | 大单卖量 |
| **`small_buy_vol`** | `buy_vol − large_buy_vol` | flush 时计算 | 散户买量 |
| **`small_sell_vol`** | 同上 | 同上 | 散户卖量 |

**阈值方案**：用每 symbol 的 EMA(trade_size, alpha=0.001) × 系数（如 5×）作为 large 阈值。

**独立解锁的因子类别**：

- **Smart money flow** — `large_buy_vol − large_sell_vol` 大单方向
- **Retail vs Whale divergence** — `(small_buy − small_sell) − (large_buy − large_sell)` 对冲信号

**注意**：阈值是 stateful 的，跨 bucket 演化，不在 reset 范围内。

---

### 3.4 Tier C — 性价比中等（时间分布）

| Feature | 公式 | 增量更新 | 说明 |
|---------|------|---------|------|
| **`first_trade_ts`** | bucket 内首笔时间戳 | `if count==0: _first_ts = ts` | 启动时刻 |
| **`last_trade_ts`** | bucket 内末笔时间戳 | `_last_ts = ts` | 结束时刻 |
| **`trade_intensity`** | `count / freq_seconds` | flush 时算 | 每秒成交数 |

**说明**：`first_ts` 和 `last_ts` 主要给 debugging / cross-symbol 时间对齐用，不一定要接入 C++ 因子。

---

## 4. 三层改造方案

### 4.1 Layer 1 — Python `TradesAccumulator` 扩展

**文件**：`dataflow/trades/accumulator.py`

新增字段（用 `__slots__` 严格控制）：

```python
__slots__ = (
    # ── 已有 ──
    "_lock", "_sum_pv", "_sum_v",
    "_buy_vol", "_sell_vol", "_count",
    "_open", "_high", "_low", "_close", "_last_close",
    # ── Tier S ──
    "_buy_count", "_sell_count",
    "_buy_pv", "_sell_pv",
    "_max_size",
    "_sum_px", "_sum_px2",     # for price_std (Welford)
    # ── Tier A ──
    "_prev_px", "_sum_logret2", "_upticks", "_downticks",
    "_running_max", "_max_drawdown",
    # ── Tier C ──
    "_first_ts", "_last_ts",
)
```

**`snapshot_and_reset()` 返回的 dict 新增 keys**：

```python
{
    # 已有 (9 个)
    "vwap", "buy_vol", "sell_vol", "trade_count",
    "open", "high", "low", "close", "volume",
    # Tier S 新增 (8 个)
    "buy_count", "sell_count",
    "buy_notional", "sell_notional",
    "vwap_buy", "vwap_sell",        # forward-fill on empty bucket
    "max_trade_size", "price_std",
    # Tier A 新增 (4 个)
    "realized_variance", "upticks", "downticks", "max_drawdown_intrabar",
    # Tier C 新增 (3 个)
    "first_trade_ts", "last_trade_ts", "trade_intensity",
}
```

`update()` 签名扩展：增加 `ts: int` 参数（用于 first_ts/last_ts/log-return）。

### 4.2 Layer 2 — C++ `BarData` 与 `INPUT_*` Op 扩展

**文件**：
- `native/include/fe/runtime/inference_engine.hpp` — 扩 `BarData` struct
- `native/include/fe/runtime/factor_graph.hpp` — 扩 `add_input()` 字符串映射 + 新 `Op::INPUT_*`
- `native/include/fe/ops/op_tags.hpp` (或类似) — 新 op 枚举
- `native/pybind/fe_runtime_bind.cpp` — 扩 `BarData` 构造函数 + `add_input()` 文档

**接入策略**：先接入 Tier S 的 8 个 + Tier A 的 4 个，共 **12 个新 inputs**。Tier B/C 暂留 Python 侧（accumulator 暴露但不接 BarData，由 stats 接口取用）。

新增 `BarData` 字段（按 cache line 对齐）：

```cpp
struct BarData {
    // 已有 9 个
    FeFloat close, volume, open, high, low, ret;
    FeFloat vwap, buy_vol, sell_vol;
    // Tier S 新增 (8 个)
    FeFloat buy_count, sell_count;
    FeFloat buy_notional, sell_notional;
    FeFloat vwap_buy, vwap_sell;
    FeFloat max_trade_size, price_std;
    // Tier A 新增 (4 个)
    FeFloat realized_variance, upticks, downticks, max_drawdown_intrabar;
};
// 总共 21 个 float = 168 bytes (3 cache lines)
```

`FactorGraph::add_input()` 扩展：

```cpp
else if (feature == "buy_count")          node.op = Op::INPUT_BUY_COUNT;
else if (feature == "sell_count")         node.op = Op::INPUT_SELL_COUNT;
else if (feature == "buy_notional")       node.op = Op::INPUT_BUY_NOTIONAL;
else if (feature == "sell_notional")      node.op = Op::INPUT_SELL_NOTIONAL;
else if (feature == "vwap_buy")           node.op = Op::INPUT_VWAP_BUY;
else if (feature == "vwap_sell")          node.op = Op::INPUT_VWAP_SELL;
else if (feature == "max_trade_size")     node.op = Op::INPUT_MAX_TRADE_SIZE;
else if (feature == "price_std")          node.op = Op::INPUT_PRICE_STD;
else if (feature == "realized_variance")  node.op = Op::INPUT_REALIZED_VAR;
else if (feature == "upticks")            node.op = Op::INPUT_UPTICKS;
else if (feature == "downticks")          node.op = Op::INPUT_DOWNTICKS;
else if (feature == "max_drawdown_intrabar") node.op = Op::INPUT_MAX_DD;
```

`SymbolRunner::push_bar()` 内部 dispatch 扩展（让每个 INPUT_* op 拿到对应 BarData 字段）。

### 4.3 Layer 3 — Engine glue + Worker 适配

**文件**：
- `factorengine/engine.py` `_runtime_loop()` — 把 `round_trades` snapshot 里的新字段填入 `rt.BarData`
- `dataflow/okx/trades/worker.py` 与 `dataflow/binance/trades/worker.py` — 透传 ts 给 accumulator
- `dataflow/okx/trades/collector.py` 与 `dataflow/binance/trades/collector.py` — `on_trade(symbol, px, sz, side, ts_ms)` 已有签名，确保 ts 传入

**`Engine._runtime_loop()` 改动示例**：

```python
batch[sym] = rt.BarData(
    close, volume, open_, high, low, ret,
    vwap, buy_vol, sell_vol,
    # 新增
    buy_count, sell_count,
    buy_notional, sell_notional,
    vwap_buy, vwap_sell,
    max_trade_size, price_std,
    realized_variance, upticks, downticks, max_drawdown_intrabar,
)
```

可以考虑把 `BarData` 的 Python 构造改成 keyword-only 或 dict-based，避免 21 个位置参数易错。

---

## 5. 新因子计划（每个 Tier 至少 2 个示例）

### 5.1 Tier S 解锁的新因子（建议 10–12 个，T0054–T0065）

| ID | 名称 | 公式 | 价值 |
|----|-----|------|-----|
| T0054 | Aggressor Count Imbalance | `TsZscore((buy_count - sell_count) / (buy_count + sell_count), 120)` | 不受单笔大小干扰的方向 |
| T0055 | Effective Spread Proxy | `TsZscore((vwap_buy - vwap_sell) / vwap, 60)` | 估计有效买卖价差 |
| T0056 | Whale Activity | `TsZscore(max_trade_size / (volume / trade_count + ε), 120)` | 大单异常检测 |
| T0057 | Intra-bar Volatility Surge | `TsRank(price_std × trade_intensity, 240)` | 信息冲击 |
| T0058 | Notional Imbalance | `TsZscore((buy_notional - sell_notional) / (buy_notional + sell_notional), 60)` | 名义金额方向 |
| T0059 | Buy Aggression Strength | `TsZscore(buy_count / trade_count, 90)` | 主动买占比 |
| T0060 | Avg Trade Size Drift | `TsRank(volume / trade_count, 180)` | 大户/散户切换 |
| T0061 | Spread Volatility | `TsStd((vwap_buy - vwap_sell) / vwap, 60)` | 流动性波动 |
| T0062 | Notional vs Volume Divergence | `Corr(buy_notional, buy_vol, 60)` | 价位结构 |
| T0063 | Whale Direction | `Sign(max_trade_size_buy_indicator - max_trade_size_sell_indicator)` | 大单方向 |

### 5.2 Tier A 解锁的新因子（建议 6–8 个，T0066–T0072）

| ID | 名称 | 公式 | 价值 |
|----|-----|------|-----|
| T0066 | Realized Vol Regime | `TsZscore(realized_variance, 240)` | 波动制度 |
| T0067 | Tick Imbalance | `(upticks - downticks) / (upticks + downticks)` | 价格动量 |
| T0068 | Realized Vol × Imbalance | `realized_variance × abs(buy_vol - sell_vol) / volume` | 信息流毒性 |
| T0069 | Intra-bar Drawdown | `TsRank(max_drawdown_intrabar / (high - low + ε), 120)` | bucket 内反转强度 |
| T0070 | Tick Direction Persistence | `TsZscore(upticks - downticks, 60)` | tick rule 动量 |
| T0071 | Realized vol vs OHLC range | `realized_variance / ((high - low) / close + ε)` | 真实波动 vs 价差 |

### 5.3 总计

执行完后：
- **trades factors**: 53 → 70+
- **独立信息维度**：从 3 (vwap, buy_vol, sell_vol) → 14 (新加 11 个独立信号)
- **C++ BarData 字段**：9 → 21
- **TradesAccumulator 输出 keys**：9 → 24

---

## 6. 实施分阶段计划

### Phase 1 — Python `TradesAccumulator` 扩展 + 单元测试（0.5 天）

- [ ] 扩展 `__slots__` 和 `update(px, sz, side, ts)`
- [ ] 实现 Tier S + Tier A 共 12 个 features 的 O(1) 增量
- [ ] 实现 forward-fill 规则（vwap_buy/sell 用 last_close, count-like 保持 0）
- [ ] 扩展 `snapshot_and_reset()` 返回 dict
- [ ] 改 OKX/Binance worker 的 `on_trade` 把 `ts_ms` 传入 accumulator
- [ ] 单元测试：每个 feature 的数学正确性、reset 行为、forward-fill 行为
- [ ] 不动 C++

**交付物**：accumulator 在 Python 侧已经能输出 24 个字段；现有因子完全不受影响（新字段只是 dict 多了 keys）。

### Phase 2 — C++ `BarData` + `add_input()` 扩展（1 天）

- [ ] 扩 `BarData` struct（注意字段顺序，向后兼容）
- [ ] 加新 `Op::INPUT_*` 枚举（12 个）
- [ ] 改 `FactorGraph::add_input()` 字符串映射
- [ ] 改 `SymbolRunner::push_bar()` dispatch
- [ ] 改 pybind 绑定：`BarData` 构造、Python 字典化输入
- [ ] 改 `Engine._runtime_loop()` 把 snapshot 字段填入 `BarData`
- [ ] C++ 单元测试：每个新 input 的 graph compile + push_bar 正确性
- [ ] Engine 集成测试：trades-only 模式下 21 字段全部正确流转

**交付物**：因子可以 `g.add_input("buy_count")` 之类，全链路打通。

### Phase 3 — 新因子注册 + 测试 + 文档（1 天）

- [ ] 在 `okx_perp_trades/factor_bank.py` 注册 T0054–T0072（共 19 个新因子）
- [ ] 更新 `tests/factors/test_inference_engine.py` 的 `NUM_FACTORS` 计数
- [ ] 更新 `docs/OPERATORS.md` 把新 input 加入 Inputs 章节
- [ ] 更新 `dataflow/README.md` 与 `README.md` 反映新字段
- [ ] 跑生产 OKX + Binance 验证因子值在合理范围

**交付物**：trades 因子库 53 → 70+，每个新 feature 至少有 2 个示例因子。

### 总时间预估：**2.5 个工作日**

---

## 7. 风险 & 注意事项

### 7.1 性能

- `update()` 每笔 trade 增加 ~6 个浮点运算，O(1) 不变
- BTC 等高频品种 ~100–300 笔/秒/symbol，单线程足够
- C++ `push_bars()` 多了 12 个字段的 dispatch，对 100 + symbols 影响约 +5–10%（实测验证）

### 7.2 数值稳定性

- **Welford 计算 `price_std`** — 必须用增量公式而非 `Σ(px²) - (Σpx)²/N`（后者有灾难性消去）
- **`realized_variance`** — 用 log-return 而非 simple return，避免低价币累计误差
- **forward-fill 与 NaN 传播** — `vwap_buy/sell` 在没有买/卖盘时要 forward-fill 上一个 bucket 的值，否则下游 rolling 因子会被 NaN 污染（参考现有 `vwap` 处理）

### 7.3 跨交易所一致性

- OKX 的 `side: "buy"/"sell"` 与 Binance 的 `is_buyer_maker` 已在 collector 层统一为 `"buy"/"sell"` aggressor side
- `ts_ms` 字段 OKX 是字符串、Binance 是整数 — collector 层已统一为 `int(ts_ms)`，Phase 1 直接用即可

### 7.4 阈值类 features 推迟

Tier B 的分层 volume 需要每 symbol 维护 EMA 阈值，跨 bucket 演化，**与现有 reset-on-flush 模型有耦合**。建议放到独立的 phase 4 或更晚，需要先评估是否真的对因子有显著贡献。

### 7.5 测试覆盖

每个新 feature 必须有：
1. **数学正确性**单测（已知输入 → 已知输出）
2. **空 bucket forward-fill** 单测
3. **reset 边界**单测（多个 bucket 之间不串数据）
4. **C++ ↔ Python 一致性**单测（同一组 trades 喂入 accumulator + push_bar，输出比对）

---

## 8. 影响面盘点

### 改动文件清单

| 文件 | 改动类型 | 预估行数 |
|------|--------|---------|
| `dataflow/trades/accumulator.py` | 扩展 | +120 |
| `dataflow/okx/trades/worker.py` | 改 `on_trade` 签名 | +5 |
| `dataflow/binance/trades/worker.py` | 同上 | +5 |
| `dataflow/okx/trades/collector.py` | 透传 ts | +2 |
| `dataflow/binance/trades/collector.py` | 同上 | +2 |
| `factorengine/engine.py` | `_runtime_loop` 填字段 | +20 |
| `native/include/fe/runtime/inference_engine.hpp` | 扩 `BarData` | +20 |
| `native/include/fe/runtime/factor_graph.hpp` | 扩 input 映射 | +30 |
| `native/include/fe/ops/op_tags.hpp` | 加 12 个 op enum | +12 |
| `native/pybind/fe_runtime_bind.cpp` | 扩 BarData 构造 | +30 |
| `factorengine/factors/okx_perp_trades/factor_bank.py` | 加 19 个新因子 | +400 |
| `tests/trades/test_accumulator.py` | 新 features 测试 | +200 |
| `tests/runtime_engine/test_engine_integration.py` | NUM_FACTORS 更新 | +5 |
| `tests/factors/test_inference_engine.py` | parametrize 范围更新 | +5 |
| `docs/OPERATORS.md` | 加 Inputs 章节 | +60 |

**总改动约 900 行**（其中因子 builder 占 400+，是主要的）。

### 兼容性

- ✅ 现有 53 个因子完全不受影响（依赖的 9 个 input 字段在 `BarData` struct 前部不变）
- ✅ 现有 OKX kline 流路径不受影响（trades-only 改动不动 kline path）
- ✅ Simulation mode 不受影响（不走 trades worker）
- ⚠️ `BarData` Python 构造函数参数从 9 个变 21 个 — 需在 `engine._runtime_loop()` 同步改，且推荐改 keyword-only

---

## 9. 推荐立即开始的最小切片

如果想先验证想法不引入太大变动，可以先做 **Tier S 的 4 个核心 features**：

1. `buy_count`
2. `sell_count`
3. `vwap_buy`
4. `vwap_sell`

这 4 个就足以注册 5 个新因子（aggressor count imbalance、effective spread proxy 等），并且：
- C++ BarData 只多 4 个字段
- Python `TradesAccumulator` 改动 < 30 行
- 1 天可完成

之后根据实测因子表现决定是否上 Tier A、B。

---

## 10. 决策点

需要 confirm 的几点：

1. **范围**：是 Tier S+A 全做（19 个新因子），还是先做 Tier S 的 minimal 切片（5 个新因子）？
2. **`BarData` 构造函数兼容性**：21 个位置参数太多，是否一并改成 keyword-only / named-args？
3. **新因子是放在现有 `okx_perp_trades` group 还是新建 `okx_perp_trades_v2` group**？前者编号继续 T0054+，后者全新命名空间避免阻塞旧因子。
4. **是否同步加 trade_count 进 C++**？目前它是 Python-only stats，但其实是个有用的 input。

---

## 附录 A：Welford 增量方差公式

避免灾难性消去的标准做法：

```python
# state: _count, _mean, _m2
def update(x):
    _count += 1
    delta = x - _mean
    _mean += delta / _count
    delta2 = x - _mean
    _m2 += delta * delta2

def variance():
    return _m2 / _count if _count > 0 else nan
```

但因为我们 bucket 通常很短（1s），且 `_count` 很少超过几百，**直接用 `Σpx, Σpx²` 然后 `Var = Σpx²/N − (Σpx/N)²`** 在 float64 下也够稳定。Welford 是 over-engineering，除非 bucket 内 trade 数 > 10⁵。

## 附录 B：log-return 近似

`log(px_t / px_{t-1}) ≈ (px_t - px_{t-1}) / px_{t-1}` 在 px 变化 < 1% 时误差 < 5e-5。但 **realized variance 标准是用 log-return**，且 `log()` 在现代 CPU 上 ~10ns，无所谓。直接 `math.log(px_t) - math.log(prev_px)` 即可。
