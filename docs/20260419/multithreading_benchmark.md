# InferenceEngine 多线程 Benchmark 报告

> 测试日期: 2026-04-19  
> 测试环境: gpu166, conda `torch_311`, Python 3.11  
> 测试脚本: `tests/runtime_engine/demo_latency.py`

---

## 1. 背景

`InferenceEngine` 是 FactorEngine 的 C++ 因子推理引擎，负责管理多个标的 (symbol) 的实时因子计算。每个标的由一个 `SymbolRunner` 管理，内含多个 `FactorGraph`（因子 DAG）。

本次实现将 `InferenceEngine` 从单线程升级为多线程，核心改动：

- 内嵌 `ThreadPool`（固定大小线程池，`std::thread` + task queue）
- 新增 `push_bars(bars_map)` 批量接口，一次调用并行分发所有 symbol
- pybind 层自动释放 GIL (`py::gil_scoped_release`)
- 保留原 `push_bar(symbol, ...)` 单标的接口（向后兼容）

## 2. 架构设计

```
Python                          C++ (no GIL)
──────                          ──────────────
                push_bars({
                  "BTC": BarData(...),       ┌─ worker 0 → SymbolRunner("BTC").push_bar()
                  "ETH": BarData(...),  ───► │─ worker 1 → SymbolRunner("ETH").push_bar()
                  "SOL": BarData(...),       │─ worker 2 → SymbolRunner("SOL").push_bar()
                  ...                        └─ ...
                })
                    │
                    ▼
              barrier.wait()  ◄── 所有 worker 完成后返回
```

**线程安全保证**：每个 `SymbolRunner` 完全独立（独立的 `FactorGraph`、`ring_buffer`），不同 symbol 之间零共享数据，因此计算路径**完全无锁**。

**锁的分布**：
| 锁 | 位置 | 作用 | 持有时间 |
|---|---|---|---|
| `ThreadPool::mtx_` | 任务队列 | 保护 `submit()`/`pop()` | ~纳秒 |
| `Barrier::mtx` | `push_bars()` 栈上 | 等待所有 task 完成 | 仅最后一个 worker 触发 notify |

计算热路径 (`SymbolRunner::push_bar()`) 完全在锁外执行。

## 3. 测试方法

### 测试指标

**Single-bar latency**: 引擎 warm up 后，测量单次 `push_bars()` 调用的延迟。这是生产场景中最关键的指标——一根新 K 线到来，从喂入数据到拿到所有标的的全部因子输出需要多久。

### 测试参数

| 参数 | 值 |
|---|---|
| 因子数/标的 | 5 |
| Warmup bars | 499 |
| 测量重复次数 | 100 (每次 reset + re-warmup) |
| 标的数量 | 10, 50, 100, 200 |
| 线程数 | 1, 2, 4, 8 |

### 5 个因子

来自 `factorengine/factors/okx_perp/factor_bank.py`：

| ID | 表达式 | 最大窗口 |
|---|---|---|
| 0001 | `Ma(TsStd(close, 120), 10)` | 120 |
| 0010 | `TsZscore(TsDiff(close, 5), 60)` | 65 |
| 0020 | `Neg(TsRank(volume, 30))` | 30 |
| 0050 | `Div(Sub(close, Ma(close, 60)), TsStd(close, 60))` | 60 |
| 0100 | `Corr(close, volume, 30)` | 30 |

## 4. 测试结果

### Single-bar Latency

```
 symbols |  threads |  mean (us) |   p50 (us) |   p99 (us) |  speedup
----------------------------------------------------------------------
      10 |        1 |         89 |         94 |        100 | baseline
      10 |        2 |         70 |         69 |         81 |    1.27x
      10 |        4 |         60 |         59 |         75 |    1.48x
      10 |        8 |         59 |         53 |         92 |    1.51x
----------------------------------------------------------------------
      50 |        1 |        352 |        364 |        419 | baseline
      50 |        2 |        217 |        213 |        266 |    1.62x
      50 |        4 |        141 |        142 |        174 |    2.49x
      50 |        8 |        143 |        132 |        277 |    2.46x
----------------------------------------------------------------------
     100 |        1 |        696 |        708 |        790 | baseline
     100 |        2 |        410 |        394 |        502 |    1.70x
     100 |        4 |        251 |        251 |        300 |    2.77x
     100 |        8 |        255 |        210 |        611 |    2.73x
----------------------------------------------------------------------
     200 |        1 |       1300 |       1301 |       1430 | baseline
     200 |        2 |        829 |        877 |        981 |    1.57x
     200 |        4 |        482 |        487 |        620 |    2.70x
     200 |        8 |        401 |        363 |        794 |    3.24x
----------------------------------------------------------------------
```

### 最优线程数选择

| 标的数 | 最优线程数 | 延迟 | 加速比 |
|--------|-----------|------|--------|
| 10 | 8 | 59 us | 1.51x |
| 50 | 4 | 141 us | 2.49x |
| 100 | 4 | 251 us | 2.77x |
| 200 | 8 | 401 us | 3.24x |

## 5. 分析

### 5.1 加速比分析

- **4 线程是当前甜点**：50-100 symbols 时 4 线程和 8 线程几乎无差别（单 symbol 仅 5 因子，计算量 ~7us，线程调度开销在 8 线程时成为瓶颈）
- **200 symbols 时 8 线程开始体现优势**（3.24x vs 2.70x），计算总量足够大时额外线程才有收益
- 理论加速上限受限于 Amdahl 定律：任务分发、barrier 等待、Python dict 构造等串行部分限制了加速比

### 5.2 尾延迟 (p99)

| 标的数 | 4 threads p99 | 8 threads p99 |
|--------|--------------|--------------|
| 50 | 174 us | 277 us |
| 100 | 300 us | 611 us |
| 200 | 620 us | 794 us |

8 线程的 p99 明显高于 4 线程——更多线程增加了 CPU 核争抢概率，导致偶发高延迟。**对尾延迟敏感的场景建议使用 4 线程**。

### 5.3 当前瓶颈

1. **每 symbol 计算量太轻**（5 因子 × ~1.4us/因子 ≈ 7us），线程调度开销（~10-20us）占比过高
2. **Python 端构造 `BarData` dict** 有额外开销（遍历所有 symbol 构造 dict 对象）
3. 随着因子数从 5 扩展到 50-100，单 symbol 计算量翻 10-20 倍，并行收益将显著增大

### 5.4 延迟预估（因子数扩展后）

假设单 symbol 计算量与因子数线性增长：

| 因子数 | 200 symbols × 1 thread | 200 symbols × 4 threads (预估) |
|--------|----------------------|-------------------------------|
| 5 | 1.3 ms | 0.48 ms |
| 50 | ~13 ms | ~3.5 ms |
| 100 | ~26 ms | ~7 ms |
| 200 | ~52 ms | ~14 ms |

## 6. 建议

1. **默认线程数**：设为 `min(4, hardware_concurrency())`，兼顾加速和尾延迟
2. **后续优化方向**：
   - 批量传入 numpy array 替代逐 symbol 构造 `BarData` dict，减少 Python 开销
   - 因子数量扩大后重新跑 benchmark 验证预估
   - 考虑 per-symbol 固定线程绑定（thread affinity）提升 cache 命中率
3. **生产环境**：200 标的 × 50 因子，4 线程，预计 p50 < 4ms

## 7. 相关文件

| 文件 | 说明 |
|---|---|
| `native/include/fe/runtime/inference_engine.hpp` | ThreadPool + InferenceEngine 实现 |
| `native/pybind/fe_runtime_bind.cpp` | pybind 绑定 (BarData, push_bars, GIL release) |
| `tests/factors/test_inference_engine.py` | 多线程正确性测试 (TestMultiThreaded, 6 cases) |
| `tests/runtime_engine/demo_latency.py` | 本报告的测试脚本 |
