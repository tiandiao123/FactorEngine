# ts_ops C++ Kernel 对齐开发文档

> **Date:** 2026-04-18  
> **Scope:** 将 `factorlib/ops/ts_ops.py` 中的 Python 算子逐个翻译为 C++ 增量 kernel，通过 pybind11 编译为 `.so`，在同一 Python 进程中与 pandas 原版做数据级对齐验证。  
> **Related:**  
> - `FactorEngine/docs/20260413/cpp_kernel_operator_parity_dev.md`  
> - `skydiscover/skygen/factorgen/cryptokbar/factorlib/ops/ts_ops.py`（算子单一真相源）

---

## 1. 目标

1. **逐算子翻译**：将 `ts_ops.py` 中的时序/元素/双序列算子翻译为 C++ 增量 kernel
2. **pybind11 暴露**：编译为 `fe_ops.so`，Python 可直接 `import fe_ops`
3. **同进程对齐**：同一脚本里同时跑 Python 版和 C++ 版，用随机数据做 tolerance 对比
4. **按需增长**：先覆盖 alpha_pool 高频算子；后续遇到新算子再增量添加

不做 AST→DAG 自动翻译。因子表达式由人工手动翻译为 C++ kernel 调用链。

---

## 2. 算子使用频率（来自 310 个 alpha_pool 因子的统计）

### 2.1 ts_ops（时序算子）

| 排名 | 算子 | 使用次数 | 优先级 | C++ 状态 |
|------|------|---------|--------|---------|
| 1 | `TsRank` | 949 | P1 | ✅ done `rolling_rank.hpp` (Fenwick Tree) |
| 2 | `Div` | 891 | P0 | ✅ done `binary.hpp` |
| 3 | `Mul` | 736 | P0 | ✅ done `binary.hpp` |
| 4 | `Sub` | 720 | P0 | ✅ done `binary.hpp` |
| 5 | `Neg` | 656 | P0 | ✅ done `unary.hpp` |
| 6 | `clean_factor` | 633 | -- | 不需要 kernel |
| 7 | `Ma` | 632 | P0 | ✅ done `rolling_mean.hpp` |
| 8 | `TsStd` | 549 | P1 | ✅ done `rolling_std.hpp` |
| 9 | `TsDiff` | 544 | P1 | ✅ done `shift.hpp` |
| 10 | `Log` | 407 | P0 | ✅ done `unary.hpp` |
| 11 | `SLog1p` | 400 | P0 | ✅ done `unary.hpp` |
| 12 | `Corr` | 393 | P2 | ✅ done `bivariate.hpp` |
| 13 | `TsZscore` | 351 | P1 | ✅ done `rolling_zscore.hpp` |
| 14 | `Abs` | 345 | P0 | ✅ done `unary.hpp` |
| 15 | `Ema` | 339 | P1 | ✅ done `rolling_ema.hpp` |
| 16 | `Add` | 199 | P0 | ✅ done `binary.hpp` |
| 17 | `TsSum` | 164 | P1 | ✅ done `rolling_sum.hpp` |
| 18 | `TsMax` | 159 | P1 | ✅ done `rolling_minmax.hpp` |
| 19 | `TsMin` | 158 | P1 | ✅ done `rolling_minmax.hpp` |
| 20 | `Tanh` | 143 | P0 | ✅ done `unary.hpp` |
| 21 | `Sign` | 50 | P0 | ✅ done `unary.hpp` |
| 22 | `Delay` | 43 | P1 | ✅ done `shift.hpp` |
| 23 | `Autocorr` | 40 | P2 | ✅ done `bivariate.hpp` |
| 24 | `Inv` | 29 | P0 | ✅ done `unary.hpp` |
| 25 | `TsMinMaxDiff` | 23 | P2 | ✅ done `rolling_extremal.hpp` |
| 26 | `TsSkew` | 21 | P2 | ✅ done `rolling_skew.hpp` |
| 27 | `Sqr` | 5 | P0 | ✅ done `unary.hpp` |
| 28 | `TsMad` | 5 | P3 | ✅ done `rolling_median.hpp` |
| 29 | `TsMed` | 4 | P3 | ✅ done `rolling_median.hpp` |
| 30 | `TsWMA` | 3 | P3 | ✅ done `rolling_wma.hpp` |
| 31 | `TsPct` | 2 | P1 | ✅ done `shift.hpp` |
| 32 | `TsMaxDiff` | 1 | P3 | ✅ done `rolling_extremal.hpp` |

### 2.2 agg_ops（整窗聚合算子，Phase 2，本期不涉及）

| 算子 | 使用次数 |
|------|---------|
| `close_position_in_range` | 47 |
| `amihud_illiq` | 37 |
| `ret_autocorr_lag1` | 20 |
| `rvol_upto` | 18 |
| `body_to_shadow_ratio` | 12 |
| `volume_weighted_price_std` | 11 |
| `price_path_convexity` | 10 |
| 其它 | <=3 |

---

## 3. 优先级定义

| 级别 | 范围 | 说明 |
|------|------|------|
| **P0** | 无状态算子 | 无 ring buffer，O(1) 计算，最简单 |
| **P1** | 单序列滚动 | 需要 ring buffer，因子池最高频 |
| **P2** | 双序列滚动 / 特殊 min_periods | `Corr`, `Autocorr`, `TsMinMaxDiff`, `TsSkew` |
| **P3** | 低频长尾 | `TsMed`, `TsMad`, `TsWMA`, `TsMaxDiff`，遇到再做 |

---

## 4. 代码架构

### 4.1 目录布局

```
FactorEngine/
  native/
    CMakeLists.txt                    # 现有，需扩展 pybind11 target
    include/fe/ops/
      spec.hpp                        # done: EPS, FeFloat, fe_close, fe_is_nan
      rolling_mean.hpp                # done: Ma -> RollingMeanKernel
      unary.hpp                       # P0: Neg, Abs, Log, Sqr, Inv, Sign, Tanh, SLog1p
      binary.hpp                      # P0: Add, Sub, Mul, Div
      shift.hpp                       # P1: Delay, TsDiff, TsPct
      rolling_sum.hpp                 # P1: TsSum
      rolling_std.hpp                 # P1: TsStd, TsVari
      rolling_ema.hpp                 # P1: Ema
      rolling_minmax.hpp              # P1: TsMin, TsMax
      rolling_zscore.hpp              # P1: TsZscore (组合: Ma + TsStd + 除法)
      rolling_rank.hpp                # P1: TsRank
      bivariate.hpp                   # P2: Corr, Cov, Autocorr
      rolling_extremal.hpp            # P2: TsMinMaxDiff, TsMaxDiff, TsMinDiff
      rolling_skew.hpp                # P2: TsSkew, TsKurt
      rolling_misc.hpp                # P3: TsMed, TsMad, TsWMA
    pybind/
      fe_ops_bind.cpp                 # pybind11 绑定: 所有 kernel -> Python 函数
    tests/
      fixtures/                       # .fegolden 文件 (保留，CI 用)
      run_ma_golden.cpp               # done

  tests/
    kernel/
      export_ts_op_golden.py          # done: 生成 .fegolden fixture
      test_ops_alignment.py           # 新增: Python 端 pybind 对齐测试
      reference/
        ts_ops.py                     # 从 factorlib 复制来的 Python 算子 (单一真相源副本)
```

### 4.2 C++ Kernel 统一接口

**Stateless（P0 无状态算子）：纯函数**

```cpp
// unary.hpp
namespace fe::ops {
inline FeFloat neg(FeFloat x) { return -x; }
inline FeFloat abs_op(FeFloat x) { return std::abs(x); }
inline FeFloat log_op(FeFloat x) {
    return x <= 0.0f ? std::numeric_limits<FeFloat>::quiet_NaN()
                     : static_cast<FeFloat>(std::log(static_cast<double>(x)));
}
inline FeFloat sqr(FeFloat x) { return x * x; }
inline FeFloat inv(FeFloat x) { return 1.0f / (x + kEps); }
inline FeFloat sign(FeFloat x) {
    return fe_is_nan(x) ? std::numeric_limits<FeFloat>::quiet_NaN()
                        : (x > 0.0f ? 1.0f : (x < 0.0f ? -1.0f : 0.0f));
}
inline FeFloat tanh_op(FeFloat x) {
    return static_cast<FeFloat>(std::tanh(static_cast<double>(x)));
}
inline FeFloat slog1p(FeFloat x) {
    double d = static_cast<double>(x);
    return static_cast<FeFloat>(std::copysign(std::log1p(std::abs(d)), d));
}
}

// binary.hpp
namespace fe::ops {
inline FeFloat add(FeFloat a, FeFloat b) { return a + b; }
inline FeFloat sub(FeFloat a, FeFloat b) { return a - b; }
inline FeFloat mul(FeFloat a, FeFloat b) { return a * b; }
inline FeFloat div_op(FeFloat a, FeFloat b) { return a / (b + kEps); }
}
```

**Stateful（P1/P2 有状态算子）：push/output/ready/reset**

```cpp
// 已有 RollingMeanKernel 为模板，所有滚动算子遵循同一接口:
class SomeRollingKernel {
public:
    explicit SomeRollingKernel(uint32_t window);
    void reset();
    void push(FeFloat x);              // 单输入
    // void push(FeFloat x, FeFloat y); // Corr 等双输入
    [[nodiscard]] bool ready() const;
    [[nodiscard]] FeFloat output() const;
};
```

### 4.3 全局常量（必须与 Python 一致）

当前 `spec.hpp`:

```cpp
inline constexpr float kEps = 1e-8f;    // 对齐 ts_ops.py EPS = 1e-8
using FeFloat = float;                   // 对齐 ts_ops.py DTYPE = np.float32
```

| 规则 | Python | C++ |
|------|--------|-----|
| dtype | `np.float32` | `FeFloat = float` |
| EPS | `1e-8` | `kEps = 1e-8f` |
| rolling center | `False` | ring buffer 天然只看历史 |
| 默认 min_periods | `t`（多数算子） | `ready()`: `n_ >= t_` |
| TsStd ddof | `0`（population std） | Welford 用 `/ n` 而非 `/ (n-1)` |
| 内部精度 | TsStd 等在 float64 上算再 cast | C++ 内部 double 累加，输出 cast float |

---

## 5. pybind11 对齐测试方案

### 5.1 编译流程

CMakeLists.txt 新增 pybind11 target:

```cmake
find_package(pybind11 REQUIRED)
pybind11_add_module(fe_ops pybind/fe_ops_bind.cpp)
target_link_libraries(fe_ops PRIVATE fe_ops_headers)
```

编译:

```bash
cd FactorEngine/native
mkdir -p build && cd build
cmake .. -Dpybind11_DIR=$(python3 -m pybind11 --cmakedir)
make -j
# 生成 fe_ops.cpython-3xx-xxx.so
```

### 5.2 pybind 绑定示例

```cpp
// pybind/fe_ops_bind.cpp
#include <pybind11/pybind11.h>
#include <pybind11/numpy.h>
#include "fe/ops/spec.hpp"
#include "fe/ops/rolling_mean.hpp"
#include "fe/ops/unary.hpp"
#include "fe/ops/binary.hpp"

namespace py = pybind11;
using fe::ops::FeFloat;

// --- 滚动算子: 逐 bar push，收集完整输出数组 ---

py::array_t<float> py_rolling_mean(py::array_t<float> x, int window) {
    auto buf = x.request();
    auto* ptr = static_cast<float*>(buf.ptr);
    int n = static_cast<int>(buf.size);

    fe::ops::RollingMeanKernel kernel(window);
    py::array_t<float> result(n);
    auto* out = static_cast<float*>(result.request().ptr);
    for (int i = 0; i < n; ++i) {
        kernel.push(ptr[i]);
        out[i] = kernel.output();
    }
    return result;
}

// --- 无状态算子: 逐元素 apply ---

py::array_t<float> py_neg(py::array_t<float> x) {
    auto buf = x.request();
    auto* ptr = static_cast<float*>(buf.ptr);
    int n = static_cast<int>(buf.size);
    py::array_t<float> result(n);
    auto* out = static_cast<float*>(result.request().ptr);
    for (int i = 0; i < n; ++i)
        out[i] = fe::ops::neg(ptr[i]);
    return result;
}

// ... 每个算子同理

PYBIND11_MODULE(fe_ops, m) {
    m.doc() = "FactorEngine C++ operator kernels";

    // P0 unary
    m.def("neg", &py_neg);
    // m.def("abs_op", &py_abs_op);
    // ...

    // P1 rolling
    m.def("rolling_mean", &py_rolling_mean, py::arg("x"), py::arg("window"));
    // m.def("rolling_std", &py_rolling_std, ...);
    // ...
}
```

### 5.3 Python 对齐测试脚本

```python
# tests/kernel/test_ops_alignment.py
import sys
from pathlib import Path
import numpy as np
import pandas as pd

# Load Python reference ops
FACTORLIB = Path(__file__).resolve().parent / "reference"
sys.path.insert(0, str(FACTORLIB))
from ts_ops import Ma, TsStd, Ema, TsRank, Neg, Abs, Sub, Mul, Div  # ...

# Load C++ ops
import fe_ops


def _random_series(n=5000, seed=42, nan_ratio=0.02):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n).astype(np.float32)
    if nan_ratio > 0:
        nan_idx = rng.choice(n, size=int(n * nan_ratio), replace=False)
        x[nan_idx] = np.nan
    return x


def assert_aligned(py_out, cpp_out, atol, op_name):
    py_arr = np.asarray(py_out, dtype=np.float32)
    cpp_arr = np.asarray(cpp_out, dtype=np.float32)
    assert py_arr.shape == cpp_arr.shape, f"{op_name}: shape mismatch"

    py_nan = np.isnan(py_arr)
    cpp_nan = np.isnan(cpp_arr)
    nan_mismatch = (py_nan != cpp_nan).sum()
    assert nan_mismatch == 0, f"{op_name}: {nan_mismatch} NaN position mismatches"

    valid = ~py_nan
    if valid.sum() == 0:
        return
    max_diff = np.max(np.abs(py_arr[valid] - cpp_arr[valid]))
    assert max_diff <= atol, f"{op_name}: max_diff={max_diff:.2e} > atol={atol:.0e}"
    print(f"  OK {op_name:20s} max_diff={max_diff:.2e}  (n_valid={valid.sum()})")


def test_rolling_mean():
    for window in [5, 30, 120, 480]:
        x = _random_series()
        py_out = Ma(pd.Series(x), window).values
        cpp_out = fe_ops.rolling_mean(x, window)
        assert_aligned(py_out, cpp_out, atol=1e-5, op_name=f"Ma(t={window})")


def test_neg():
    x = _random_series()
    py_out = Neg(pd.Series(x)).values
    cpp_out = fe_ops.neg(x)
    assert_aligned(py_out, cpp_out, atol=0, op_name="Neg")


# ... 每实现一个算子，添加一个 test_xxx()

if __name__ == "__main__":
    print("=== ts_ops alignment tests ===")
    test_rolling_mean()
    test_neg()
    print("\nAll tests passed.")
```

---

## 6. 各算子 C++ 实现要点

### 6.1 P0 — 无状态算子

| 算子 | Python 语义 | C++ 要点 | 容差 |
|------|------------|---------|------|
| `Neg(x)` | `-x` | trivial | 0 |
| `Abs(x)` | `abs(x)` | `std::abs` | 0 |
| `Log(x)` | `log(x)`，`x<=0 -> NaN` | 检查 `x <= 0` | `1e-6` |
| `Sqr(x)` | `x * x` | trivial | 0 |
| `Inv(x)` | `1 / (x + EPS)` | 用 `kEps` | `1e-6` |
| `Sign(x)` | `np.sign(x)` | NaN -> NaN, 0 -> 0 | 0 |
| `Tanh(x)` | `np.tanh(x)` | float64 算再 cast | `1e-6` |
| `SLog1p(x)` | `sign(x) * log1p(abs(x))` | `std::copysign` + `std::log1p` | `1e-6` |
| `Add(a,b)` | `a + b` | NaN 传播 | 0 |
| `Sub(a,b)` | `a - b` | NaN 传播 | 0 |
| `Mul(a,b)` | `a * b` | NaN 传播 | 0 |
| `Div(a,b)` | `a / (b + EPS)` | 用 `kEps` | `1e-6` |

### 6.2 P1 — 单序列滚动

| 算子 | Python 语义 | C++ kernel 类 | 增量算法 | 容差 |
|------|------------|-------------|---------|------|
| `Ma(x,t)` | `rolling(t).mean()` | `RollingMeanKernel` (done) | ring + 累加 | `1e-5` |
| `TsSum(x,t)` | `rolling(t).sum()` | `RollingSumKernel` | ring + 累加 | `1e-5` |
| `TsStd(x,t,ddof=0)` | `rolling(t).std(ddof=0)` float64 内部 | `RollingStdKernel` | Welford，内部 double | `1e-4` |
| `TsVari(x,t)` | `rolling(t).var(ddof=0)` | 复用 StdKernel 或独立 | 同 TsStd | `1e-4` |
| `Ema(x,t)` | `ewm(span=t, adjust=False).mean()` | `EmaKernel` | `a*x + (1-a)*prev` | `1e-5` |
| `TsMin(x,t)` | `rolling(t).min()` | `RollingMinKernel` | 单调递增队列 O(1) 摊销 | `1e-6` |
| `TsMax(x,t)` | `rolling(t).max()` | `RollingMaxKernel` | 单调递减队列 O(1) 摊销 | `1e-6` |
| `TsRank(x,t)` | 窗口内 pct rank | `RollingRankKernel` | 有序数组或逐窗排序 | `1e-5` |
| `TsZscore(x,t)` | `(x - Ma) / (TsStd + EPS)` | 组合 Ma + Std + div | 或独立 kernel | `1e-4` |
| `TsDiff(x,t)` | `x - x.shift(t)` | `DelayKernel` + sub | ring 取历史值 | 0 |
| `TsPct(x,t)` | `x / (x.shift(t) + EPS) - 1` | `DelayKernel` + div | 同上 | `1e-6` |
| `Delay(x,t)` | `x.shift(t)` | `DelayKernel` | ring 存 t 步前的值 | 0 |

**关键实现细节：**

- **`TsStd` 内部用 double**：Python 端显式 `astype(np.float64)` 再 rolling 再 cast 回 float32。C++ 必须内部 double 累加，否则长序列累加误差超过容差。
- **`Ema` 初值**：pandas `ewm(adjust=False)` 的初值是第 `min_periods` 个有效值开始递推。`alpha = 2.0 / (span + 1)`。C++ 必须对齐这一行为。
- **`TsRank` 特殊分支**：`t <= 0` 全 NaN；`t == 1` 全 0。正常 case: `(rank - 1) / (n - 1)` 归一化到 [0,1]，ties 取平均秩。
- **`TsZscore` NaN mask**：当 `|std| < EPS` 或 `std` 为 NaN 时输出 NaN（不是 0）。

### 6.3 P2 — 双序列 / 特殊规则

| 算子 | Python 语义 | C++ 要点 | 容差 | 状态 |
|------|------------|---------|------|------|
| `Corr(x,y,t)` | `rolling(t).corr()` | online sum accumulators, `n*Sxy-Sx*Sy` 公式 | `1e-4` | ✅ done |
| `Cov(x,y,t)` | `rolling(t).cov()` | 同 Corr 的子计算 | `1e-4` | - |
| `Autocorr(x,t,n)` | `corr(x, x.shift(n), t)` | 精确复现 Python double-rolling 计算路径 | `1e-4` | ✅ done |
| `TsMinMaxDiff(x,t)` | `max - min`，**`min_periods=1`** | 双单调队列, min_periods=1 | `1e-6` | ✅ done |
| `TsSkew(x,t)` | `rolling(t).skew()` | Fisher-Pearson centered moments, 逐窗重算 | `1e-3` | ✅ done |

### 6.4 P3 — 低频（遇到再做）

`TsMed`, `TsMad`, `TsWMA`, `TsMaxDiff`, `TsMinDiff`, `TsKurt`

---

## 7. 对齐测试必测边界

每个算子至少覆盖：

| Case | 目的 |
|------|------|
| 纯随机 n=5000, seed=42 | 基础正确性 |
| 前 `t-1` 行 | 验证 NaN warmup（min_periods=t 时前 t-1 行必须为 NaN） |
| 输入含 NaN（2% 注入） | 验证 NaN 传播与 pandas 行为一致 |
| 全 NaN 输入 | 输出全 NaN |
| 极大/极小值 `1e30` | 不溢出 |
| window=1 | 退化情况（Ma(x,1) = x, TsRank(x,1) = 0） |
| 大窗口 window=2880 | 长窗口精度验证 |
| 多 window 参数 | `t in {5, 30, 60, 120, 480, 1440, 2880}` |

---

## 8. 开发流程（单个算子生命周期）

```
Step 1: 确认 Python 语义
  - 阅读 ts_ops.py 中的实现
  - 记录: pandas 调用、min_periods、dtype、EPS 使用

Step 2: 实现 C++ kernel
  - 新建 .hpp 文件到 native/include/fe/ops/
  - 遵循 push/output/ready/reset 接口 (stateful)
  - 或实现为 inline 函数 (stateless)

Step 3: pybind11 绑定
  - 在 fe_ops_bind.cpp 中添加对应的 Python 接口
  - 重新编译 fe_ops.so

Step 4: 对齐测试
  - 在 test_ops_alignment.py 中添加 test_xxx()
  - 运行: python -m tests.kernel.test_ops_alignment
  - 所有 case 通过 -> done
  - 容差不满足 -> 检查 C++ 精度策略，调整或记录差异

Step 5: 更新本文档
  - 标记该算子 C++ 状态为 done
  - 如有已知差异，记录在 S9 差异附录
```

---

## 9. 已知差异附录

本节记录 C++ 与 Python 之间无法 bit-identical 的已知情况。

| 算子 | 差异描述 | 容差 | 原因 |
|------|---------|------|------|
| `Ma` | 长序列累加顺序不同 | <= 1e-5 | ring buffer sum vs pandas Kahan-like |
| *(待补充)* | | | |

---

## 10. 测试策略

公司无 CI pipeline，不需要纯 C++ golden test。已移除：
- `native/tests/run_ma_golden.cpp`
- `tests/kernel/export_ts_op_golden.py`
- `CMakeLists.txt` 中对应的 `fe_ma_golden_tests` target

**唯一测试手段：Python pybind 对齐测试**

| 测试层 | 文件 | 用途 |
|--------|------|------|
| **Python pybind 对齐** | `tests/kernel/test_ops_alignment.py` + `fe_ops.so` | 全面对齐，任意参数组合和 NaN 注入 |

---

## 11. 建议落地顺序

| 阶段 | 任务 | 预计时间 | 状态 |
|------|------|---------|------|
| W1 | P0 全部 (unary + binary)，搭建 pybind 构建流程和对齐脚本框架 | 3 天 | ✅ 完成 |
| W2 | P1 核心: `TsStd`, `TsSum`, `Delay`, `TsDiff`, `TsPct`, `Ema` | 4 天 | ✅ 完成 |
| W3 | P1 续: `TsMin`, `TsMax`, `TsRank`, `TsZscore` | 3 天 | ✅ 完成 |
| W4 | P2: `Corr`, `Autocorr`, `TsMinMaxDiff`, `TsSkew` | 4 天 | ✅ 完成 |
| 后续 | P3: TsMed, TsMad, TsWMA, TsMaxDiff, TsMinDiff | 1 天 | ✅ 完成 |

**W1-W4 + P3 全部完成，覆盖 alpha_pool 中 100% 的 `ts_ops` 算子调用。共 33 个 C++ kernel，502 个对齐测试全部通过。**

---

*本文档应随 C++ kernel 的实现进展持续更新状态标记。当 ts_ops.py 发生变更时，需同步更新 reference 副本并重跑对齐测试。*
