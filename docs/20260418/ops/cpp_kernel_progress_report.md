# C++ Kernel 对齐进度报告

> **Date:** 2026-04-18  
> **Env:** conda `torch_311` (Python 3.11.14), GCC 11.4.0, pybind11 3.0.3  
> **Machine:** gpu166  
> **Related:** `ts_ops_cpp_kernel_alignment.md`（设计文档）

---

## 1. 总览

P0（无状态）+ P1（单序列滚动）+ P2（双序列/特殊规则）+ P3（低频长尾）**全部完成**，共 33 个 C++ kernel，覆盖 alpha_pool 中 **100%** 的 `ts_ops` 算子调用。

| 优先级 | 范围 | 算子数 | 状态 |
|--------|------|--------|------|
| **P0** | 无状态 (unary + binary) | 12 | **全部完成** |
| **P1** | 单序列滚动 | 12 | **全部完成** |
| **P2** | 双序列/特殊规则 | 4 | **全部完成** |
| **P3** | 低频长尾 | 5 | **全部完成** |

---

## 2. 已实现算子清单

### 2.1 P0 — 无状态算子

| 算子 | C++ 文件 | pybind 接口 | 对齐容差 | 状态 |
|------|----------|-------------|----------|------|
| `Neg(x)` | `unary.hpp` | `fe_ops.neg` | 0 | done |
| `Abs(x)` | `unary.hpp` | `fe_ops.abs_op` | 0 | done |
| `Log(x)` | `unary.hpp` | `fe_ops.log_op` | 1e-6 | done |
| `Sqr(x)` | `unary.hpp` | `fe_ops.sqr` | 0 | done |
| `Inv(x)` | `unary.hpp` | `fe_ops.inv` | 1e-6 | done |
| `Sign(x)` | `unary.hpp` | `fe_ops.sign` | 0 | done |
| `Tanh(x)` | `unary.hpp` | `fe_ops.tanh_op` | 1e-6 | done |
| `SLog1p(x)` | `unary.hpp` | `fe_ops.slog1p` | 1e-6 | done |
| `Add(a,b)` | `binary.hpp` | `fe_ops.add` | 0 | done |
| `Sub(a,b)` | `binary.hpp` | `fe_ops.sub` | 0 | done |
| `Mul(a,b)` | `binary.hpp` | `fe_ops.mul` | 0 | done |
| `Div(a,b)` | `binary.hpp` | `fe_ops.div_op` | 1e-6 | done |

Binary 算子均支持 3 种重载：array+array、array+scalar、scalar+array。

### 2.2 P1 — 单序列滚动算子

| 算子 | C++ 文件 | pybind 接口 | 增量算法 | 对齐容差 | 状态 |
|------|----------|-------------|----------|----------|------|
| `Ma(x,t)` | `rolling_mean.hpp` | `fe_ops.rolling_mean` | ring + 累加 | 1e-5 | done |
| `TsSum(x,t)` | `rolling_sum.hpp` | `fe_ops.rolling_sum` | ring + 累加 | 1e-4 | done |
| `TsStd(x,t)` | `rolling_std.hpp` | `fe_ops.rolling_std` | Welford (double 内部) | 1e-4 | done |
| `TsVari(x,t)` | `rolling_std.hpp` | `fe_ops.rolling_var` | 复用 StdKernel，output²  | 1e-4 | done |
| `Ema(x,t)` | `rolling_ema.hpp` | `fe_ops.ema` | α·x + (1-α)·prev | 1e-5 | done |
| `TsMin(x,t)` | `rolling_minmax.hpp` | `fe_ops.rolling_min` | 单调队列 O(1) 摊销 | 0 | done |
| `TsMax(x,t)` | `rolling_minmax.hpp` | `fe_ops.rolling_max` | 单调队列 O(1) 摊销 | 0 | done |
| `TsRank(x,t)` | `rolling_rank.hpp` | `fe_ops.rolling_rank` | Fenwick Tree + 坐标压缩 | 1e-5 | done |
| `TsZscore(x,t)` | `rolling_zscore.hpp` | `fe_ops.rolling_zscore` | Ma + Std + div 组合 | 1e-4 | done |
| `Delay(x,t)` | `shift.hpp` | `fe_ops.delay` | ring buffer | 0 | done |
| `TsDiff(x,t)` | `shift.hpp` | `fe_ops.ts_diff` | delay + sub | 0 | done |
| `TsPct(x,t)` | `shift.hpp` | `fe_ops.ts_pct` | delay + div | 1e-6 | done |

### 2.3 P2 — 双序列 / 特殊规则算子

| 算子 | C++ 文件 | pybind 接口 | 算法 | 对齐容差 | 状态 |
|------|----------|-------------|------|----------|------|
| `Corr(x,y,t)` | `bivariate.hpp` | `fe_ops.rolling_corr` | online sum accumulators | 1e-4 | done |
| `Autocorr(x,t,n)` | `bivariate.hpp` | `fe_ops.autocorr` | 精确复现 Python double-rolling | 1e-4 | done |
| `TsMinMaxDiff(x,t)` | `rolling_extremal.hpp` | `fe_ops.ts_minmax_diff` | 双单调队列, min_periods=1 | 1e-6 | done |
| `TsSkew(x,t)` | `rolling_skew.hpp` | `fe_ops.rolling_skew` | Fisher-Pearson centered moments | 1e-3 | done |

### 2.4 P3 — 低频长尾算子

| 算子 | C++ 文件 | pybind 接口 | 算法 | 对齐容差 | 状态 |
|------|----------|-------------|------|----------|------|
| `TsMed(x,t)` | `rolling_median.hpp` | `fe_ops.rolling_median` | sorted buffer + binary search | 1e-6 | done |
| `TsMad(x,t)` | `rolling_median.hpp` | `fe_ops.rolling_mad` | two-pass rolling median | 1e-5 | done |
| `TsWMA(x,t)` | `rolling_wma.hpp` | `fe_ops.rolling_wma` | ring + double 加权累加 | 1e-5 | done |
| `TsMaxDiff(x,t)` | `rolling_extremal.hpp` | `fe_ops.ts_max_diff` | 单调队列, min_periods=1 | 1e-6 | done |
| `TsMinDiff(x,t)` | `rolling_extremal.hpp` | `fe_ops.ts_min_diff` | 单调队列, min_periods=1 | 1e-6 | done |

**P3 实现要点：**

- **TsMed** — sorted buffer 维护有序窗口，O(t) per step（insert/erase shift），min_periods=t。
- **TsMad** — two-pass：先 rolling median（`min_periods=max(2, t/2)`），再对 `|x - med|` 做 rolling median。精确复现 Python `TsMad` 的 `mp = max(2, t//2)` 语义。
- **TsWMA** — weights = `[1, 2, ..., t]` normalized（double 精度），min_periods=t。O(t) per step。
- **TsMaxDiff / TsMinDiff** — `x - rolling_max(x)` / `x - rolling_min(x)`，min_periods=1，与 TsMinMaxDiff 共用 `rolling_extremal.hpp`。

---

## 3. 对齐测试结果

| 测试文件 | 测试数 | 结果 |
|----------|--------|------|
| `test_ops_alignment.py` (P0+P1) | 293 | **293/293 passed** |
| `test_p2_alignment.py` (P2) | 96 | **96/96 passed** |
| `test_p3_alignment.py` (P3) | 113 | **113/113 passed** |
| **合计** | **502** | **502/502 passed, 0 failed** |

测试覆盖维度：

| 维度 | 覆盖情况 |
|------|---------|
| 多 seed 随机 (42, 77, 123, 256, 999) | ✅ |
| 常数 / 全零 / 全正 / 全负 | ✅ |
| 极值 (±1e30, ±1e-38) | ✅ |
| 单元素数组 | ✅ |
| window=1 / window=n / window>n | ✅ |
| 大窗口 window=1440, 2880 | ✅ |
| 单调递增 / 递减 | ✅ |
| Corr(x,x)=1, Corr(x,-x)=-1 | ✅ |
| Corr(const, x) → ±inf | ✅ |
| Autocorr edge (t<2, n<1 → NaN) | ✅ |
| TsMinMaxDiff/TsMaxDiff/TsMinDiff window=1 → 0 | ✅ |
| TsSkew window<3 → NaN / const → 0 | ✅ |
| TsMad small window (t=2, mp=max(2,1)=2) | ✅ |

> **注意：** 输入端不含 NaN（数据流阶段已清洗），测试仅验证计算过程中产生的 NaN（warmup、零方差等）对齐。

---

## 4. Benchmark 结果

> 测试配置：N_REPEAT=20, N_WARMUP=3, median time  
> 数组大小：1,440 / 4,320 / 10,000 / 100,000  
> 滚动窗口：30 / 120 / 480

### 4.1 P0 Unary — 代表性结果（n=1,440 为实际生产规模）

| 算子 | n=1,440 py | n=1,440 cpp | speedup | n=10K speedup | n=100K speedup |
|------|-----------|-------------|---------|---------------|----------------|
| Neg | 77 µs | 1.6 µs | **49x** | 18x | 3.3x |
| Abs | 73 µs | 1.6 µs | **46x** | 17x | 3.0x |
| Log | 454 µs | 6.5 µs | **70x** | 7.6x | 3.3x |
| Sqr | 110 µs | 1.6 µs | **69x** | 26x | 4.4x |
| Inv | 152 µs | 2.6 µs | **58x** | 13x | 1.9x |
| Sign | 64 µs | 3.3 µs | **19x** | 1.6x | 0.3x |
| Tanh | 72 µs | 27 µs | **2.7x** | 0.5x | 0.3x |
| SLog1p | 87 µs | 18 µs | **4.9x** | 1.5x | 2.1x |

### 4.2 P0 Binary — 代表性结果

| 算子 | n=1,440 (arr+arr) speedup | n=1,440 (arr+scl) speedup | n=100K speedup |
|------|--------------------------|--------------------------|----------------|
| Add | **57x** | **50x** | 2.8x |
| Sub | **57x** | **43x** | 4.6x |
| Mul | **66x** | **51x** | 4.6x |
| Div | **59x** | **36x** | 2.2x |

### 4.3 P1 Rolling — n=1,440（生产规模，window=30）

| 算子 | Python | C++ | Speedup |
|------|--------|-----|---------|
| Ma | 177 µs | 15 µs | **12x** |
| TsSum | 165 µs | 15 µs | **11x** |
| TsStd | 374 µs | 15 µs | **25x** |
| Ema | 154 µs | 5.2 µs | **30x** |
| TsMin | 172 µs | 30 µs | **5.8x** |
| TsMax | 172 µs | 30 µs | **5.8x** |
| TsZscore | 1.07 ms | 16 µs | **65x** |
| TsRank | 655 µs | 240 µs | **2.7x** |
| Delay | 102 µs | 1.6 µs | **62x** |
| TsDiff | 150 µs | 2.1 µs | **72x** |
| TsPct | 235 µs | 2.7 µs | **87x** |

### 4.4 P2 Rolling — n=1,440（生产规模，window=30）

| 算子 | Python | C++ | Speedup |
|------|--------|-----|---------|
| Corr(t=30) | 527 µs | 11 µs | **47x** |
| Autocorr(t=30,n=1) | 1.11 ms | 39 µs | **29x** |
| TsMinMaxDiff(t=30) | 310 µs | 60 µs | **5.1x** |
| TsSkew(t=30) | 194 µs | 48 µs | **4.0x** |

### 4.5 P3 Rolling — n=1,440（生产规模，window=30）

| 算子 | Python | C++ | Speedup |
|------|--------|-----|---------|
| TsMed(t=30) | 661 µs | 112 µs | **5.9x** |
| TsMad(t=30) | 1.25 ms | 244 µs | **5.1x** |
| TsWMA(t=30) | 1.90 ms | 212 µs | **9.0x** |
| TsMaxDiff(t=30) | 222 µs | 31 µs | **7.1x** |
| TsMinDiff(t=30) | 223 µs | 31 µs | **7.2x** |

### 4.6 P3 Speedup 随规模变化（window=30）

| 算子 | n=1,440 | n=4,320 | n=10K | n=100K |
|------|---------|---------|-------|--------|
| TsMed | **5.9x** | 4.2x | 4.0x | 4.1x |
| TsMad | **5.1x** | 4.2x | 4.0x | 4.0x |
| TsWMA | **9.0x** | 8.2x | 7.7x | 7.0x |
| TsMaxDiff | **7.1x** | 2.4x | 1.4x | 1.0x |
| TsMinDiff | **7.2x** | 2.4x | 1.4x | 1.0x |

**P3 关键观察：**

1. **TsWMA** 在所有规模下保持 **7-9x** 加速，因为 Python 用 `rolling.apply(lambda)` 逐窗调用，开销巨大。C++ 用 ring buffer + double 累加。大窗口 (t≥480) 时 C++ 也是 O(n·t)，性能会下降。
2. **TsMed / TsMad** 在所有规模下稳定 **4-6x** 加速。C++ O(t) sorted buffer vs pandas Cython rolling median。
3. **TsMaxDiff / TsMinDiff** 在生产规模 **7x+** 加速，大数组时趋近 1x（pandas 的 Cython rolling max/min 已很高效）。

### 4.7 TsRank 优化效果

TsRank 经 Fenwick Tree + 坐标压缩优化后，从 O(t·log t) 降至 O(log V)：

| n | window | Python | C++ | Speedup |
|---|--------|--------|-----|---------|
| 1,440 | 30 | 655 µs | 240 µs | **2.7x** |
| 1,440 | 120 | 790 µs | 228 µs | **3.5x** |
| 10,000 | 120 | 4.4 ms | 2.0 ms | **2.2x** |
| 100,000 | 120 | 42 ms | 25 ms | **1.7x** |

### 4.8 性能热力图（Speedup 倍数, window=30）

```
                n=1,440   n=4,320   n=10K    n=100K
               ───────────────────────────────────────
 Neg             49x       30x       18x      3.3x
 Log             70x       20x       7.6x     3.3x
 Add(aa)         57x       33x       18x      2.8x
 Ma              12x       4.7x      2.4x     1.1x
 TsStd           25x       9.5x      5.0x     2.0x
 Ema             30x       13x       7.5x     3.1x
 TsZscore        65x       25x       12x      3.7x
 TsRank          2.7x      -         1.7x     1.3x
 Delay           62x       39x       24x      3.9x
 TsPct           87x       41x       21x      3.0x
 Corr            47x       23x       23x      19x
 Autocorr        29x       11x       6.5x     2.2x
 MinMaxDiff      5.1x      2.0x      1.3x     1.0x
 TsSkew          4.0x      1.8x      1.2x     1.2x
 TsMed           5.9x      4.2x      4.0x     4.1x
 TsMad           5.1x      4.2x      4.0x     4.0x
 TsWMA           9.0x      8.2x      7.7x     7.0x
 TsMaxDiff       7.1x      2.4x      1.4x     1.0x
 TsMinDiff       7.2x      2.4x      1.4x     1.0x
```

---

## 5. 代码结构

```
native/
  include/fe/ops/
    spec.hpp              # FeFloat, kEps, fe_is_nan
    unary.hpp             # 8 unary kernels (element-wise + array)
    binary.hpp            # 4 binary kernels × 3 overloads (aa/as/sa)
    rolling_mean.hpp      # RollingMeanKernel
    rolling_sum.hpp       # RollingSumKernel
    rolling_std.hpp       # RollingStdKernel + rolling_var
    rolling_ema.hpp       # EmaKernel
    rolling_minmax.hpp    # RollingMinKernel + RollingMaxKernel (单调队列)
    rolling_rank.hpp      # Fenwick Tree + 坐标压缩
    rolling_zscore.hpp    # Ma + Std + div 组合
    shift.hpp             # delay / ts_diff / ts_pct
    bivariate.hpp         # Corr (online) + Autocorr (double-rolling)
    rolling_extremal.hpp  # TsMinMaxDiff + TsMaxDiff + TsMinDiff (min_periods=1)
    rolling_skew.hpp      # TsSkew (Fisher-Pearson centered moments)
    rolling_median.hpp    # TsMed + TsMad (sorted buffer)
    rolling_wma.hpp       # TsWMA (linear weighted average)
  pybind/
    fe_ops_bind.cpp       # 统一 pybind11 绑定 (33 个接口)
  build.sh                # 一键编译脚本
  CMakeLists.txt          # CMake 配置

tests/kernel/
  test_ops_alignment.py   # P0+P1 对齐测试 (293 cases)
  test_p2_alignment.py    # P2 对齐测试 (96 cases)
  test_p3_alignment.py    # P3 对齐测试 (113 cases)
  reference/
    ts_ops.py             # Python 算子真相源副本
  benchmark/
    bench_ops.py          # P0+P1 性能对比
    bench_p2_ops.py       # P2 性能对比
    bench_p3_ops.py       # P3 性能对比
```

---

## 6. 构建 & 测试流程

```bash
# 1. 激活环境
conda activate torch_311

# 2. 编译
cd FactorEngine/native && bash build.sh

# 3. 对齐测试 (P0+P1)
cd FactorEngine && python tests/kernel/test_ops_alignment.py

# 4. 对齐测试 (P2)
cd FactorEngine && python tests/kernel/test_p2_alignment.py

# 5. 对齐测试 (P3)
cd FactorEngine && python tests/kernel/test_p3_alignment.py

# 6. 性能测试
cd FactorEngine && python tests/kernel/benchmark/bench_ops.py
cd FactorEngine && python tests/kernel/benchmark/bench_p2_ops.py
cd FactorEngine && python tests/kernel/benchmark/bench_p3_ops.py
```

---

## 7. 下一步

| 优先级 | 任务 | 说明 |
|--------|------|------|
| 优化 | TsSkew 增量算法 | 当前 O(n·t)，可改 incremental 3rd moment |
| 优化 | TsWMA 增量算法 | 当前 O(n·t)，可改增量 weighted sum (O(n)) |
| 优化 | `TsMin`/`TsMax` SIMD | 大数组下略慢于 pandas |
| 优化 | `Sign`/`Tanh` SIMD | 大数组分支型 kernel 需要向量化 |
| 集成 | FactorKernel DAG 执行器 | 将 33 个 kernel 串联为因子 DAG |
| 集成 | Scheduler 调用 C++ kernel | 替换 Python ts_ops 调用为 fe_ops |
