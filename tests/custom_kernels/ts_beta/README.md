# TS_BETA — Rolling OLS Beta (线性回归斜率)

## 数学定义

给定两个时间序列 `x` (自变量) 和 `y` (因变量)，窗口大小为 `t`，
Rolling Beta 定义为窗口内 `y` 对 `x` 的 OLS 线性回归斜率：

```
beta = Cov(x, y) / Var(x)
```

展开为原始统计量：

```
         n * sum(xi * yi) - sum(xi) * sum(yi)
beta = ─────────────────────────────────────────
            n * sum(xi^2) - sum(xi)^2
```

其中 `n = t`（窗口大小），所有求和均在最近 `t` 个样本上进行。

### 特殊情况

| 条件 | 输出 |
|------|------|
| `window < 2` | 图构建阶段抛出 `invalid_argument` |
| 窗口内样本不足 (`count < t`) | `NaN` |
| 窗口内含 `NaN` | `NaN` |
| `Var(x) <= 0`（x 为常数） | `NaN` |
| `Beta(x, x)` | `1.0`（因为 `Cov(x,x) / Var(x) = 1`） |
| `y = a * x + b` | 精确等于 `a` |

### 与 pandas 对齐

```python
# TS_BETA 等价于以下 pandas 运算：
beta = series_y.rolling(t, min_periods=t).cov(series_x) \
     / series_x.rolling(t, min_periods=t).var(ddof=1)
```

---

## 代码实现

### 1. 批量内核 (`bivariate.hpp :: rolling_beta`)

适用于离线回测场景，一次性处理整个数组。

```
入参:  y[0..n-1], x[0..n-1], window=t
输出:  out[0..n-1]
```

内部维护一个大小为 `t` 的 **ring buffer**，以及 4 个增量统计量：

| 变量 | 含义 |
|------|------|
| `sx` | sum(x_i) |
| `sy` | sum(y_i) |
| `sxx` | sum(x_i ^ 2) |
| `sxy` | sum(x_i * y_i) |

每步 `i` 的操作：

```
1. 若窗口已满 (count >= t):
   - 从 ring buffer 取出即将滑出的旧值 (ox, oy)
   - sx -= ox,  sy -= oy,  sxx -= ox^2,  sxy -= ox * oy

2. 写入新值到 ring buffer

3. 累加新值:
   - sx += xv,  sy += yv,  sxx += xv^2,  sxy += xv * yv

4. 计算输出:
   - var_x  = n * sxx - sx^2
   - cov_xy = n * sxy - sx * sy
   - out[i] = cov_xy / var_x
```

### 2. 流式推理内核 (`kernels.hpp :: BetaPush`)

适用于实时流式推理场景（FactorGraph 的 `push_bar` 路径），每次只接收一对新值。

```cpp
class BetaPush {
    int    t_;          // 窗口大小
    vector rx_, ry_;    // ring buffer (大小 t_)
    int    pos_;        // 写入指针
    int    count_;      // 已填充数量 (<= t_)
    int    nan_count_;  // 窗口内 NaN 计数
    double sx_, sy_;    // sum(x), sum(y)
    double sxx_, sxy_;  // sum(x^2), sum(x*y)
};
```

**`push(x, y)` 流程:**

```
1. 判断新值是否含 NaN

2. 若窗口满 -> 弹出最旧一对值:
   - 若旧值含 NaN  ->  nan_count_--
   - 否则  ->  sx_ -= ox,  sy_ -= oy,  sxx_ -= ox^2,  sxy_ -= ox * oy

3. 写入 ring buffer:  rx_[pos % t] = x,  ry_[pos % t] = y

4. 若新值含 NaN  ->  nan_count_++
   否则  ->  sx_ += x,  sy_ += y,  sxx_ += x^2,  sxy_ += x * y

5. pos_++,  count_ = min(count_ + 1, t_)
```

**`output()` 流程:**

```
1. 若 count_ < t_ 或 nan_count_ > 0  ->  返回 NaN

2. var_x = t * sxx_ - sx_^2
   若 var_x <= 0  ->  返回 NaN (x 为常数, 回归无意义)

3. cov_xy = t * sxy_ - sx_ * sy_
   返回 cov_xy / var_x
```

### 3. FactorGraph 集成

```python
import fe_runtime as rt

g = rt.FactorGraph()
x = g.add_input("close")
y = g.add_input("volume")

# TS_BETA(x, y, 60) = Cov(x,y) / Var(x), 即 y 对 x 的敏感度
g.add_bivariate(rt.Op.TS_BETA, x, y, 60)
g.compile()

# 逐步推送
g.push_bar(close=100.0, volume=5000.0)
beta_value = g.raw_output()
```

> **注意:** `add_bivariate(TS_BETA, src_a, src_b, w)` 中 **src_a 是自变量 x, src_b 是因变量 y**,
> 结果为 `Cov(a, b) / Var(a)`。

---

## 时间复杂度

| 操作 | 时间复杂度 | 空间复杂度 |
|------|-----------|-----------|
| `push(x, y)` | **O(1)** | O(t) |
| `output()` | **O(1)** | -- |
| `rolling_beta` (batch, n 个点) | **O(n)** | O(t) |

每次 `push` 仅做常数次加减乘操作（弹出旧值 + 累加新值），与窗口大小 `t` 无关。
这得益于增量维护 4 个统计量 `(sx, sy, sxx, sxy)` 而非每步重新遍历窗口。

### 实测性能

```
BATCH:  fe_ops.rolling_beta  vs  pandas Cov/Var
  n=1,440   w=30    pandas=698 us    C++=16 us     加速 44x
  n=10,000  w=120   pandas=1.49 ms   C++=133 us    加速 11x

PUSH:  FactorGraph TS_BETA streaming
  w=30    avg=1524 ns/push    ~656K push/s
  w=120   avg=1527 ns/push    ~655K push/s
  w=480   avg=1458 ns/push    ~686K push/s
```

push 吞吐量约 **65 万次/秒**, 且不随窗口增大而衰减 (O(1) 特性)。

---

## 与 Cov / Corr 的关系

三者共享相同的增量统计量 `(sx, sy, sxx, sxy)`, 区别仅在 `output()` 的分母:

| 算子 | 公式 | 分母 |
|------|------|------|
| `TS_COV` | `Cov(x, y)` | `n * (n - 1)` |
| `CORR` | `Cov(x, y) / (std_x * std_y)` | 标准差之积 |
| **`TS_BETA`** | **`Cov(x, y) / Var(x)`** | **x 的方差** |

---

## 在 crypto 因子中的应用

| 因子 | 描述 | 公式 |
|------|------|------|
| T0009 | 成交量-收益率敏感度 | `TsZscore(TsBeta(SLog1p(vol), ret, 60), 120)` |
| T0010 | 买卖失衡对价格的冲击 | `Neg(TsRank(TsBeta(imbalance, pct_close, 30), 120))` |

典型用法: 衡量某个驱动因素 (如成交量、买卖不平衡度) 对价格变动的**线性敏感度**。
Beta 值越大, 说明因变量对自变量变化的响应越强。

---

## 测试文件

| 文件 | 内容 |
|------|------|
| `test_ts_beta_alignment.py` | 批量 C++ vs pandas 对齐 + NaN 处理 + 边界条件 + push vs batch |
| `test_ts_beta_factor_graph.py` | FactorGraph 单因子对齐 |
| `test_ts_beta_trade_factor.py` | T0009 / T0010 端到端因子对齐 |
| `bench_ts_beta.py` | 批量 + 流式推理性能基准 |
