# FactorGraph 架构详解

> **Date:** 2026-04-18  
> **读者:** 需要理解 FactorEngine DAG 推理全貌的开发者  
> **前置:** 33 个 C++ kernel 已对齐，FactorGraph S1 实现已完成并通过 120 项测试

---

## 1. 全局架构

```
┌─────────────────────────────────────────────────────────────┐
│                    Python 应用层                              │
│  • 建图：调用 FactorGraph.add_*() 描述因子表达式               │
│  • 注册：将编译好的图交给 SymbolRunner / InferenceEngine       │
│  • 收集：获取因子信号，做截面处理 (clean_factor, zscore)        │
└──────────────────────┬──────────────────────────────────────┘
                       │ pybind11 (一次性建图调用)
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                    C++ 推理层                                 │
│                                                             │
│  InferenceEngine (线程池, 未来 S2)                            │
│  ├── SymbolRunner("BTC")                                    │
│  │   ├── FactorGraph #0001  ←  每根 bar push 一次           │
│  │   ├── FactorGraph #0002                                  │
│  │   └── ...                                                │
│  ├── SymbolRunner("ETH")                                    │
│  │   ├── FactorGraph #0001  ←  独立状态，互不干扰            │
│  │   └── ...                                                │
│  └── SymbolRunner("SOL")                                    │
│      └── ...                                                │
└─────────────────────────────────────────────────────────────┘
```

**关键原则：Python 建图，C++ 推理。** 建图只做一次（启动时），推理做百万次。

---

## 2. 核心概念

### 2.1 FactorGraph = 一个因子表达式

一个 `FactorGraph` 实例对应一个因子公式。例如因子 0001：

```
Div(Sub(close, Ma(close, 120)), TsStd(close, 60))
```

对应的 `FactorGraph` 内部是一个 DAG（有向无环图），由多个 `FactorNode` 组成。

### 2.2 FactorNode = 图中的一个节点

```cpp
struct FactorNode {
    Op op;              // 算子类型 (MA, SUB, DIV, ...)
    int input_a = -1;   // 第一个输入：values_[] 的索引
    int input_b = -1;   // 第二个输入（二元算子才用）
    int window  = 0;    // 滚动窗口大小
    float scalar = 0.0; // 标量参数
    KernelVar kernel;   // 有状态算子的 kernel 实例
};
```

### 2.3 values_[] = 节点间的数据总线

`values_[]` 是一个 float 数组，长度等于节点数。每个节点计算完把结果写入 `values_[i]`，下游节点通过 `input_a` / `input_b` 索引来读取。

---

## 3. 完整生命周期

### 3.1 建图阶段（Python 端，只执行一次）

以因子 0001 为例：

```python
import fe_runtime as rt
Op = rt.Op

g = rt.FactorGraph()

# Step 1: 声明输入
c = g.add_input("close")           # 节点 0: INPUT_CLOSE

# Step 2: 构建计算链
ma120 = g.add_rolling(Op.MA, c, 120)      # 节点 1: Ma(close, 120)
dev   = g.add_binary(Op.SUB, c, ma120)    # 节点 2: close - Ma(close, 120)
vol   = g.add_rolling(Op.TS_STD, c, 60)   # 节点 3: TsStd(close, 60)
sig   = g.add_binary(Op.DIV, dev, vol)    # 节点 4: 最终输出

# Step 3: 编译
g.compile()
```

**建图完成后 FactorGraph 的内部状态：**

```
nodes_[0] = { op=INPUT_CLOSE, input_a=-1, input_b=-1, window=0,   kernel=monostate  }
nodes_[1] = { op=MA,          input_a=0,  input_b=-1, window=120, kernel=RollingMeanKernel(120) }
nodes_[2] = { op=SUB,         input_a=0,  input_b=1,  window=0,   kernel=monostate  }
nodes_[3] = { op=TS_STD,      input_a=0,  input_b=-1, window=60,  kernel=RollingStdKernel(60)  }
nodes_[4] = { op=DIV,         input_a=2,  input_b=3,  window=0,   kernel=monostate  }

values_ = [NaN, NaN, NaN, NaN, NaN]   (长度 5)
warmup_bars_ = 120                     (最大窗口路径)
output_node_ = 4                       (最后一个节点)
```

### 3.2 compile() 做了什么

`compile()` 在建图结束后调用一次，完成三件事：

1. **分配 values_ 数组**：`values_.assign(nodes_.size(), NaN)`
2. **实例化 kernel**：为每个有状态节点创建 kernel 对象（`RollingMeanKernel`, `TsRankPush` 等）
3. **计算 warmup_bars**：沿 DAG 反向传播，累加所有路径上的窗口大小

```
warmup 计算示例（因子 0001）:

节点 0 (INPUT):   max_dep=0, self_window=0   → warmup=0
节点 1 (MA,120):  max_dep=0, self_window=120 → warmup=120
节点 2 (SUB):     max_dep=max(0,120)=120     → warmup=120
节点 3 (STD,60):  max_dep=0, self_window=60  → warmup=60
节点 4 (DIV):     max_dep=max(120,60)=120    → warmup=120

总 warmup = warmup[output_node_=4] = 120
```

### 3.3 push_bar() 执行流程（C++ 端，每根 bar 执行一次）

```
push_bar(close=102.3, volume=3200, ...)

按拓扑序遍历 nodes_[0] → nodes_[4]:

节点 0 (INPUT_CLOSE):
    values_[0] = 102.3

节点 1 (MA, input_a=0):
    a = values_[0] = 102.3
    RollingMeanKernel.push(102.3)   ← 内部更新 ring buffer + sum
    values_[1] = kernel.output()    ← 窗口满了返回均值，否则 NaN

节点 2 (SUB, input_a=0, input_b=1):
    a = values_[0] = 102.3
    b = values_[1] = Ma 输出
    values_[2] = a - b              ← 直接调 fe::ops::sub()

节点 3 (TS_STD, input_a=0):
    a = values_[0] = 102.3
    RollingStdKernel.push(102.3)
    values_[3] = kernel.output()

节点 4 (DIV, input_a=2, input_b=3):
    a = values_[2] = deviation
    b = values_[3] = std
    values_[4] = a / b              ← 直接调 fe::ops::div_op()

因子输出 = values_[4]
```

### 3.4 warmup 与 ready()

```
bars_seen:  1  2  3 ... 119  120  121  122 ...
ready():    ✗  ✗  ✗ ...  ✗    ✓    ✓    ✓  ...
output():  NaN        ... NaN  有效值  有效值 ...
```

- `ready()` 返回 `bars_seen >= warmup_bars`
- `output()` 在 ready 之后返回有效值（inf/NaN 会被替换为 0.0）
- `raw_output()` 返回原始值（保留 NaN/inf，用于测试对齐）

---

## 4. Kernel 的两层架构

```
fe/ops/                           fe/runtime/kernels.hpp
──────────                        ─────────────────────
标量函数 (P0):                     直接调用，不包装
  neg(x), add(a,b), ...             factor_graph.hpp 里直接写
                                     fe::ops::neg(a)

有状态 Kernel 类 (部分 P1):        直接使用，不包装
  RollingMeanKernel                  KernelVar 里直接存
  RollingSumKernel                   fe::ops::RollingMeanKernel
  RollingStdKernel
  EmaKernel
  RollingMinKernel / MaxKernel
  DelayKernel

                                   组合类 (Composite):
                                     RollingVarComposite  = StdKernel + 平方
                                     TsDiffComposite      = DelayKernel + 减法
                                     TsPctComposite       = DelayKernel + 除法
                                     TsZscoreComposite    = MeanKernel + StdKernel
                                     AutocorrPush         = DelayKernel + CorrPush

                                   全新实现 (ops/ 里没有):
                                     TsRankPush, CorrPush,
                                     TsMinMaxDiffPush, TsSkewPush,
                                     TsMedPush, TsMadPush, TsWmaPush,
                                     TsMaxDiffPush, TsMinDiffPush
```

**原则：`ops/` 有的就直接用，不重复实现。`runtime/kernels.hpp` 只补充缺失的。**

---

## 5. 推荐部署方案：Python 建图 + C++ 推理

### 5.1 为什么不全用 C++ 建图？

| 对比 | Python 建图 | C++ 建图 |
|------|------------|----------|
| 建图代码量 | 5-10 行/因子 | 5-10 行/因子 |
| 可读性 | 高，接近原始因子表达式 | 中等 |
| 灵活性 | 可循环、读配置、动态生成 | 需要重新编译 |
| 推理性能 | **完全相同**（都是 C++ 执行） | 完全相同 |
| 建图性能 | 微秒级（只调几次 add_*） | 微秒级 |

建图通过 pybind11 调用，但 `add_input()`, `add_rolling()` 等方法内部只是 `push_back` 一个 struct，开销可忽略。**编译后的 FactorGraph 是纯 C++ 对象，push_bar() 不经过 Python。**

### 5.2 端到端流程

```
启动阶段（一次性）:
┌────────────────────────────────────────────────────────┐
│  Python                                                │
│                                                        │
│  1. 读取因子配置 (310 个因子表达式)                       │
│  2. 创建 InferenceEngine(num_threads=4)                │
│  3. for symbol in symbols:                             │
│         engine.add_symbol(symbol)                      │
│         for factor_id in factor_ids:                   │
│             g = build_factor_graph(factor_id)  ←建图    │
│             g.compile()                                │
│             engine.add_factor(symbol, g)  ←移交C++持有  │
│                                                        │
│  此后 Python 不再触碰 FactorGraph 的内部状态             │
└────────────────────────────────────────────────────────┘

推理阶段（每根 bar）:
┌────────────────────────────────────────────────────────┐
│  Python                                                │
│                                                        │
│  bar = receive_market_data()                           │
│  signals = engine.push_bar("BTC", bar)  ──────────┐   │
│                                          pybind11  │   │
│  # signals 是 310 个 float 的 list     穿越一次   │   │
│  # 做截面处理: clean_factor, zscore              │   │
│                                                    │   │
└────────────────────────────────────────────────────┘   │
                                                         │
     ┌───────────────────────────────────────────────────┘
     │  C++ (纯 C++，无 GIL)
     ▼
┌────────────────────────────────────────────────────────┐
│  SymbolRunner::push_bar(close, volume, ...)            │
│  │                                                     │
│  ├── graphs_[0].push_bar(close, vol, ...)  → output 0 │
│  ├── graphs_[1].push_bar(close, vol, ...)  → output 1 │
│  ├── ...                                               │
│  └── graphs_[309].push_bar(...)            → output 309│
│  │                                                     │
│  └── return std::vector<float>{o0, o1, ..., o309}      │
└────────────────────────────────────────────────────────┘
```

### 5.3 性能估算

单个 `push_bar()` 执行时间：

| 因子复杂度 | 节点数 | 估算耗时/push |
|-----------|:------:|:------------:|
| 简单 (0001) | 5 | ~200ns |
| 中等 (0050) | 7 | ~500ns |
| 复杂 (0010) | 12 | ~1μs |

310 个因子 × 1 个标的 ≈ **100-300μs / bar**

线程池 4 线程，100 个标的 → 每根 bar 理论耗时 ≈ 100×300μs/4 ≈ **7.5ms**

对于 1 分钟 bar 来说绰绰有余（60 秒窗口内完成 7.5ms 的计算）。

---

## 6. 真实因子翻译示例

### 6.1 因子 0001: 波动率归一化价格偏离

**Python 原始表达式：**
```python
deviation = Sub(close, Ma(close, 120))
vol = TsStd(close, 60)
signal = Div(deviation, vol)
```

**C++ FactorGraph 翻译（在 Python 端调用 pybind11）：**
```python
g = rt.FactorGraph()
c     = g.add_input("close")                     # 节点 0
ma120 = g.add_rolling(Op.MA, c, 120)             # 节点 1
dev   = g.add_binary(Op.SUB, c, ma120)           # 节点 2
vol   = g.add_rolling(Op.TS_STD, c, 60)          # 节点 3
sig   = g.add_binary(Op.DIV, dev, vol)           # 节点 4
g.compile()
```

**DAG 可视化：**
```
[close] ──┬──────────────────── [SUB] ── [DIV] ── output
           │                      ↑        ↑
           ├── [Ma(120)] ─────────┘        │
           │                               │
           └── [TsStd(60)] ───────────────┘
```

### 6.2 因子 0020: 区间效率 × 量比 (多输入)

**Python 原始表达式：**
```python
rolling_high = TsMax(high, 120)
rolling_low  = TsMin(low, 120)
range_pos    = Div(Sub(close, rolling_low), Sub(rolling_high, rolling_low))
centered     = Sub(range_pos, 0.5)
vol_ratio    = Div(Ma(volume, 15), Ma(volume, 120))
raw          = Mul(centered, vol_ratio)
signal       = Neg(TsZscore(raw, 240))
```

**C++ FactorGraph 翻译：**
```python
g  = rt.FactorGraph()
c  = g.add_input("close")                              # 0
h  = g.add_input("high")                               # 1
lo = g.add_input("low")                                # 2
v  = g.add_input("volume")                             # 3
rh = g.add_rolling(Op.TS_MAX, h, 120)                  # 4
rl = g.add_rolling(Op.TS_MIN, lo, 120)                 # 5
rng      = g.add_binary(Op.SUB, rh, rl)                # 6
pos      = g.add_binary(Op.DIV,
               g.add_binary(Op.SUB, c, rl), rng)       # 7,8
centered = g.add_scalar_op(Op.SUB_SCALAR, pos, 0.5)    # 9
vs = g.add_rolling(Op.MA, v, 15)                        # 10
vl = g.add_rolling(Op.MA, v, 120)                       # 11
vr = g.add_binary(Op.DIV, vs, vl)                       # 12
raw = g.add_binary(Op.MUL, centered, vr)                # 13
zs  = g.add_rolling(Op.TS_ZSCORE, raw, 240)             # 14
sig = g.add_unary(Op.NEG, zs)                           # 15
g.compile()
```

### 6.3 翻译规则速查表

| Python 表达式 | FactorGraph 调用 |
|---------------|-----------------|
| `close` | `g.add_input("close")` |
| `volume` | `g.add_input("volume")` |
| `Neg(x)` | `g.add_unary(Op.NEG, x)` |
| `Log(x)` | `g.add_unary(Op.LOG, x)` |
| `SLog1p(x)` | `g.add_unary(Op.SLOG1P, x)` |
| `Add(x, y)` | `g.add_binary(Op.ADD, x, y)` |
| `Sub(x, y)` | `g.add_binary(Op.SUB, x, y)` |
| `Mul(x, y)` | `g.add_binary(Op.MUL, x, y)` |
| `Div(x, y)` | `g.add_binary(Op.DIV, x, y)` |
| `Sub(x, 0.5)` | `g.add_scalar_op(Op.SUB_SCALAR, x, 0.5)` |
| `Sub(1.0, x)` | `g.add_scalar_op(Op.SCALAR_SUB, x, 1.0)` |
| `Ma(x, 120)` | `g.add_rolling(Op.MA, x, 120)` |
| `Ema(x, 60)` | `g.add_rolling(Op.EMA, x, 60)` |
| `TsStd(x, 60)` | `g.add_rolling(Op.TS_STD, x, 60)` |
| `TsRank(x, 180)` | `g.add_rolling(Op.TS_RANK, x, 180)` |
| `TsZscore(x, 240)` | `g.add_rolling(Op.TS_ZSCORE, x, 240)` |
| `TsMin(x, 120)` | `g.add_rolling(Op.TS_MIN, x, 120)` |
| `TsMax(x, 120)` | `g.add_rolling(Op.TS_MAX, x, 120)` |
| `Delay(x, 5)` | `g.add_rolling(Op.DELAY, x, 5)` |
| `TsDiff(x, 30)` | `g.add_rolling(Op.TS_DIFF, x, 30)` |
| `TsPct(x, 1)` | `g.add_rolling(Op.TS_PCT, x, 1)` |
| `pct_change()` | `g.add_rolling(Op.PCT_CHANGE, x, 1)` |
| `Corr(x, y, 120)` | `g.add_bivariate(Op.CORR, x, y, 120)` |
| `Autocorr(x, 20, 5)` | `g.add_autocorr(x, 20, 5)` |
| `TsMed(x, 60)` | `g.add_rolling(Op.TS_MED, x, 60)` |
| `TsMad(x, 60)` | `g.add_rolling(Op.TS_MAD, x, 60)` |
| `TsWMA(x, 30)` | `g.add_rolling(Op.TS_WMA, x, 30)` |
| `TsMaxDiff(x, 60)` | `g.add_rolling(Op.TS_MAX_DIFF, x, 60)` |
| `TsMinDiff(x, 60)` | `g.add_rolling(Op.TS_MIN_DIFF, x, 60)` |

---

## 7. 测试验证

### 7.1 已有测试

| 测试文件 | 内容 | 数量 |
|---------|------|:----:|
| `tests/kernel/test_ops_alignment.py` | P0-P1 单算子对齐 | 61 |
| `tests/kernel/test_p2_alignment.py` | P2 算子对齐 | 16 |
| `tests/kernel/test_p3_alignment.py` | P3 算子对齐 | 12 |
| `tests/kernel/test_factor_graph.py` | FactorGraph 集成测试 | 16 |
| `tests/factors/test_real_factors.py` | **真实因子端到端对齐** | 15 |
| **总计** | | **120** |

### 7.2 真实因子测试覆盖

| 因子 | 描述 | 涉及算子 | 节点数 |
|------|------|---------|:------:|
| 0001 | 波动率归一化偏离 | Sub, Ma, TsStd, Div | 5 |
| 0050 | 量价 Rank 相关 | PctChange, TsRank, Corr, Neg | 7 |
| 0010 | 量价凸性背离 | Ma, Sub, Div, TsStd, SLog1p, TsRank, Neg | 12 |
| 0020 | 区间效率×量比 | TsMax, TsMin, Sub, Div, SUB_SCALAR, Ma, Mul, TsZscore, Neg | 13 |
| 0100 | 量价背离反转 | TsRank, Sub, Ma, TsStd, Div, Neg | 9 |

每个因子用 3 个随机种子测试，全部 15/15 通过。

---

## 8. 代码文件索引

```
native/include/fe/
├── ops/                          # 已有 C++ 算子 kernel
│   ├── spec.hpp                  #   FeFloat 类型定义, EPS 常量
│   ├── unary.hpp                 #   P0 一元标量函数
│   ├── binary.hpp                #   P0 二元标量函数
│   ├── rolling_mean.hpp          #   RollingMeanKernel (push-level)
│   ├── rolling_sum.hpp           #   RollingSumKernel
│   ├── rolling_std.hpp           #   RollingStdKernel
│   ├── rolling_ema.hpp           #   EmaKernel
│   ├── rolling_minmax.hpp        #   RollingMinKernel / RollingMaxKernel
│   ├── rolling_rank.hpp          #   Fenwick Tree 版 (batch only)
│   ├── rolling_median.hpp        #   sorted buffer 版 (batch)
│   ├── rolling_skew.hpp          #   moment 累加 (batch)
│   └── shift.hpp                 #   DelayKernel
│
└── runtime/                      # DAG 推理层
    ├── kernels.hpp               #   补充 kernel: Composite + 全新 push-level
    └── factor_graph.hpp          #   FactorGraph 核心实现

native/pybind/
├── fe_ops_bind.cpp               # 批量数组算子的 Python 绑定
└── fe_runtime_bind.cpp           # FactorGraph 的 Python 绑定

tests/
├── kernel/                       # 单算子级别测试
│   ├── test_ops_alignment.py
│   ├── test_p2_alignment.py
│   ├── test_p3_alignment.py
│   └── test_factor_graph.py
└── factors/                      # 真实因子端到端测试
    └── test_real_factors.py
```

---

## 9. 下一步 (S2)

| 步骤 | 内容 | 优先级 |
|------|------|:------:|
| S2a | 实现 `SymbolRunner`：持有 N 个 FactorGraph，一次 push_bar 产出 N 个信号 | 高 |
| S2b | 实现 `InferenceEngine`：线程池 + 多标的并行 | 高 |
| S2c | pybind11 绑定 SymbolRunner / InferenceEngine | 高 |
| S2d | 翻译全部 310 个因子的建图函数 (Python) | 中 |
| S2e | 集成测试：多标的 × 多因子 × 多线程 | 中 |
