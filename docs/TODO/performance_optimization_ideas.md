# 性能优化 — 后续想法（暂不实现）

> 来源：讨论 "每个因子编译成静态 .so" 方案时的分析与延伸。
> 日期：2026-04-18

---

## 背景

当前 DAG factor inference 采用**解释图 (interpreted graph)** 方案：

- `FactorGraph` 持有 `FactorNode` 拓扑序数组
- `push_bar()` 遍历节点，switch/dispatch 到对应 kernel
- 每 tick 每因子额外 dispatch 开销 ~50ns（15 节点图）
- 311 因子 × 300 标的，单 tick 总推理耗时 ~18ms
- tick 间隔 60,000ms（1 分钟 K 线），overhead 占比 <0.03%

**结论：当前阶段解释图已足够快，以下优化方案留作后续储备。**

---

## 方案 1：模板特化 — 编译期图展开（推荐，中等收益）

**思路**：对 top-N 高频因子，用 C++ 模板在编译期展开计算图，消除所有 dispatch 开销。与解释图共存于同一 .so。

```cpp
// 示例：编译期展开的 factor
template<int Window1, int Window2>
struct Factor0001_Static {
    RollingMean<Window1> ma;    // Window 作为模板参数
    RollingStd<Window2>  std;

    float push(float close) {
        float m = ma.push(close);
        float s = std.push(close);
        return (close - m) / (s + kEps);
    }
};
```

**优势**：
- 编译器可以内联全部调用、常量折叠窗口大小、优化寄存器分配
- 预计单因子 push 提速 ~25%（200ns → 150ns）
- 不需要 dlopen，与现有系统共存于单一 binary

**劣势**：
- 需要为每个特化因子手写模板代码
- 编译时间增加
- 只对 top-N 因子有价值

**触发条件**：tick 间隔降到 <5s 或单 tick 推理耗时 >50ms。

---

## 方案 2：批量 push + SIMD（推荐，高收益）

**思路**：将同一算子类型的所有节点 batch 在一起，利用 SIMD 向量化指令并行处理。

```cpp
// 批量 push 示意
void batch_push_rolling_mean(
    RollingMeanKernel* kernels[],  // N 个同类 kernel
    const float* inputs[],
    float* outputs[],
    int count
) {
    // 编译器可自动向量化这个循环
    for (int i = 0; i < count; i++) {
        outputs[i] = kernels[i]->push(inputs[i]);
    }
}
```

**优势**：
- 利用 AVX2/AVX-512 一次处理 8-16 个 float
- 对 300 标的 × 同一因子 的场景非常友好
- 改善 cache locality（同类 kernel state 连续内存布局）

**劣势**：
- 需要重新设计 kernel state 的内存布局（AoS → SoA）
- push 逻辑中的条件分支（NaN 处理等）会影响向量化效果

**触发条件**：标的数 >1000 且需要亚秒级延迟。

---

## 方案 3：每因子独立 .so（低优先级，工程代价大）

**思路**：每个因子编译成独立的共享库文件，dlopen 加载运行。

**优势**：
- 编译器能看到完整数据流，做全局优化（内联、常量折叠、分支消除）
- 理论上单因子 push 最快

**劣势**：
- 311 个 .cpp 文件需要维护，每加一个因子要写新 .cpp 并编译
- 编译时间长（311 × header-only 模板展开 = 分钟级）
- dlopen/dlclose 生命周期管理、符号冲突、ABI 兼容问题
- 调试困难（311 个 .so 的 crash stack trace）
- 修改窗口参数需要重新编译（解释图只需改数字重启）
- 实测收益有限：~50ns/factor/tick，总省 ~4.6ms/tick

**结论**：工程代价远大于性能收益，不推荐。

---

## 方案 4：跨标的并行调优

**思路**：当标的数大幅增长时，优化线程池策略。

可探索方向：
- **Work stealing** — 标的间计算量不均匀时自动平衡负载
- **NUMA-aware** — 大型多路服务器上按 NUMA 节点分配标的
- **协程化** — 如果因子计算中有 IO wait（如远端特征），用协程避免线程阻塞

**触发条件**：标的数 >3000 或多路服务器部署。

---

## 优先级排序

| 优先级 | 方案 | 预估收益 | 工程复杂度 | 触发条件 |
|--------|------|----------|-----------|---------|
| 1 | 批量 push + SIMD | 高 (2-4x) | 中 | 标的 >1000, 亚秒延迟 |
| 2 | 模板特化编译期图 | 中 (~25%) | 中 | tick <5s |
| 3 | 跨标的并行调优 | 视场景 | 低-中 | 标的 >3000 |
| 4 | 每因子独立 .so | 低 (~25%) | 高 | 不推荐 |

---

## 一句话版本

**当前解释图够用。如果未来需要压延迟，先做批量 SIMD，再做模板特化，.so 方案最后考虑。**
