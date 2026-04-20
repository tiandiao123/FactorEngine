# DAG Factor Inference Runtime 设计文档

> **Date:** 2026-04-18  
> **Scope:** 将 rewritten_factor_bank 的 311 个 Python 因子翻译为 C++ 增量 DAG，按标的线程并行推理  
> **前置:** 33 个 C++ kernel 已全部对齐（P0-P3），502/502 测试通过

---

## 1. 核心决策

| 问题 | 决定 | 原因 |
|------|------|------|
| AST 自动翻译 vs 手动翻译 | **手动翻译** | 因子数量有限(311)、表达式结构规整、自动翻译的 debug 成本 > 手动翻译成本 |
| 执行模型 | **每标的一个线程，线程内串行求值所有因子** | 每个标的的 kernel 有独立状态，无跨标的依赖 |
| 数据流 | **push-based 增量** | 每根新 bar 到达时 push 一次，O(1) 状态更新（无需回溯整个窗口） |
| 截面处理 | **Python 薄层** | `clean_factor` 只是 `inf→NaN, NaN→0`；截面 zscore 是全标的向量运算，C++ 收益低 |

---

## 2. 因子表达式模式分析

通过分析 311 个因子，提炼出以下共性：

### 2.1 输入特征

所有因子只读取 kbar 的以下列（通过 `pivot_table`）:

| 列名 | 含义 | 使用频率 |
|------|------|---------|
| `close` | 收盘价 | ~100% (几乎所有因子) |
| `volume` / `volCcyQuote` | 成交量/成交额 | ~70% |
| `ret` | 收益率（预计算） | ~10% |
| `open` / `high` / `low` | OHLC | ~5% |

**约束：** 每个因子的输入是**单标的时序**（pivot 后是 per-coin Series），无跨标的读取。

### 2.2 计算结构

**所有因子都是纯 DAG（无循环、无条件分支）**，模式为：

```
Input Features (close, volume, ...)
       │
       ▼
  ts_ops chain: Ma → Sub → Div → TsRank → Neg → ...
       │
       ▼
  Output: 单标量（iloc[-1] of the final Series）
```

典型复杂度：5-15 个算子节点，最大窗口 120-480 bars。

### 2.3 额外操作（需补充支持）

| 操作 | 出现次数 | 说明 | 处理方式 |
|------|---------|------|---------|
| `close.pct_change()` | ~32 个因子 | 等价于 `TsPct(close, 1)` 但语义略不同 | 翻译为 `Div(Sub(x, Delay(x,1)), Delay(x,1))` 或加 `pct_change` kernel |
| `Sub(x, scalar)` / `Sub(scalar, x)` | ~5 个因子 | 如 `Sub(1.0, TsRank(...))` | 已有 binary scalar 重载 |
| `clean_factor(signal)` | 100% | `inf→NaN, NaN→0` | 在 output 后用 C++ inline 处理 |

---

## 3. 架构设计

### 3.1 分层架构

```
┌─────────────────────────────────────────────────────────────────┐
│  Python Orchestration Layer                                      │
│  - Engine.start/stop, 配置加载, 标的管理                          │
│  - 截面后处理 (cross_sectional_zscore)                            │
│  - FactorSnapshot 输出                                           │
└──────────────────────────┬──────────────────────────────────────┘
                           │  每 tick: bar_snapshot dict
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  C++ Factor Runtime (pybind11 暴露)                               │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  FactorGraph                                              │    │
│  │  - nodes[]: 拓扑序排列的算子节点                           │    │
│  │  - push_bar(close, volume, ...) → 更新所有节点状态          │    │
│  │  - output() → float (最后一个节点的当前值)                  │    │
│  │  - ready() → bool (warmup 是否完成)                        │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  SymbolRunner                                             │    │
│  │  - factors[]: vector<FactorGraph> 该标的所有因子            │    │
│  │  - push_bar(bar) → 遍历所有因子 push                       │    │
│  │  - get_signals() → vector<float>                          │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                   │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  InferenceEngine (线程池)                                  │    │
│  │  - runners: map<symbol, SymbolRunner>                      │    │
│  │  - push_bars(bar_snapshot) → 并行分发到各线程               │    │
│  │  - collect() → dict<symbol, vector<float>>                 │    │
│  └──────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 核心类设计

#### FactorNode — DAG 中单个算子节点

```cpp
struct FactorNode {
    enum class Op {
        // 输入
        INPUT_CLOSE, INPUT_VOLUME, INPUT_RET, INPUT_OPEN, INPUT_HIGH, INPUT_LOW,
        // P0 无状态
        NEG, ABS, LOG, SQR, INV, SIGN, TANH, SLOG1P,
        ADD, SUB, MUL, DIV,
        ADD_SCALAR, SUB_SCALAR, MUL_SCALAR, DIV_SCALAR,  // scalar overloads
        SCALAR_SUB, SCALAR_DIV,                           // scalar on left
        // P1 rolling
        MA, TS_SUM, TS_STD, TS_VARI, EMA, TS_MIN, TS_MAX,
        TS_RANK, TS_ZSCORE, DELAY, TS_DIFF, TS_PCT,
        // P2
        CORR, AUTOCORR, TS_MINMAX_DIFF, TS_SKEW,
        // P3
        TS_MED, TS_MAD, TS_WMA, TS_MAX_DIFF, TS_MIN_DIFF,
        // derived
        PCT_CHANGE,  // close.pct_change(n), equivalent to (x - delay(x,n)) / delay(x,n)
    };

    Op op;
    int input_a;         // index into node_values[], or -1 for inputs
    int input_b;         // second input (for binary ops like Corr), or -1
    int window;          // rolling window parameter
    float scalar;        // scalar parameter for ADD_SCALAR etc.
    void* kernel_state;  // opaque pointer to stateful kernel (Ma, TsStd, etc.)
};
```

#### FactorGraph — 单个因子的完整计算图

```cpp
class FactorGraph {
public:
    // 构建时：按拓扑序添加节点
    void add_input(const char* feature_name);   // close, volume, ...
    void add_unary(Op op, int src);
    void add_binary(Op op, int src_a, int src_b);
    void add_rolling(Op op, int src, int window);
    void add_bivariate(Op op, int src_a, int src_b, int window);
    void add_scalar_op(Op op, int src, float scalar);
    void compile();  // 分配 kernel state, 计算 warmup_bars

    // 运行时：每根 bar 调用一次
    void push_bar(float close, float volume, float open, float high, float low, float ret);
    bool ready() const;        // warmup 完成?
    float output() const;      // 最后一个节点的当前值
    void reset();              // 重置所有状态

private:
    std::vector<FactorNode> nodes_;
    std::vector<float> values_;       // 当前 tick 每个节点的值
    int output_node_;
    int warmup_bars_;
    int bars_seen_;
};
```

#### SymbolRunner — 单标的全因子执行器

```cpp
class SymbolRunner {
public:
    void add_factor(std::unique_ptr<FactorGraph> graph);
    
    // 每根 bar 调用，更新所有因子
    void push_bar(float close, float volume, float open, float high, float low, float ret);
    
    // 收集所有因子信号
    std::vector<float> get_signals() const;
    
private:
    std::vector<std::unique_ptr<FactorGraph>> factors_;
};
```

#### InferenceEngine — 多标的并行推理引擎

```cpp
class InferenceEngine {
public:
    InferenceEngine(int num_threads);
    
    void register_symbol(const std::string& symbol);
    void add_factor_to_all(FactorGraph&& prototype);  // 克隆到每个标的
    
    // 每 tick 调用：传入所有标的的 bar 数据，并行推理
    // bar_data: dict[symbol] → (close, volume, open, high, low, ret)
    void push_bars(/* bar snapshot */);
    
    // 收集结果: dict[symbol] → vector<float> (311 个因子信号)
    std::unordered_map<std::string, std::vector<float>> collect() const;
    
private:
    std::unordered_map<std::string, SymbolRunner> runners_;
    ThreadPool pool_;
};
```

---

## 4. 因子翻译规范

### 4.1 翻译模板

每个 Python 因子翻译为一个 C++ `FactorGraph` 构建函数：

**Python 原始（0001.py）：**

```python
close = kbar.pivot_table(index="time", columns="coin", values="close")
deviation = Sub(close, Ma(close, 120))
vol = TsStd(close, 60)
signal = Div(deviation, vol)
return clean_factor(signal)
```

**C++ 翻译：**

```cpp
FactorGraph build_factor_0001() {
    FactorGraph g;
    int close = g.add_input("close");          // node 0
    int ma120 = g.add_rolling(Op::MA, close, 120);     // node 1
    int dev   = g.add_binary(Op::SUB, close, ma120);   // node 2
    int vol   = g.add_rolling(Op::TS_STD, close, 60);  // node 3
    int sig   = g.add_binary(Op::DIV, dev, vol);       // node 4  ← output
    g.compile();
    return g;
}
```

### 4.2 翻译规则

| Python | C++ 翻译 |
|--------|---------|
| `Ma(x, t)` | `add_rolling(Op::MA, x, t)` |
| `TsStd(x, t)` | `add_rolling(Op::TS_STD, x, t)` |
| `Sub(a, b)` | `add_binary(Op::SUB, a, b)` |
| `Div(a, b)` | `add_binary(Op::DIV, a, b)` |
| `Neg(x)` | `add_unary(Op::NEG, x)` |
| `Corr(x, y, t)` | `add_bivariate(Op::CORR, x, y, t)` |
| `Sub(1.0, TsRank(...))` | `add_scalar_op(Op::SCALAR_SUB, rank_node, 1.0f)` |
| `close.pct_change()` | `add_rolling(Op::PCT_CHANGE, close, 1)` |
| `clean_factor(signal)` | 自动在 `output()` 中处理：`inf→NaN→0` |

### 4.3 warmup 计算

每个因子的 `warmup_bars` 由 DAG 中最长依赖链决定。例如：

```
Factor 0100:
  TsRank(close, 180) → max_window = 180
  TsStd(divergence, 360) → max_window = 360
  Ma(divergence, 30) → max_window = 30
  
  Critical path: TsRank(180) → Sub → Ma(30) → Div(需要 TsStd(360))
  warmup = max(180, 360) + 30 = 390 bars
```

实际可简化为：**沿 DAG 每个 path 累加 window 取最大值**。

---

## 5. 线程池执行流程

### 5.1 每 tick 执行流程

```
Engine tick (1分钟一次)
    │
    ├─ Engine.get_data() → bar_snapshot: dict[symbol, ndarray]
    │
    ├─ InferenceEngine.push_bars(bar_snapshot)
    │   │
    │   ├─ Thread 1: BTC-USDT-SWAP  → SymbolRunner.push_bar() → 311 factors 串行
    │   ├─ Thread 2: ETH-USDT-SWAP  → SymbolRunner.push_bar() → 311 factors 串行
    │   ├─ Thread 3: SOL-USDT-SWAP  → ...
    │   ├─ ...
    │   └─ Thread N: XRP-USDT-SWAP  → ...
    │   │
    │   └─ barrier: 等所有线程完成
    │
    ├─ InferenceEngine.collect()
    │   → dict[symbol][factor_id] = float
    │
    ├─ Python: cross_sectional_zscore per factor
    │   → for each factor: rank/zscore across all symbols
    │
    └─ Output: FactorSnapshot
        → dict[factor_id] → dict[symbol] → float
```

### 5.2 线程模型

- **线程数 = min(CPU cores, num_symbols)**，典型 300+ 标的，16-32 线程足够
- 每个标的绑定到线程池的一个 task，无锁（各标的状态完全独立）
- 线程池在 `InferenceEngine` 构造时创建，生命周期与 Engine 一致
- 每 tick 提交 `num_symbols` 个 task，wait all 后 collect

### 5.3 耗时估算

| 环节 | 单标的耗时 | 说明 |
|------|----------|------|
| 311 factors × push_bar | ~2-5 ms | 每个因子 ~10-15 µs (5-15 节点，n=1 per tick) |
| collect | ~0.1 ms | memcpy |
| 截面 zscore (Python) | ~1 ms | 311 factors × 300 symbols numpy |
| **总计 (16 threads, 300 symbols)** | **~60-100 ms** | 300/16 ≈ 19 轮 × 5ms |

在 1 分钟 tick 间隔下，<100ms 绰绰有余。

---

## 6. pybind11 接口设计

```cpp
PYBIND11_MODULE(fe_runtime, m) {
    py::class_<FactorGraph>(m, "FactorGraph")
        .def(py::init<>())
        .def("add_input", &FactorGraph::add_input)
        .def("add_unary", &FactorGraph::add_unary)
        .def("add_binary", &FactorGraph::add_binary)
        .def("add_rolling", &FactorGraph::add_rolling)
        .def("add_bivariate", &FactorGraph::add_bivariate)
        .def("add_scalar_op", &FactorGraph::add_scalar_op)
        .def("compile", &FactorGraph::compile)
        .def("push_bar", &FactorGraph::push_bar)
        .def("output", &FactorGraph::output)
        .def("ready", &FactorGraph::ready)
        .def("reset", &FactorGraph::reset);

    py::class_<InferenceEngine>(m, "InferenceEngine")
        .def(py::init<int>(), py::arg("num_threads") = 0)
        .def("register_symbol", &InferenceEngine::register_symbol)
        .def("add_factor_to_all", &InferenceEngine::add_factor_to_all)
        .def("push_bars", &InferenceEngine::push_bars)
        .def("collect", &InferenceEngine::collect);
}
```

Python 侧用法：

```python
import fe_runtime

engine = fe_runtime.InferenceEngine(num_threads=16)

# 注册标的
for sym in symbols:
    engine.register_symbol(sym)

# 加载因子（手动翻译的 C++ 构建函数，通过 pybind 暴露）
for factor_fn in [build_factor_0001, build_factor_0002, ...]:
    engine.add_factor_to_all(factor_fn())

# 每 tick
while True:
    bar_snapshot = data_engine.get_data()  # dict[symbol] → ndarray
    engine.push_bars(bar_snapshot)
    signals = engine.collect()  # dict[symbol] → list[float]
    
    # 截面处理 (Python)
    factor_snapshot = cross_sectional_process(signals)
```

---

## 7. 实施计划

| 阶段 | 任务 | 预计 | 产出 |
|------|------|------|------|
| **S1** | FactorGraph + FactorNode 核心实现 | 2-3 天 | `factor_graph.hpp` |
| **S2** | 手动翻译 10 个代表性因子，做对齐验证 | 1-2 天 | `factors/` + `test_factor_alignment.py` |
| **S3** | SymbolRunner + 线程池 InferenceEngine | 2 天 | `inference_engine.hpp` |
| **S4** | pybind11 绑定 + Python 集成测试 | 1 天 | `fe_runtime_bind.cpp` |
| **S5** | 批量翻译剩余 301 个因子 | 3-5 天 | 全部因子 C++ 版本 |
| **S6** | 与 Scheduler / Engine 集成 | 1 天 | 端到端流程 |

### S1 优先做的事

1. `FactorNode` struct + `Op` enum
2. `FactorGraph::push_bar()` — 按拓扑序遍历节点，调用对应 kernel
3. 复用现有 33 个 kernel 的**有状态版本**（Kernel 类 + push/output 接口）
4. 关键：每个有状态节点维护自己的 ring buffer / kernel state，**push 一次只处理一个新值**

### 现有 kernel 适配

当前 `fe_ops` 的 kernel 是 **array-level** 函数（一次处理整个数组）。DAG 执行需要 **push-level** 接口（一次处理一个值）。

好消息：P1 的 `RollingMeanKernel`, `RollingStdKernel`, `RollingExtremalKernel` 等已经有 `push()` + `output()` 接口。需要补充的是 P0 无状态算子（trivial）和部分 P2/P3 算子的 push 版本。

---

## 8. 关键设计约束

1. **无跨标的依赖**：每个 `SymbolRunner` 完全独立，线程安全无锁
2. **无条件分支**：所有因子是纯 DAG，无 if/else
3. **编译期确定结构**：因子的节点数、连接关系、窗口参数在构建时固定，运行时不变
4. **增量更新**：每根 bar 只 push 一次，有状态 kernel 内部维护滑动窗口
5. **clean_factor 内联**：`output()` 自动执行 `if (isinf(v) || isnan(v)) return 0.0f`
6. **不做截面运算**：`cross_sectional_zscore` 留在 Python，因为它需要所有标的的最新值
