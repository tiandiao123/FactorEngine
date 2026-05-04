# Ops 算子扩展与因子压测计划

> 日期：2026-05-04
> 分支：feature/ops_scaling
> 来源：公司 Operations.py 算子库 (1192 行, ~80+ 算子) 对标 FactorEngine C++ Op enum

---

## 1. 现状盘点

### 1.1 FactorEngine C++ 算子 (50 个)

| 类别 | 算子 | 数量 |
|------|------|------|
| Input | close, volume, open, high, low, ret, vwap, buy_vol, sell_vol | 9 |
| P0 Unary | NEG, ABS, LOG, SQR, INV, SIGN, TANH, SLOG1P | 8 |
| P0 Binary | ADD, SUB, MUL, DIV | 4 |
| P0 Scalar | ADD_SCALAR, SUB_SCALAR, MUL_SCALAR, DIV_SCALAR, SCALAR_SUB, SCALAR_DIV | 6 |
| P1 Rolling | MA, TS_SUM, TS_STD, TS_VARI, EMA, TS_MIN, TS_MAX, TS_RANK, TS_ZSCORE, DELAY, TS_DIFF, TS_PCT | 12 |
| P2 Bivariate | CORR, AUTOCORR, TS_MINMAX_DIFF, TS_SKEW | 4 |
| P3 Complex | TS_MED, TS_MAD, TS_WMA, TS_MAX_DIFF, TS_MIN_DIFF | 5 |
| Optimized | TREAP_TS_RANK | 1 |
| Derived | PCT_CHANGE | 1 |
| **合计** | | **50** |

### 1.2 已注册因子 (66 个)

| Group | 数量 | 输入 |
|-------|------|------|
| okx_perp | 60 | close, volume, open, high, low, ret |
| okx_perp_trades | 6 | vwap, buy_vol, sell_vol, close |
| **合计** | **66** | |

### 1.3 未被使用的 C++ 算子

以下算子已有 C++ kernel 但**没有任何因子使用**：

| 算子 | 类别 | 用途 |
|------|------|------|
| TS_VARI | P1 Rolling | 方差 (std² 的直接版本) |
| TS_PCT | P1 Rolling | 百分比变化 |
| TS_MINMAX_DIFF | P2 Bivariate | max - min (双端队列优化) |
| TS_SKEW | P2 | 偏度 |
| TS_MED | P3 | 中位数 |
| TS_MAD | P3 | 中位数绝对偏差 |
| TS_WMA | P3 | 加权移动平均 |
| TS_MAX_DIFF | P3 | close - rolling max (回撤) |
| TS_MIN_DIFF | P3 | close - rolling min (突破) |
| TREAP_TS_RANK | Optimized | O(log t) 排序 |

---

## 2. Operations.py 算子分析

### 2.1 完整算子清单与 FactorEngine 对标

#### ✅ 已有 — 无需新增

| Operations.py | FactorEngine C++ Op | 备注 |
|---------------|---------------------|------|
| `Add` | ADD | |
| `Sub` | SUB | |
| `Mul` | MUL | |
| `Div` | DIV | |
| `Neg` / `CsNeg` | NEG | |
| `Abs` / `CsAbs` | ABS | |
| `Log` | LOG | |
| `Sign` | SIGN | |
| `TsMean` | MA | |
| `TsSum` | TS_SUM | |
| `TsStd` | TS_STD | |
| `TsMax` | TS_MAX | |
| `TsMin` | TS_MIN | |
| `TsRank` | TS_RANK / TREAP_TS_RANK | |
| `TsZScore` | TS_ZSCORE | |
| `TsEMA` | EMA | |
| `TsDelay` | DELAY | |
| `TsDelta` | TS_DIFF | TsDelta = x - x[t-n] |
| `TsCor` | CORR | |
| `TsMedian` | TS_MED | ⚠️ 已有 kernel 但无因子使用 |
| `TsSkew` | TS_SKEW | ⚠️ 已有 kernel 但无因子使用 |
| `TsRange` | 可由 TS_MAX - TS_MIN 组合 | 不需要独立算子 |

#### 🔶 有价值 — 建议新增到 C++

| Operations.py | 建议 Op 名 | 优先级 | Crypto Trades 用途 |
|---------------|-----------|--------|-------------------|
| **`TsKurt`** | TS_KURT | ⭐⭐⭐ | 尾部风险检测：trades return 的峰度突变是异常交易活动的信号 |
| **`TsCov`** | COV | ⭐⭐⭐ | 买卖量协方差：buy_vol 与 price_change 的协变可捕捉 informed trading |
| **`TsBeta`** | TS_BETA | ⭐⭐⭐ | 回归斜率：vwap 对 close 的 beta 衡量大单冲击力度 |
| **`TsIntercept`** | TS_INTERCEPT | ⭐⭐ | 回归截距：与 TsBeta 配合使用 |
| **`TsNorm`** | TS_NORM | ⭐⭐ | Min-Max 归一化：将价格/量标准化到 [0,1] 区间，对 TS_RANK 的连续版本 |
| **`TsCV`** | TS_CV | ⭐⭐ | 变异系数 (std/mean)：trades 量的 CV 异常高意味着聚集性交易 |
| **`TsCos`** | TS_COS | ⭐⭐ | 余弦相似度：buy/sell 时序模式的方向性比较 |
| **`TsGrowth`** | — | ⭐ | (x - x[t-n]) / x[t-n]，可由 TS_DIFF + DIV 组合，优先级低 |
| **`TsRsi`** | TS_RSI | ⭐⭐⭐ | RSI 指标：crypto 经典技术指标，trades 级别 RSI 有独特价值 |
| **`TsMACD`** / `TsDIFF` / `TsDEA` | — | ⭐⭐ | MACD 系列：可由 EMA 组合实现，见"不做的事情"说明 |
| **`TsCCI`** | TS_CCI | ⭐⭐ | CCI 商品通道指数：适合 crypto 的超买超卖检测 |
| **`TsSumPos`** | TS_SUM_POS | ⭐⭐ | 正值求和：只累加正 return 的部分，计算上行动量 |
| **`TsPosRatio`** | TS_POS_RATIO | ⭐⭐ | 正比率：n 期内正 return 的占比，衡量趋势一致性 |
| **`TsUpDownRatio`** | TS_UP_DOWN_RATIO | ⭐⭐⭐ | 上下比：配合 trades 数据的 buy/sell 量，天然适合微观结构分析 |
| **`TsDeCor`** | — | ⭐ | 去相关：y - y*corr(x,y)，可由组合实现 |
| **`TsModulus`** | — | ⭐ | 模：sqrt(Σx²)，即 L2 范数，对价格路径的"能量"度量 |
| **`SignSqrt`** | SIGN_SQRT | ⭐ | sign(x)*sqrt(|x|)，压缩极端值的对称变换 |
| **`FMax` / `FMin`** | MAX2, MIN2 | ⭐ | 逐元素 max/min（binary），目前只有 rolling 版本 |

#### ❌ 不适用于 streaming 实时系统

| Operations.py | 原因 |
|---------------|------|
| `CsRank` / `CsZScore` / `CsMean` / `CsStd` 等 Cs* 截面算子 | 需要所有 symbol 同时参与计算，当前架构每个 symbol 独立推理 |
| `CsResidual` / `CsBeta` 截面回归 | 同上 |
| `TsBetaT` / `TsCorT` (时间趋势回归) | 可转为 TS_BETA(x, time_index)，但 time_index 需额外构造 |
| `TsBetaV` / `TsCorV` (V 型权重) | 非标准，优先级低 |
| `Resample` / `BackResample` | 数据预处理，不属于因子算子 |
| `Category_*` / `Group*` | 分组算子，需要行业/类别数据，crypto 不适用 |

---

## 3. Crypto Trades 因子扩展方向

### 3.1 现有 trades 输入字段

```
BarData → C++ InferenceEngine:
  close, volume, open, high, low, ret, vwap, buy_vol, sell_vol
```

### 3.2 高价值 trades 因子类别

#### A. 微观结构因子 (基于 buy_vol / sell_vol)

| 因子思路 | 涉及算子 | 描述 |
|----------|----------|------|
| Trade Imbalance 动量 | SUB, DIV, EMA, TS_DIFF | (buy-sell)/(buy+sell) 的 EMA 的趋势 |
| Buy/Sell Volume 比率的偏度 | DIV, TS_SKEW | buy/sell ratio 时序的偏度，捕捉持续单边交易 |
| Buy Volume 峰度突变 | SLOG1P, **TS_KURT** | buy_vol 的峰度 z-score，尾部事件检测 |
| **上下比率** | **TS_UP_DOWN_RATIO** | 基于 ret 正负分组的 buy_vol/sell_vol 聚合 |
| Buy/Sell 自相关 | AUTOCORR | buy_vol 的滞后自相关，检测持续性买入/卖出 |
| Net Volume 加速度 | SUB, EMA, TS_DIFF | (buy-sell) 的 EMA 二阶差分 |

#### B. VWAP 因子

| 因子思路 | 涉及算子 | 描述 |
|----------|----------|------|
| VWAP-Close 偏离趋势 | SUB, DIV, **TS_BETA** | vwap-close premium 对 time 的 beta |
| VWAP 波动率比 | TS_STD, DIV | TsStd(vwap) / TsStd(close)，大单冲击度量 |
| VWAP 均值回复强度 | EMA, SUB, **TS_RSI** | VWAP 的 RSI |
| VWAP-MA 交叉 | EMA, SUB, SIGN | EMA(vwap,fast) vs EMA(vwap,slow) |
| VWAP 变异系数 | **TS_CV** | TsStd(vwap)/TsMean(vwap)，聚集性度量 |

#### C. 量价关系因子 (kline + trades 混合)

| 因子思路 | 涉及算子 | 描述 |
|----------|----------|------|
| 量价背离 (trades 版) | CORR, TS_RANK | corr(ret, buy_vol-sell_vol) 的 rank |
| Buy Volume-Close Beta | **TS_BETA** | buy_vol 对 close 的回归斜率 |
| 成交效率 | ABS, TS_DIFF, DIV | |ret| / (buy_vol + sell_vol)，每单位交易量的价格移动 |
| VWAP 定价权 | **TS_COS** | vwap 和 close 变化方向的余弦相似度 |
| 量比异常 | TS_ZSCORE, DIV | (buy_vol/sell_vol) 的 z-score |

#### D. 技术指标因子

| 因子思路 | 涉及算子 | 描述 |
|----------|----------|------|
| Trades RSI | **TS_RSI** | 基于 trades-derived close 的 RSI |
| VWAP CCI | **TS_CCI** | 基于 vwap 的 CCI |
| Net Volume MACD | EMA (可组合) | (buy-sell) 的 MACD |
| Imbalance 趋势一致性 | **TS_POS_RATIO** | imbalance > 0 的比例 |

---

## 4. 实施计划

### Phase 1: 补齐未使用的 C++ 算子覆盖 (0.5 天) ✅ 已完成

为 9 个已有但从未使用的 C++ Op 编写因子（TS_PCT 已在 Phase 2 trades 因子中使用）：

| 步骤 | 工作 | 状态 |
|------|------|------|
| 1-1 | 为 TS_SKEW 写 3 个因子（0066 return skew、0067 volume skew、T0047 imbalance skew） | ✅ |
| 1-2 | 为 TS_MED 写 2 个因子（0068 median deviation、T0048 VWAP median） | ✅ |
| 1-3 | 为 TS_MAD 写 2 个因子（0069 robust volatility、T0048 VWAP MAD） | ✅ |
| 1-4 | 为 TS_WMA 写 2 个因子（0070 decay-weighted momentum、T0049 WMA volume） | ✅ |
| 1-5 | 为 TS_MAX_DIFF / TS_MIN_DIFF 写 4 个因子（0071/0072 kline、T0050/T0051 trades） | ✅ |
| 1-6 | 为 TS_MINMAX_DIFF 写 2 个因子（0073 kline、T0052 trades） | ✅ |
| 1-7 | 为 TS_VARI 写 2 个因子（0074 variance ratio、T0053 imbalance variance） | ✅ |
| 1-8 | 为 TREAP_TS_RANK 写 1 个因子（0075 large-window optimized rank） | ✅ |

**产出**：17 个新因子（kline 0066-0075 + trades T0047-T0053），100% kernel 覆盖达成

### Phase 2: 新增 C++ 算子 (1-2 天)

按优先级分批新增：

| 批次 | 算子 | 复杂度 | 估时 |
|------|------|--------|------|
| **Batch A (⭐⭐⭐)** | TS_KURT, TS_BETA, TS_RSI, TS_UP_DOWN_RATIO | 中 | 0.5 天 |
| **Batch B (⭐⭐)** | COV, TS_CV, TS_NORM, TS_CCI, TS_SUM_POS, TS_POS_RATIO | 中 | 0.5 天 |
| **Batch C (⭐)** | TS_COS, SIGN_SQRT, MAX2, MIN2, TS_INTERCEPT | 低 | 0.5 天 |

每个新算子需要：
1. C++ kernel 实现 (header-only in `ops/`)
2. `factor_graph.hpp` 中 Op enum 和 dispatch 注册
3. pybind11 绑定
4. Python 单元测试

### Phase 3: Trades 因子大规模扩展 (1 天)

目标：`okx_perp_trades` 从 6 个 → **30-50 个** trades 因子

| 因子组 | 数量 | 输入 |
|--------|------|------|
| 微观结构 (imbalance/flow) | ~10 | buy_vol, sell_vol |
| VWAP 衍生 | ~8 | vwap, close |
| 量价混合 | ~8 | close, volume, buy_vol, sell_vol, vwap |
| 技术指标 | ~6 | close, vwap (RSI/CCI/MACD) |
| 波动率/尾部 | ~6 | close, vwap (skew/kurt/mad) |

### Phase 4: 压测实验 (0.5 天)

| 场景 | 参数 | 目标 |
|------|------|------|
| 因子数量压测 | 200 factors × 300 symbols | 单 tick 推理耗时 <100ms |
| 高频压测 | 50 factors × 300 symbols × 1s tick | ClockDriver + 推理端到端延迟 |
| 内存压测 | 100 factors × 500 symbols | 峰值内存 |
| 算子覆盖压测 | 每个 C++ Op 至少被 2 个因子使用 | 100% kernel 覆盖率 |

---

## 5. 优先级排序总结

```
Week 1:
  ├── Phase 1: 补齐已有算子覆盖 (10 个新因子)         [0.5d]
  ├── Phase 2 Batch A: TS_KURT, TS_BETA, TS_RSI, ...  [0.5d]
  └── Phase 3 前半: 20 个 trades 因子                  [0.5d]

Week 2:
  ├── Phase 2 Batch B+C: 剩余新算子                    [1d]
  ├── Phase 3 后半: 剩余 trades 因子                   [0.5d]
  └── Phase 4: 全量压测                                [0.5d]
```

**最终目标**：
- C++ Op: 50 → **~65 个**（新增 ~15 个算子）
- 注册因子: 66 → **~120+ 个**（新增 ~55 个）
- trades 因子: 6 → **~40 个**
- 全部 C++ kernel 被至少 2 个因子覆盖
- 200 factors × 300 symbols × 5s tick 单 tick 推理 <100ms

---

## 6. 不做的事情

| 内容 | 原因 |
|------|------|
| 截面算子 (Cs*) | 架构不支持：当前 per-symbol 独立推理，没有 cross-sectional 通路。如需截面因子需要架构级改动（Phase 5+） |
| 回归残差 (CsResidual) | 同上 |
| Resample / BackResample | 数据预处理层面的事情，不属于因子算子 |
| 分组算子 (GroupSum 等) | crypto 没有行业分组概念 |
| V 型权重回归 (TsBetaV 等) | 非标准算子，优先级极低 |
| MACD 专用 kernel | 可由 EMA(close,12) - EMA(close,26) 组合实现，无需独立 Op；因子可直接用组合图写出 |
| Step 系列 (TsStepMean 等) | Operations.py 中的 step 参数用于降采样频率，我们的系统在 ClockDriver 层面控制频率，不需要算子层面做 |
| Agg 系列 (TsAggMean 等) | 单一截面聚合，不适合 streaming push 架构 |

## 7. 截面算子展望 (不在本期)

Operations.py 中有大量 Cs (Cross-Sectional) 算子，在传统股票多因子中非常重要：

| Cs 算子 | 用途 |
|---------|------|
| CsRank | 截面排名，消除量纲差异 |
| CsZScore | 截面标准化 |
| CsResidual | 截面回归残差（行业中性化） |
| CsDeMean | 截面去均值 |

**当前不做的原因**：FactorEngine 的 C++ `InferenceEngine` 采用 per-symbol 独立推理架构
（每个 symbol 有自己的 `SymbolRunner`，互不通信）。实现截面算子需要：
1. 在 `push_bars()` 所有 symbol 完成后，收集所有 symbol 的指定节点输出
2. 做截面计算
3. 将结果写回各 symbol

这是一个**架构级改动**，可作为 Phase 5 规划。对 crypto 来说，截面标准化的需求
相对较低（200-300 symbol 的截面远小于 A 股 5000+），但 CsRank 对因子合成仍有价值。
