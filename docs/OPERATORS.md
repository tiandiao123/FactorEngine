# FactorEngine 算子清单 (Operator Reference)

> 最后更新：2026-05-04 | 总算子数：**50+** | 总因子数：**123** (70 kline + 53 trades) | **100% kernel 覆盖**

本文档列出 FactorEngine C++ 引擎中所有已注册算子，包括实现位置、测试位置、数学表达式和功能描述。

---

## 目录

- [P0 — 无状态一元算子 (Unary Stateless)](#p0--无状态一元算子-unary-stateless)
- [P0 — 无状态二元算子 (Binary Stateless)](#p0--无状态二元算子-binary-stateless)
- [P0 — 标量运算 (Scalar Ops)](#p0--标量运算-scalar-ops)
- [P1 — 滚动算子 (Rolling Single-Input)](#p1--滚动算子-rolling-single-input)
- [P2 — 双变量/特殊算子 (Bivariate / Special)](#p2--双变量特殊算子-bivariate--special)
- [P2b — 滚动归一化算子](#p2b--滚动归一化算子)
- [P2c — 动量/技术指标](#p2c--动量技术指标)
- [P2d — 条件滚动算子](#p2d--条件滚动算子)
- [P3 — 复杂算子](#p3--复杂算子)
- [衍生/别名算子](#衍生别名算子)
- [组合因子 (Composite Factors)](#组合因子-composite-factors)
- [全局常量](#全局常量)
- [输入特征](#输入特征)
- [文件索引](#文件索引)

---

## P0 — 无状态一元算子 (Unary Stateless)

无状态算子没有 rolling kernel，每次 push 直接计算标量函数。

### NEG — 取负

| 属性 | 值 |
|------|-----|
| **Op 名** | `NEG` |
| **实现** | `native/include/fe/ops/unary.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **公式** | `neg(x) = -x` |

### ABS — 绝对值

| 属性 | 值 |
|------|-----|
| **Op 名** | `ABS` |
| **实现** | `native/include/fe/ops/unary.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **公式** | `abs(x) = \|x\|` |

### LOG — 自然对数

| 属性 | 值 |
|------|-----|
| **Op 名** | `LOG` |
| **实现** | `native/include/fe/ops/unary.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **公式** | `log(x) = ln(x)`，`x ≤ 0` 返回 NaN |

### SQR — 平方

| 属性 | 值 |
|------|-----|
| **Op 名** | `SQR` |
| **实现** | `native/include/fe/ops/unary.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **公式** | `sqr(x) = x²` |

### INV — 倒数

| 属性 | 值 |
|------|-----|
| **Op 名** | `INV` |
| **实现** | `native/include/fe/ops/unary.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **公式** | `inv(x) = 1 / (x + ε)`，`ε = 1e-8` 防止除零 |

### SIGN — 符号

| 属性 | 值 |
|------|-----|
| **Op 名** | `SIGN` |
| **实现** | `native/include/fe/ops/unary.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **公式** | `sign(x) = {+1 if x>0, -1 if x<0, 0 if x=0}` |

### TANH — 双曲正切

| 属性 | 值 |
|------|-----|
| **Op 名** | `TANH` |
| **实现** | `native/include/fe/ops/unary.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **公式** | `tanh(x) = (eˣ - e⁻ˣ) / (eˣ + e⁻ˣ)`，输出范围 `(-1, 1)` |

### SLOG1P — 符号对数

| 属性 | 值 |
|------|-----|
| **Op 名** | `SLOG1P` |
| **实现** | `native/include/fe/ops/unary.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **公式** | `slog1p(x) = sign(x) × ln(1 + \|x\|)`，对称的对数压缩 |

### SQRT — 平方根 ⭐ *新增*

| 属性 | 值 |
|------|-----|
| **Op 名** | `SQRT` |
| **实现** | `native/include/fe/ops/unary.hpp` → `sqrt_op()`, `sqrt_array()` |
| **Pybind** | `native/pybind/fe_ops_bind.cpp` → `sqrt_array` |
| **测试** | `tests/custom_kernels/stateless_ops/test_sqrt_sign_sqrt.py` |
| **公式** | `sqrt(x) = √x`，`x < 0` 返回 NaN |

C++ 实现:
```cpp
inline FeFloat sqrt_op(FeFloat x) {
    return (x < 0) ? std::numeric_limits<FeFloat>::quiet_NaN()
                    : static_cast<FeFloat>(std::sqrt(static_cast<double>(x)));
}
```

用于组合因子 TsModulus（路径能量）: `Sqrt(TsSum(Sqr(x), w))`。

### SIGN_SQRT — 符号平方根 ⭐ *新增*

| 属性 | 值 |
|------|-----|
| **Op 名** | `SIGN_SQRT` |
| **实现** | `native/include/fe/ops/unary.hpp` → `sign_sqrt()`, `sign_sqrt_array()` |
| **Pybind** | `native/pybind/fe_ops_bind.cpp` → `sign_sqrt_array` |
| **测试** | `tests/custom_kernels/stateless_ops/test_sqrt_sign_sqrt.py` |
| **公式** | `sign_sqrt(x) = sign(x) × √\|x\|` |

C++ 实现:
```cpp
inline FeFloat sign_sqrt(FeFloat x) {
    if (fe_is_nan(x)) return x;
    double ax = std::abs(static_cast<double>(x));
    double r  = std::sqrt(ax);
    return static_cast<FeFloat>(x < 0 ? -r : r);
}
```

对称的极端值压缩变换。相比 `SLOG1P`，`SIGN_SQRT` 对大值的压缩更激进：

| x | sign_sqrt(x) | slog1p(x) |
|---|---|---|
| 100 | 10.0 | 4.62 |
| -100 | -10.0 | -4.62 |
| 10000 | 100.0 | 9.21 |

---

## P0 — 无状态二元算子 (Binary Stateless)

### ADD / SUB / MUL / DIV — 四则运算

| 属性 | 值 |
|------|-----|
| **Op 名** | `ADD`, `SUB`, `MUL`, `DIV` |
| **实现** | `native/include/fe/ops/binary.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **公式** | `a + b`, `a - b`, `a × b`, `a / (b + ε)` |

`DIV` 分母加 `ε = 1e-8` 防止除零。

### FMAX — 逐元素最大值 ⭐ *新增*

| 属性 | 值 |
|------|-----|
| **Op 名** | `FMAX` |
| **实现** | `native/include/fe/ops/binary.hpp` → `fmax_op()`, `fmax_aa()` |
| **Pybind** | `native/pybind/fe_ops_bind.cpp` → `fmax_aa` |
| **测试** | `tests/custom_kernels/stateless_ops/test_fmax_fmin.py` |
| **公式** | `fmax(a, b) = max(a, b)`，任一输入为 NaN 则返回 NaN |

C++ 实现:
```cpp
inline FeFloat fmax_op(FeFloat a, FeFloat b) {
    if (fe_is_nan(a) || fe_is_nan(b))
        return std::numeric_limits<FeFloat>::quiet_NaN();
    return (a >= b) ? a : b;
}
```

用于因子组合，例如 `FMax(ret, volume_chg)`。

### FMIN — 逐元素最小值 ⭐ *新增*

| 属性 | 值 |
|------|-----|
| **Op 名** | `FMIN` |
| **实现** | `native/include/fe/ops/binary.hpp` → `fmin_op()`, `fmin_aa()` |
| **Pybind** | `native/pybind/fe_ops_bind.cpp` → `fmin_aa` |
| **测试** | `tests/custom_kernels/stateless_ops/test_fmax_fmin.py` |
| **公式** | `fmin(a, b) = min(a, b)`，任一输入为 NaN 则返回 NaN |

---

## P0 — 标量运算 (Scalar Ops)

| Op 名 | 公式 | 说明 |
|--------|------|------|
| `ADD_SCALAR` | `x + s` | 节点 + 标量 |
| `SUB_SCALAR` | `x - s` | 节点 - 标量 |
| `MUL_SCALAR` | `x × s` | 节点 × 标量 |
| `DIV_SCALAR` | `x / (s + ε)` | 节点 / 标量 |
| `SCALAR_SUB` | `s - x` | 标量 - 节点 |
| `SCALAR_DIV` | `s / (x + ε)` | 标量 / 节点 |

**实现**: `native/include/fe/ops/binary.hpp` + `native/include/fe/runtime/factor_graph.hpp` (dispatch)

---

## P1 — 滚动算子 (Rolling Single-Input)

单输入、有状态的滚动窗口算子。每个都有一个 push-level kernel 类。

### MA — 简单移动平均

| 属性 | 值 |
|------|-----|
| **Op 名** | `MA` |
| **Kernel** | `RollingMeanKernel` |
| **实现** | `native/include/fe/ops/rolling_mean.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1), output O(1) |
| **公式** | `MA(x, w) = (1/w) × Σᵢ₌ₜ₋ᵥ₊₁ᵗ xᵢ` |

使用 ring buffer + running sum 实现增量更新。warmup 阶段 (`count < w`) 返回 NaN。

### TS_SUM — 滚动求和

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_SUM` |
| **Kernel** | `RollingSumKernel` |
| **实现** | `native/include/fe/ops/rolling_sum.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1), output O(1) |
| **公式** | `TS_SUM(x, w) = Σᵢ₌ₜ₋ᵥ₊₁ᵗ xᵢ` |

### TS_STD — 滚动标准差

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_STD` |
| **Kernel** | `RollingStdKernel` |
| **实现** | `native/include/fe/ops/rolling_std.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1), output O(1) |
| **公式** | `TS_STD(x, w) = √(Var(x, w))`，`ddof=1` |

使用 Welford's online algorithm 的变体，维护 `sum_x` 和 `sum_x²`:

```
Var = (sum_x² - sum_x² / w) / (w - 1)
```

### TS_VARI — 滚动方差

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_VARI` |
| **Kernel** | `RollingVarComposite` (复用 `RollingStdKernel`) |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1), output O(1) |
| **公式** | `TS_VARI(x, w) = STD(x, w)²` |

组合实现：output 时对 STD 结果平方。

### EMA — 指数移动平均

| 属性 | 值 |
|------|-----|
| **Op 名** | `EMA` |
| **Kernel** | `EmaKernel` |
| **实现** | `native/include/fe/ops/rolling_ema.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1), output O(1) |
| **公式** | `EMA(x, span) = α × xₜ + (1-α) × EMAₜ₋₁`，`α = 2/(span+1)` |

第一个有效值作为种子。用于构建 MACD 系列因子。

### TS_MIN / TS_MAX — 滚动最小/最大

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_MIN`, `TS_MAX` |
| **Kernel** | `RollingMinKernel`, `RollingMaxKernel` (基于 `RollingExtremalKernel<IsMax>`) |
| **实现** | `native/include/fe/ops/rolling_minmax.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1) 均摊, output O(1) |
| **公式** | `TS_MIN(x, w) = min(xₜ₋ᵥ₊₁, ..., xₜ)` |

基于单调双端队列 (monotonic deque) 实现，最坏 O(w) 但均摊 O(1)。

### TS_RANK — 滚动百分位排名

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_RANK` / `TREAP_TS_RANK` |
| **Kernel** | `TsRankPush` (O(w)) / `TreapTsRankPush` (O(log w)) |
| **实现** | `native/include/fe/runtime/kernels.hpp`, `native/include/fe/ops/treap_rank.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | `TS_RANK`: output O(w) / `TREAP_TS_RANK`: push O(log w) + output O(log w) |
| **公式** | `TS_RANK(x, w) = (rank of xₜ in window) / w` |

使用 mid-rank 方法处理 tie。`TREAP_TS_RANK` 是优化版本，使用 Treap order-statistic tree。

### TS_ZSCORE — 滚动 Z-Score

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_ZSCORE` |
| **Kernel** | `TsZscoreComposite` |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1), output O(1) |
| **公式** | `TS_ZSCORE(x, w) = (xₜ - MA(x, w)) / (STD(x, w) + ε)` |

组合实现：复用 `RollingMeanKernel` + `RollingStdKernel`。

### DELAY — 延迟

| 属性 | 值 |
|------|-----|
| **Op 名** | `DELAY` |
| **Kernel** | `DelayKernel` |
| **实现** | `native/include/fe/ops/shift.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1), output O(1) |
| **公式** | `DELAY(x, t) = xₜ₋ₜ` |

ring buffer 实现的固定延迟。

### TS_DIFF — 差分

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_DIFF` |
| **Kernel** | `TsDiffComposite` |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1), output O(1) |
| **公式** | `TS_DIFF(x, t) = xₜ - xₜ₋ₜ` |

### TS_PCT — 百分比变化

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_PCT` |
| **Kernel** | `TsPctComposite` |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1), output O(1) |
| **公式** | `TS_PCT(x, t) = xₜ / (xₜ₋ₜ + ε) - 1` |

---

## P2 — 双变量/特殊算子 (Bivariate / Special)

### CORR — 滚动 Pearson 相关系数

| 属性 | 值 |
|------|-----|
| **Op 名** | `CORR` |
| **Kernel** | `CorrPush` |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **Batch** | `native/include/fe/ops/bivariate.hpp` → `rolling_corr()` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1), output O(1) |
| **窗口限制** | `w ≥ 2` |
| **公式** | `CORR(x, y, w) = Cov(x,y) / (σₓ × σᵧ)` |

维护 `sx, sy, sxx, syy, sxy` 五个 running sum:

```
cov_xy  = (w × sxy - sx × sy) / (w × (w-1))
var_x   = (w × sxx - sx²) / (w × (w-1))
var_y   = (w × syy - sy²) / (w × (w-1))
corr    = cov_xy / sqrt(var_x × var_y)
```

### TS_COV — 滚动协方差 ⭐

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_COV` |
| **Kernel** | `CovPush` |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **Batch** | `native/include/fe/ops/bivariate.hpp` → `rolling_cov()` |
| **测试** | `tests/custom_kernels/ts_cov/test_ts_cov_alignment.py` |
| **Benchmark** | `tests/custom_kernels/ts_cov/bench_ts_cov.py` |
| **复杂度** | push O(1), output O(1) |
| **窗口限制** | `w ≥ 2` |
| **公式** | `TS_COV(x, y, w) = (1/(w-1)) × [Σxy - (Σx × Σy)/w]`，`ddof=1` |

**Benchmark**: batch ~2500x faster than pandas `rolling(w).cov()`

### TS_BETA — 滚动 OLS 斜率 ⭐

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_BETA` |
| **Kernel** | `BetaPush` (继承 `OlsStatsPush` 基类) |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **Batch** | `native/include/fe/ops/bivariate.hpp` → `rolling_beta()` |
| **测试** | `tests/custom_kernels/ts_beta/test_ts_beta_alignment.py` |
| **Benchmark** | `tests/custom_kernels/ts_beta/bench_ts_beta.py` |
| **文档** | `tests/custom_kernels/ts_beta/README.md` |
| **复杂度** | push O(1), output O(1) |
| **窗口限制** | `w ≥ 2` |

**数学公式**:
```
β = Cov(x, y) / Var(x)
  = (w × Σxy - Σx × Σy) / (w × Σx² - (Σx)²)
```

`OlsStatsPush` 基类维护 `sx, sy, sxx, sxy` 四个增量 sum，`BetaPush` 和 `InterceptPush` 共享：

```cpp
class OlsStatsPush {
protected:
    RingBuf<double> rx_, ry_;
    double sx_{}, sy_{}, sxx_{}, sxy_{};
    int count_{};
    // push(x, y) 增量更新四个 sum
};

class BetaPush : public OlsStatsPush {
    FeFloat output() {
        double denom = w * sxx_ - sx_ * sx_;
        return static_cast<FeFloat>((w * sxy_ - sx_ * sy_) / denom);
    }
};
```

**Benchmark**: push ~1500 ns/call，batch ~3200x faster than pandas

### TS_INTERCEPT — 滚动 OLS 截距 ⭐

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_INTERCEPT` |
| **Kernel** | `InterceptPush` (继承 `OlsStatsPush` 基类) |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **Batch** | `native/include/fe/ops/bivariate.hpp` → `rolling_intercept()` |
| **测试** | `tests/custom_kernels/ts_intercept/test_ts_intercept_alignment.py` |
| **Benchmark** | `tests/custom_kernels/ts_intercept/bench_ts_intercept.py` |
| **复杂度** | push O(1), output O(1) |
| **窗口限制** | `w ≥ 2` |

**数学公式**:
```
α = mean(y) - β × mean(x)
  = (Σy - β × Σx) / w
```

与 `BetaPush` 共享 `OlsStatsPush` 基类，output 时先算 β 再推导截距。

### TS_COS — 滚动余弦相似度 ⭐

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_COS` |
| **Kernel** | `RollingCosKernel` |
| **实现** | `native/include/fe/ops/bivariate.hpp` |
| **测试** | `tests/custom_kernels/ts_cos/test_ts_cos_alignment.py` |
| **Benchmark** | `tests/custom_kernels/ts_cos/bench_ts_cos.py` |
| **复杂度** | push O(1), output O(1) |
| **窗口限制** | `w ≥ 2` |

**数学公式**:
```
COS(x, y, w) = Σ(xᵢ × yᵢ) / (√Σxᵢ² × √Σyᵢ²)
```

与 CORR 不同，COS **不做中心化** (不减均值)。维护 `sxx, syy, sxy` 三个增量 sum:

```cpp
FeFloat output() const {
    double denom = std::sqrt(sxx_ * syy_);
    return (denom < 1e-30) ? NAN : static_cast<FeFloat>(sxy_ / denom);
}
```

### AUTOCORR — 自相关

| 属性 | 值 |
|------|-----|
| **Op 名** | `AUTOCORR` |
| **Kernel** | `AutocorrPush` |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1), output O(1) |
| **公式** | `AUTOCORR(x, w, lag) = CORR(x, DELAY(x, lag), w)` |

组合实现：`DelayKernel` + `CorrPush`。

### TS_MINMAX_DIFF — 极差

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_MINMAX_DIFF` |
| **Kernel** | `TsMinMaxDiffPush` |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1) 均摊, output O(1) |
| **公式** | `TS_MINMAX_DIFF(x, w) = max(x, w) - min(x, w)` |

内部复用两个 `RollingExtremalKernel`（双单调队列）。

### TS_SKEW — 滚动偏度

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_SKEW` |
| **Kernel** | `TsSkewPush` |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **Batch** | `native/include/fe/ops/rolling_skew.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1), output **O(w)** |
| **窗口限制** | `w ≥ 3` |

**数学公式** (Fisher-Pearson 标准化偏度):
```
γ₁ = [n × √(n-1) / (n-2)] × m₃ / m₂^(3/2)

m₂ = (1/n) × Σ(xᵢ - x̄)²
m₃ = (1/n) × Σ(xᵢ - x̄)³
```

push 时 O(1) 维护 ring buffer + running sum，output 时 O(w) 遍历计算二阶和三阶中心矩。

### TS_KURT — 滚动峰度 ⭐

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_KURT` |
| **Kernel** | `TsKurtPush` |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **Batch** | `native/include/fe/ops/rolling_kurt.hpp` |
| **测试** | `tests/custom_kernels/ts_kurt/test_ts_kurt_alignment.py` |
| **Benchmark** | `tests/custom_kernels/ts_kurt/bench_ts_kurt.py` |
| **复杂度** | push O(1), output **O(w)** |
| **窗口限制** | `w ≥ 4` |

**数学公式** (Fisher excess kurtosis):
```
κ = [n(n+1)(n-1) / ((n-2)(n-3))] × m₄/m₂² - 3(n-1)² / ((n-2)(n-3))

m₂ = (1/n) × Σ(xᵢ - x̄)²
m₄ = (1/n) × Σ(xᵢ - x̄)⁴
```

与 `pandas.Series.rolling(w).kurt()` 对齐。output O(w) 因为需要遍历 ring buffer 重新计算 m₂ 和 m₄（无法仅靠 running sum 增量维护四阶中心矩）。

---

## P2b — 滚动归一化算子

### TS_CV — 变异系数 ⭐

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_CV` |
| **Kernel** | `TsCvComposite` (复用 `RollingMeanKernel` + `RollingStdKernel`) |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **Batch** | `native/include/fe/ops/rolling_cv.hpp` |
| **测试** | `tests/custom_kernels/ts_cv/test_ts_cv_alignment.py` |
| **Benchmark** | `tests/custom_kernels/ts_cv/bench_ts_cv.py` |
| **复杂度** | push O(1), output O(1) |
| **窗口限制** | `w ≥ 2` |

**数学公式**:
```
CV(x, w) = STD(x, w) / |MA(x, w)|
```

当 `|mean| < kCvMeanEps (1e-12)` 时返回 NaN（避免除以极小值导致的数值不稳定）。

### TS_NORM — Min-Max 归一化 ⭐

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_NORM` |
| **Kernel** | `TsNormComposite` (复用 `RollingMaxKernel` + `RollingMinKernel`) |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **Batch** | `native/include/fe/ops/rolling_norm.hpp` |
| **测试** | `tests/custom_kernels/ts_norm/test_ts_norm_alignment.py` |
| **Benchmark** | `tests/custom_kernels/ts_norm/bench_ts_norm.py` |
| **复杂度** | push O(1) 均摊, output O(1) |
| **窗口限制** | `w ≥ 2` |

**数学公式**:
```
NORM(x, w) = (xₜ - min(x, w)) / (max(x, w) - min(x, w))
```

输出范围 `[0, 1]`。`max == min`（常值窗口）时返回 NaN。

---

## P2c — 动量/技术指标

### TS_RSI — 相对强弱指数 ⭐

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_RSI` |
| **Kernel** | `RsiKernel` |
| **实现** | `native/include/fe/ops/rolling_rsi.hpp` |
| **测试** | `tests/custom_kernels/ts_rsi/test_ts_rsi_alignment.py` |
| **Benchmark** | `tests/custom_kernels/ts_rsi/bench_ts_rsi.py` |
| **复杂度** | push O(1), output O(1) |
| **窗口限制** | `w ≥ 2` |

**数学公式** (Wilder's RSI):
```
RSI = 100 - 100 / (1 + RS)
RS  = avg_gain / avg_loss
```

两阶段处理:

1. **种子阶段** (前 `w+1` 个 push): 用 SMA 计算初始 avg_gain / avg_loss
```
avg_gain₀ = sum(gains[0:w]) / w
avg_loss₀ = sum(losses[0:w]) / w
```

2. **平滑阶段** (后续 push): Wilder 指数平滑
```
avg_gain = (prev_avg_gain × (w-1) + gain) / w
avg_loss = (prev_avg_loss × (w-1) + loss) / w
```

内部使用 `double` 精度累加 (`avg_gain_`, `avg_loss_`, `gains_sum_`, `losses_sum_`) 以避免浮点误差累积。

### TS_CCI — 商品通道指数 ⭐ *新增*

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_CCI` |
| **Kernel** | `CciKernel` |
| **实现** | `native/include/fe/ops/rolling_cci.hpp` |
| **测试** | `tests/custom_kernels/ts_cci/test_ts_cci_alignment.py` |
| **Benchmark** | `tests/custom_kernels/ts_cci/bench_ts_cci.py` |
| **复杂度** | push O(1), output **O(w)** |
| **窗口限制** | `w ≥ 2` |

**数学公式** (Lambert's CCI):
```
TP  = (High + Low + Close) / 3
CCI = (TP - SMA(TP, w)) / (0.015 × MAD(TP, w))
MAD = (1/w) × Σ|TPᵢ - SMA(TP, w)|
```

- Lambert 常数 `0.015` 使约 70-80% 的 CCI 值落在 `[-100, +100]`
- `MAD < kCciMadEps (1e-12)` 时返回 `0.0`（常值序列无波动）
- push O(1) 维护 ring buffer + running sum，output O(w) 因为 MAD(Mean Absolute Deviation) 无法增量计算

实际应用中，CCI 作为单输入算子，**Typical Price 在 Python 侧组合** (因子表达式中先用 `Add`, `Div` 组合 TP)，然后传入 `TS_CCI`。

**Benchmark**: batch ~370x faster than pandas

---

## P2d — 条件滚动算子

### TS_SUM_POS — 正值求和 ⭐ *新增*

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_SUM_POS` |
| **Kernel** | `SumPosKernel` |
| **实现** | `native/include/fe/ops/rolling_sumpos.hpp` |
| **测试** | `tests/custom_kernels/ts_sumpos/test_sumpos_alignment.py` |
| **Benchmark** | `tests/custom_kernels/ts_sumpos/bench_sumpos.py` |
| **复杂度** | push O(1), output O(1) |
| **窗口限制** | `w ≥ 1` |

**数学公式**:
```
TS_SUM_POS(x, w) = Σ max(xᵢ, 0)    for i ∈ [t-w+1, t]
```

只累加严格正值 (`x > 0`)。零和负值不计入。push 时增量更新 `sum_pos_`:

```cpp
void push(FeFloat x) {
    if (count_ >= w_) {
        FeFloat old = ring_[pos_];
        if (old > 0) sum_pos_ -= static_cast<double>(old);
    }
    ring_[pos_] = x;
    if (!fe_is_nan(x) && x > 0) sum_pos_ += static_cast<double>(x);
    // ...
}
```

**Benchmark**: batch ~391x faster than pandas

### TS_POS_RATIO — 正值比率 ⭐ *新增*

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_POS_RATIO` |
| **Kernel** | `PosRatioKernel` |
| **实现** | `native/include/fe/ops/rolling_sumpos.hpp` |
| **测试** | `tests/custom_kernels/ts_sumpos/test_sumpos_alignment.py` |
| **Benchmark** | `tests/custom_kernels/ts_sumpos/bench_sumpos.py` |
| **复杂度** | push O(1), output O(1) |
| **窗口限制** | `w ≥ 1` |

**数学公式**:
```
TS_POS_RATIO(x, w) = count(xᵢ > 0) / w    for i ∈ [t-w+1, t]
```

输出范围 `[0.0, 1.0]`。衡量窗口内正值的比例，即趋势一致性。

**Benchmark**: batch ~934x faster than pandas

### TS_UP_DOWN_RATIO — 上下比 ⭐ *新增*

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_UP_DOWN_RATIO` |
| **Kernel** | `UpDownRatioKernel` |
| **实现** | `native/include/fe/ops/rolling_sumpos.hpp` |
| **测试** | `tests/custom_kernels/ts_sumpos/test_sumpos_alignment.py` |
| **复杂度** | push O(1), output O(1) |
| **窗口限制** | `w ≥ 1` |

**数学公式**:
```
TS_UP_DOWN_RATIO(x, w) = Σ(xᵢ where xᵢ > 0) / Σ(|xᵢ| where xᵢ < 0)
```

- `ratio > 1`: 上行力量主导
- `ratio < 1`: 下行力量主导
- `ratio = 1`: 多空平衡
- 当窗口内无负值时返回 NaN（分母为零）

配合 trades 数据的 `buy_vol - sell_vol`，天然适合微观结构分析。

---

## P3 — 复杂算子

### TS_MED — 滚动中位数

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_MED` |
| **Kernel** | `TsMedPush` |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **Batch** | `native/include/fe/ops/rolling_median.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(w), output O(1) |

使用排序缓冲区 + 二分插入/删除维护有序窗口。

### TS_MAD — 滚动中位数绝对偏差

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_MAD` |
| **Kernel** | `TsMadPush` |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **Batch** | `native/include/fe/ops/rolling_median.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(w), output O(w log w) |
| **公式** | `TS_MAD(x, w) = median(\|xᵢ - median(x)\|)` |

### TS_WMA — 线性加权移动平均

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_WMA` |
| **Kernel** | `TsWmaPush` |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **Batch** | `native/include/fe/ops/rolling_wma.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1), output O(w) |
| **公式** | `TS_WMA(x, w) = Σ(k × xₖ) / Σk`，权重 `k` 从 `1` (最旧) 线性递增到 `w` (最新) |

### TS_MAX_DIFF / TS_MIN_DIFF — 当前值与极值之差

| 属性 | 值 |
|------|-----|
| **Op 名** | `TS_MAX_DIFF`, `TS_MIN_DIFF` |
| **Kernel** | `TsMaxDiffPush`, `TsMinDiffPush` |
| **实现** | `native/include/fe/runtime/kernels.hpp` |
| **Batch** | `native/include/fe/ops/rolling_extremal.hpp` |
| **测试** | `tests/kernels/test_ops_alignment.py` |
| **复杂度** | push O(1) 均摊, output O(1) |
| **公式** | `TS_MAX_DIFF(x, w) = xₜ - max(x, w)`，`TS_MIN_DIFF(x, w) = xₜ - min(x, w)` |

---

## 衍生/别名算子

| Op 名 | 等价于 | 说明 |
|--------|--------|------|
| `PCT_CHANGE` | `TS_PCT(x, 1)` | 单步百分比变化 |
| `TS_GROWTH` | `TS_PCT(x, n)` | 语义别名: `(x - x[t-n]) / (x[t-n] + ε)` |

**TS_GROWTH 测试**: `tests/custom_kernels/ts_growth/test_ts_growth_alignment.py`

---

## 组合因子 (Composite Factors)

以下因子由现有算子组合而成，不需要新的 C++ kernel，在 Python 层通过 factor builder 表达式定义。

### MACD 系列 (T0024-T0028)

**注册位置**: `factorengine/factors/okx_perp_trades/factor_bank.py`

| ID | 名称 | 表达式 |
|----|------|--------|
| T0024 | MACD Line | `Sub(Ema(close, 12), Ema(close, 26))` |
| T0025 | MACD Signal | `Ema(Sub(Ema(c,12),Ema(c,26)), 9)` |
| T0026 | MACD Z-Score Rank | `Neg(TsRank(TsZscore(MACD_line, 120), 120))` |
| T0027 | MACD Histogram | `Sub(MACD_line, signal)` |
| T0028 | MACD Divergence | `Sub(TsZscore(close,120), TsZscore(MACD_line,120))` |

### TsDeCor 系列 (T0039-T0040)

去相关化因子：`y - corr(x, y, w) × y`，消除两个序列间的线性依赖。

| ID | 名称 | 表达式 |
|----|------|--------|
| T0039 | De-correlated VWAP | `Sub(vwap, Mul(Corr(close, vwap, 60), vwap))` |
| T0040 | De-correlated Volume | `Sub(SLog1p(vol), Mul(Corr(ret, SLog1p(vol), 60), SLog1p(vol)))` |

### TsModulus 系列 (T0041-T0042)

路径能量/L2范数：`√Σ(x²)`，度量序列在窗口内的"波动能量"。

| ID | 名称 | 表达式 |
|----|------|--------|
| T0041 | Price Path Modulus | `Sqrt(TsSum(Sqr(ret), 30))` |
| T0042 | Volume Path Modulus | `Sqrt(TsSum(Sqr(TsPct(SLog1p(volume), 1)), 30))` |

### SignSqrt 系列 (T0043-T0044)

极端值压缩：`sign(x) × √|x|`，保留方向的同时压缩幅度。

| ID | 名称 | 表达式 |
|----|------|--------|
| T0043 | SignSqrt Return | `SignSqrt(ret)` |
| T0044 | SignSqrt Imbalance | `SignSqrt(Sub(buy_vol, sell_vol))` |

### 其他组合 (T0045-T0046)

| ID | 名称 | 表达式 | 说明 |
|----|------|--------|------|
| T0045 | Saturated Imbalance | `Tanh(Div(Sub(bv,sv),Add(bv,sv)))` | Tanh 平滑压缩到 `[-1,1]` |
| T0046 | FMax(ret, vol_chg) | `FMax(ret, TsPct(SLog1p(volume), 1))` | 选择 ret 和 vol_chg 中较大者 |

---

## 全局常量

定义在 `native/include/fe/ops/spec.hpp`:

| 常量 | 值 | 用途 |
|------|-----|------|
| `kEps` | `1e-8f` | DIV, INV, TS_PCT 等分母保护 |
| `kCvMeanEps` | `1e-12` | TS_CV 均值过小时返回 NaN |
| `kRsiLossEps` | `1e-15` | RSI avg_loss 为零的判断阈值 |
| `kCciLambert` | `0.015` | CCI Lambert 常数 |
| `kCciMadEps` | `1e-12` | CCI MAD 过小时返回 0 |

---

## 输入特征

FactorGraph 支持 9 个输入特征，通过 `add_input("feature_name")` 注册:

| 特征 | Op 名 | 来源 |
|------|--------|------|
| `close` | `INPUT_CLOSE` | Kline / Trades |
| `volume` | `INPUT_VOLUME` | Kline / Trades |
| `open` | `INPUT_OPEN` | Kline / Trades |
| `high` | `INPUT_HIGH` | Kline / Trades |
| `low` | `INPUT_LOW` | Kline / Trades |
| `ret` | `INPUT_RET` | 计算得出: `close[t]/close[t-1] - 1` |
| `vwap` | `INPUT_VWAP` | Trades only |
| `buy_vol` | `INPUT_BUY_VOL` | Trades only |
| `sell_vol` | `INPUT_SELL_VOL` | Trades only |

---

## 文件索引

### C++ 头文件 (`native/include/fe/`)

| 文件 | 包含的算子 |
|------|----------|
| `ops/spec.hpp` | 全局常量、`FeFloat` 类型、`fe_is_nan` |
| `ops/unary.hpp` | NEG, ABS, LOG, SQR, INV, SIGN, TANH, SLOG1P, **SQRT**, **SIGN_SQRT** |
| `ops/binary.hpp` | ADD, SUB, MUL, DIV, **FMAX**, **FMIN** |
| `ops/rolling_mean.hpp` | MA → `RollingMeanKernel` |
| `ops/rolling_sum.hpp` | TS_SUM → `RollingSumKernel` |
| `ops/rolling_std.hpp` | TS_STD → `RollingStdKernel` |
| `ops/rolling_ema.hpp` | EMA → `EmaKernel` |
| `ops/rolling_minmax.hpp` | TS_MIN, TS_MAX → `RollingExtremalKernel<IsMax>` |
| `ops/rolling_rank.hpp` | batch `rolling_rank` |
| `ops/rolling_zscore.hpp` | batch `rolling_zscore` |
| `ops/shift.hpp` | DELAY → `DelayKernel`, `ts_diff`, `ts_pct`, `ts_growth` |
| `ops/bivariate.hpp` | CORR, TS_COV, TS_BETA, TS_INTERCEPT (batch); **TS_COS** → `RollingCosKernel` |
| `ops/rolling_skew.hpp` | batch `rolling_skew` |
| `ops/rolling_kurt.hpp` | batch `rolling_kurt` |
| `ops/rolling_cv.hpp` | batch `rolling_cv` |
| `ops/rolling_norm.hpp` | batch `rolling_norm` |
| `ops/rolling_rsi.hpp` | **TS_RSI** → `RsiKernel` |
| `ops/rolling_cci.hpp` | **TS_CCI** → `CciKernel` |
| `ops/rolling_sumpos.hpp` | **TS_SUM_POS** → `SumPosKernel`, **TS_POS_RATIO** → `PosRatioKernel`, **TS_UP_DOWN_RATIO** → `UpDownRatioKernel` |
| `ops/rolling_median.hpp` | batch `rolling_median`, `rolling_mad` |
| `ops/rolling_wma.hpp` | batch `rolling_wma` |
| `ops/rolling_extremal.hpp` | batch `ts_max_diff`, `ts_min_diff` |
| `ops/treap_rank.hpp` | `TreapOrderStatistic` (O(log w) order-statistic tree) |
| `runtime/kernels.hpp` | 所有 push-level kernel 类和组合类 |
| `runtime/factor_graph.hpp` | `Op` 枚举、`FactorGraph` DAG、push dispatch |

### Pybind 绑定 (`native/pybind/`)

| 文件 | 绑定内容 |
|------|---------|
| `fe_ops_bind.cpp` | 所有 batch 函数 (rolling_*, sqrt_array, sign_sqrt_array, fmax_aa, fmin_aa, ...) |
| `fe_runtime_bind.cpp` | Op 枚举、FactorGraph、InferenceEngine |

### 测试文件 (`tests/custom_kernels/`)

| 目录 | 覆盖算子 | 文件类型 |
|------|---------|---------|
| `ts_kurt/` | TS_KURT | alignment, benchmark, trade factor |
| `ts_cov/` | TS_COV | alignment, benchmark, factor graph |
| `ts_beta/` | TS_BETA | alignment, benchmark, factor graph, trade factor |
| `ts_intercept/` | TS_INTERCEPT | alignment, benchmark |
| `ts_cv/` | TS_CV | alignment, benchmark |
| `ts_cos/` | TS_COS | alignment, benchmark |
| `ts_norm/` | TS_NORM | alignment, benchmark |
| `ts_rsi/` | TS_RSI | alignment, benchmark |
| `ts_growth/` | TS_GROWTH | alignment |
| `ts_cci/` | TS_CCI | alignment, benchmark |
| `ts_sumpos/` | TS_SUM_POS, TS_POS_RATIO, TS_UP_DOWN_RATIO | alignment, benchmark |
| `stateless_ops/` | SQRT, SIGN_SQRT, FMAX, FMIN | alignment |

基础算子 (MA, TS_SUM, TS_STD, EMA, TS_MIN, TS_MAX, TS_RANK, CORR, etc.) 的测试在 `tests/kernels/test_ops_alignment.py` 中统一覆盖。

组合因子 (T0024-T0046) 的注册和基本推理测试在 `tests/factors/test_inference_engine.py` 中覆盖。
