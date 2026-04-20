# FactorEngine 待办任务清单

> 更新日期: 2026-04-19  
> 当前状态: S1/S2 完成, T1(多线程)/T3(Engine集成) 完成, 30 因子已翻译, **235 tests passing**

---

## 已完成

| 编号 | 任务 | 完成日期 | 备注 |
|------|------|---------|------|
| S1 | FactorGraph 核心实现 (DAG builder + push executor) | 04-18 | `factor_graph.hpp` |
| S2a | SymbolRunner 实现 | 04-18 | `symbol_runner.hpp` |
| S2c | pybind11 绑定 SymbolRunner + InferenceEngine | 04-18 | `fe_runtime_bind.cpp` |
| — | Python FactorRegistry + 按平台分子目录 | 04-18 | `factorengine/factors/` |
| — | FactorGraph 可视化工具 (ASCII / DOT / PNG) | 04-18 | `factorengine/factors/visualize.py` |
| — | 5 个真实因子翻译 + 端到端对齐测试 | 04-18 | `tests/factors/test_real_factors.py` |
| — | `pip install -e .` 一键安装 (setup.py + CMake) | 04-19 | `setup.py`, `pyproject.toml` |
| — | InferenceEngine 单线程版 | 04-18 | `inference_engine.hpp` (无线程池) |
| — | 开发教程文档 | 04-19 | `docs/20260419/inference_engine_tutorial.md` |
| T1 | InferenceEngine 多线程化 | 04-19 | `inference_engine.hpp` ThreadPool + `push_bars()` |
| — | 多线程 benchmark 报告 | 04-19 | `docs/20260419/multithreading_benchmark.md` |
| T3 | Engine 三线程架构重构 (v2) | 04-19 | `engine.py` bar_queue + runtime 线程解耦 |
| — | 30 个因子翻译 + 对齐测试 | 04-19 | `factor_bank.py`, `test_new_factors.py` |
| — | Engine 架构重构设计文档 | 04-19 | `docs/20260419/engine_architecture_refactor.md` |

---

## 待办任务

### P0 — 高优先级 (核心功能缺失)

#### ~~T1: InferenceEngine 多线程化~~ ✅ 已完成

已实现 C++ ThreadPool + `push_bars()` 批量接口, pybind 自动释放 GIL。200 标的 × 8 线程达 3.24x 加速。详见 `docs/20260419/multithreading_benchmark.md`。

---

#### T2: 翻译全量 310 个因子

**当前状态**: 已翻译 30 个 (0001-0034, 0050, 0100), 位于 `factorengine/factors/okx_perp/factor_bank.py`.

**目标**: 把 `rewritten_factor_bank` 的全部 311 个因子翻译为 `@register_factor` 建图函数.

**工作量估计**: 每个因子 ~5 分钟翻译 + 测试, 共约 3-5 天.

**建议分批**:
1. 先按因子复杂度分组:
   - 简单 (只用 P0+P1, ~150 个): 批量翻译
   - 中等 (用 P2, ~100 个): 逐个翻译
   - 复杂 (用 P3 或多输入, ~60 个): 仔细翻译
2. 每批翻译完跑对齐测试
3. 可按文件拆分: `factor_bank_001_050.py`, `factor_bank_051_100.py`, ...

**对齐测试策略**:
- 每个因子需要 Python pandas 参考实现 (ground truth)
- 用 3 个随机种子, 600 bars, atol=1e-3
- 已有框架: `tests/factors/test_real_factors.py` 可扩展

---

### P1 — 中优先级 (集成与工程化)

#### ~~T3: 与 Engine/Dataflow 集成~~ ✅ 已完成

已实现 v2 三线程架构: dataflow 线程 → `bar_queue` → runtime 线程 → `signal_deque` → 主线程。详见 `docs/20260419/engine_architecture_refactor.md`。

新增参数: `signal_buffer_size`, `bar_queue_size`, `bar_queue_timeout`。

---

#### T4: 多标的 × 多因子 × 多线程集成测试

**目标**: 端到端压力测试, 验证:
- 300+ 标的 × 5 因子 × 8 线程, 推 1000 bars
- 多线程结果与单线程完全一致 (bit-exact)
- 无内存泄漏 (valgrind / asan)
- 性能基准: 总延迟 < 目标值

**依赖**: T1 (多线程) 已完成, 可直接开始.

---

#### T5: 截面处理 Python 层

**目标**: 实现跨标的截面运算 (cross-sectional), 这部分留在 Python:
- `clean_factor`: `inf→NaN, NaN→0` (已内嵌在 `FactorGraph.output()`)
- `cross_sectional_zscore`: 所有标的同一因子的 zscore 标准化
- `cross_sectional_rank`: 截面排名
- `factor_neutralize`: 行业中性化 (如果需要)

**设计**: 在每个 tick 后, Python 收集所有标的的 factor outputs → numpy 截面运算 → 输出最终信号.

---

### P2 — 低优先级 (优化与增强)

#### T6: TsRank 流式优化

**当前问题**: 流式 `TsRankPush` 是 O(window) brute-force (每次 push 遍历整个 buffer 做排名). 批量版用 Fenwick Tree + 坐标压缩是 O(log n), 但流式因为不知道未来数据, 无法预先做坐标压缩.

**可能方案**:
- 平衡二叉搜索树 (std::set + order statistic tree / policy-based tree)
- BIT 动态离散化 (值域分桶)
- 对于常见窗口 (120, 180), 当前 brute-force 已足够快 (~2μs/push)

**优先级低**: 除非 profiling 显示 TsRank 是瓶颈.

---

#### T7: FactorGraph 序列化 / 反序列化

**目标**: 把编译好的 FactorGraph 序列化到文件, 避免每次启动重新建图.

**方案**: protobuf 或自定义二进制格式, 存储节点列表 + 拓扑 + 窗口参数.

**优先级低**: 建图时间可忽略 (5 因子 < 1ms), 即使 310 个因子也 < 100ms.

---

#### T8: 动态因子增删

**目标**: 运行时动态添加或移除因子, 不需要重启 `InferenceEngine`.

**涉及**:
- `SymbolRunner::remove_factor(factor_id)`
- `InferenceEngine::add_factor_to_all(factor_id, graph)`
- 线程安全: 需要 reader-writer lock 或 copy-on-write

---

#### T9: 因子输出持久化

**目标**: 把因子输出流式写入时序数据库 (InfluxDB / QuestDB / Parquet 文件).

**设计**: Python 薄层, 每 N 个 bar 批量 flush.

---

#### T10: 监控与告警

**目标**: 运行时监控:
- 每标的 push 延迟 (p50 / p99)
- 因子输出异常检测 (连续 N 个 0.0, 突变)
- warmup 进度
- 线程池利用率

---

## 任务依赖图

```
T2 (翻译剩余280因子) ──┐
                        │
T1 (多线程) ✅ ──┬── T4 (压力测试) ──── T3 (接入Engine) ✅
                  │                          │
                  │                          v
                  │                    T5 (截面处理)
                  │
                  └── T6 (TsRank优化, 可选)

T7 (序列化)  ← 独立, 低优先级
T8 (动态增删) ← 独立, 低优先级
T9 (持久化)  ← 依赖 T3 ✅
T10 (监控)   ← 依赖 T3 ✅
```

## 建议执行顺序

```
当前阶段:  T2 (翻译剩余因子) + T4 (压力测试) 可并行
下一阶段:  T5 (截面处理) → T9 (持久化) → T10 (监控)
优化阶段:  T6 ~ T8 按需
```

---

## 当前代码统计

| 类别 | 文件数 | 代码行 (估) |
|------|--------|------------|
| C++ headers (ops/) | 15 | ~3,500 |
| C++ headers (runtime/) | 4 | ~700 |
| C++ pybind | 2 | ~280 |
| Python (factorengine/) | 8 | ~450 |
| Python (tests/) | 10 | ~2,200 |
| 测试数 | — | **235 passing** |
| 已翻译因子 | — | **30 / 311** |
