# C++ Runtime 集成方案（2026-04-07）

## 1. 文档目的

这份文档讨论的不是当前 Python 原型怎么继续堆功能，而是：

**后续正式版本的 factor runtime 应该如何往 C++ 方向演进。**

重点回答三个问题：

1. C++ runtime 的职责边界应该是什么
2. 因子库到底应该写成 C++ 还是 numba
3. 现在这套 Python dataflow / scheduler 原型，应该如何平滑迁移到 C++

这份文档只做方案设计，不涉及代码实现。

---

## 2. 当前状态

当前系统已经具备：

### 2.1 Python Dataflow

已经能稳定提供三路数组化输入：

- `bars`: `dict[symbol, ndarray(N, 6)]`
- `trades`: `dict[symbol, ndarray(N, 3)]`
- `books`: `dict[symbol, ndarray(N, 20)]`

### 2.2 Python Scheduler Prototype

已经有最小原型：

- `Scheduler`
- `FactorRuntime`
- `FactorSpec`
- `FactorSnapshot`

并且你已经实测：

- 单 symbol 可以工作
- 全市场 304 个合约也可以跑
- 每个 tick 的 Python 原型耗时在毫秒级

这说明一件很重要的事：

**接口层和数据模型已经初步成形。**

所以现在讨论 C++ runtime，是合适的时机。

---

## 3. 要先明确的总原则

### 原则 1：C++ 负责热路径，不负责一切

不要因为目标是 C++ runtime，就把所有层都往 C++ 里塞。

更合理的边界是：

#### Python 保留

- dataflow 接入
- collector 生命周期
- 配置加载
- 测试脚本
- 调试和观测
- 回放驱动

#### C++ 接管

- factor runtime
- scheduler 热路径
- worker pool
- 因子库
- factor snapshot 计算
- 共享中间量和状态

### 原则 2：不要让 C++ runtime 再回头依赖 Python/Numba 热路径

如果已经进入 C++ runtime，就不应该在热路径里再“dispatch 到 numba”。

原因很简单：

- 又重新回到 Python 边界
- 又会碰到 GIL/对象开销
- 延迟会不稳定
- debug 更难

### 原则 3：先做清晰边界，再做极致性能

现在最重要的，不是“先把所有因子都改成 C++”，而是：

1. 定义清楚 C++ runtime 的输入
2. 定义清楚输出
3. 定义清楚运行时状态和调度方式

只要边界清楚，后面迁移会顺。

---

## 4. C++ Runtime 应该负责什么

我建议最终 C++ runtime 负责下面这些事情。

## 4.1 Evaluation Tick 驱动

它应该知道：

- 当前 tick_id
- 当前 ts_eval
- 当前要计算哪些 symbol
- 当前要计算哪些 factor

也就是说，**调度主循环最终也应该在 C++**。

但这不代表第一版迁移就要一步到位。

## 4.2 Cache Slicing

当前 Python prototype 已经验证了：

- bars 需要按最近 N 根切
- trades 需要按最近 N 笔切
- books 需要按最近 N 次更新切

最终这些 slicing 逻辑应该在 C++ runtime 里完成。

## 4.3 Factor 执行

每个 factor 的实际计算函数最终应在 C++ 中实现。

例如：

- `bar_momentum_20`
- `trade_imbalance_500`
- `book_l1_imbalance_50`

这类函数本质上都是对连续数组的简单算子，很适合 C++。

## 4.4 Worker Pool

未来真正提升性能的关键不是“每个因子一个线程”，而是：

- C++ worker pool
- 按 symbol shard 分片
- 每个 tick 上调度批量计算任务

## 4.5 Shared State / Shared Features

很多中间量会被多个因子重复使用，例如：

- latest mid
- spread
- l1/l5 imbalance
- trade signed volume
- rolling sums

这些应该在 runtime 里统一维护，而不是每个因子各算一遍。

---

## 5. 因子库到底该写成 C++ 还是 numba

这是最核心的问题之一。

我的判断非常明确：

## 5.1 生产热路径的因子库应直接写成 C++

原因：

1. 你做 C++ runtime 的目的，本来就是为了热路径稳定和低延迟
2. 如果 C++ runtime 再回头调用 numba，相当于又跨回 Python 世界
3. 这样会重新引入：
   - Python 对象边界
   - JIT 预热问题
   - GIL/解释器耦合
   - 调试和部署复杂度

所以：

**生产热路径里的因子库，应该直接用 C++ 内置实现。**

## 5.2 Numba 可以保留，但只适合两个场景

### 场景 A：研究阶段快速验证公式

例如：

- 先在 Python/Numba 里验证某个新因子值是不是合理
- 验证完之后再移植到 C++

### 场景 B：离线回测/研究环境

对于：

- 研究脚本
- notebook
- 小规模回放

numba 仍然有价值。

## 5.3 不推荐的方案

不推荐：

```text
C++ scheduler / runtime
-> 每个因子 dispatch 到 Python numba 函数
```

这个方案看上去两边都能用，但实际上最容易变成“最复杂、最难维护”的混合体。

一句话：

**numba 适合研究，不适合成为正式 C++ runtime 的计算后端。**

---

## 6. 既然数据现在是 numpy，怎么接 C++

这是另一个关键问题。

当前 dataflow 已经把数据整理成 numpy 数组：

- bars: `(N, 6)`
- trades: `(N, 3)`
- books: `(N, 20)`

所以最自然的第一步集成方式是：

## 6.1 第一步：Python 把 numpy arrays 直接传给 C++

也就是：

- dataflow 仍在 Python
- scheduler 原型可以先在 Python
- `FactorRuntime.evaluate()` 的核心部分改成调用 C++ runtime

比如概念上：

```python
snapshot = cpp_runtime.evaluate(
    tick_id=tick_id,
    ts_eval_ms=ts_eval_ms,
    bar_snapshot=bar_snapshot,
    trade_snapshot=trade_snapshot,
    book_snapshot=book_snapshot,
)
```

这一步的优点是：

- 边界非常清晰
- 与当前原型最接近
- 改动最小

## 6.2 第二步：逐步把调度和状态也迁到 C++

当第一步稳定后，再考虑：

- 把 Scheduler 也下沉到 C++
- 把 FactorRuntime 的内部状态完全迁到 C++
- Python 只保留：
  - dataflow
  - 控制平面
  - 调试

## 6.3 第三步：如果有必要，再考虑更深的 buffer ownership 优化

例如：

- C++ 持有内部 ring buffer
- Python dataflow 直接写到 C++ buffer

但这属于更后期的优化，不应该一开始就做。

---

## 7. 推荐的迁移路线

我建议按三阶段推进。

## Phase 1：Python Scheduler + C++ FactorRuntime Kernel

### 结构

```text
Python Dataflow
-> Python Scheduler
-> C++ Runtime.evaluate(snapshot)
-> FactorSnapshot
```

### 目标

- 不改 dataflow
- 不改测试方式
- 先把 factor 计算核心迁到 C++

### 优点

- 风险最小
- 接口最清楚
- 方便逐步替换 Python compute_fn

### 这一步适合做什么

- 把 `compute_bar_momentum`
- `compute_trade_imbalance`
- `compute_book_l1_imbalance`

这类简单函数先迁过去

---

## Phase 2：C++ Runtime 接管调度和 worker pool

### 结构

```text
Python Dataflow
-> C++ Scheduler / Runtime
-> Worker Pool
-> FactorSnapshot
```

### 目标

- 把 tick 逻辑移到 C++
- 把 symbol shard / worker pool 移到 C++
- 把 factor registry 移到 C++

### 这一步适合做什么

- 多 symbol 并行
- 多 factor 批量执行
- 共享中间量

---

## Phase 3：必要时进一步压缩 Python/C++ 边界

### 结构

```text
Python Dataflow (thin)
-> C++ Runtime-owned buffers
-> Scheduler / Worker / Factor Library
```

### 目标

- 降低 numpy 到 C++ 的边界开销
- 进一步控制延迟和内存布局

### 但注意

这一步是高级优化，不是第一目标。

---

## 8. C++ 代码层面的建议结构

如果你准备开始规划 `cpp/` 目录，我建议类似这样：

```text
cpp/
  runtime/
    include/
      factor_runtime/
        scheduler.hpp
        runtime.hpp
        factor_spec.hpp
        factor_snapshot.hpp
        factor_registry.hpp
        kernels/
          bar_kernels.hpp
          trade_kernels.hpp
          book_kernels.hpp
    src/
      scheduler.cpp
      runtime.cpp
      factor_registry.cpp
      factor_snapshot.cpp
      kernels/
        bar_kernels.cpp
        trade_kernels.cpp
        book_kernels.cpp
    pybind/
      module.cpp
```

### 这个结构的好处

- `scheduler`
  和 `runtime`
  分离
- `kernels`
  单独管理
- `pybind`
  单独管理 Python 绑定层

---

## 9. 对当前 Python 原型的建议

### 9.1 不要把当前 Python scheduler 当成最终实现

它的价值是：

- 验证 tick
- 验证 slicing
- 验证 factor snapshot 格式

不是最终性能方案。

### 9.2 现在最适合做的事

不是立刻把所有因子写成 C++，而是：

1. 先选 2-3 个代表性因子
2. 定清楚 C++ runtime 的 `evaluate()` 输入输出
3. 做第一版 pybind11 边界

### 9.3 最该避免的坑

最该避免的是：

- 一边写 C++ runtime
- 一边还想让生产热路径走 numba

这会把架构搞得非常不稳定。

---

## 10. 最终建议

我的明确建议是：

### 结论 1

**正式生产版 factor runtime 应该是 C++ 内置因子库，不应该依赖 numba 热路径。**

### 结论 2

**迁移顺序不要一步到位，先做 Python scheduler + C++ evaluate kernel。**

### 结论 3

**当前最合理的路线是：先把 C++ runtime 的输入输出边界定清楚，再开始迁移核心因子。**

一句话总结：

```text
Python dataflow 保留
Python scheduler 只是原型
生产热路径的 scheduler/runtime/factor library 最终都应迁到 C++
numba 只保留给研究和验证，不进入正式热路径
```
