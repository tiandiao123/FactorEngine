# 按币 Batch 的因子图运行时改造

> 日期：2026-11-24
> 状态：开发文档，待实现
> 分支：`feature/batch_graph`
> 目标：把因子推理从「一币一任务、因子 for 循环、标量 push」改成「一张逻辑图、状态按币展开、算子按截面 batch」。在 2000+ 币 × 1000+ 因子的规模下，单拍计算落在 1 秒以内，并给更短的预算留出余量。

---

## 1. 背景

当前实时链路已经把「收行情」和「算因子」拆开：

```text
WebSocket 线程
  -> 每个币一个 TradesAccumulator / KlinePublisher   # 标量增量，O(1)
ClockDriver
  -> 每个墙钟边界 flush 一轮
  -> bar_queue 放一条消息：这一拍所有币各一根 bar
factor-infer 线程
  -> 组出 dict[symbol, BarData]
  -> InferenceEngine.push_bars
  -> 8 个线程按币并行
  -> 每个币内部 for 循环推自己的因子图
```

行情侧维持现状。本文只改因子运行时，以及算子要新增的截面 batch 入口。

目标规模：

```text
symbols        >= 2000
factors        >= 1000
bar 周期       1s 或 5s
单拍计算预算   < 1s，最好更短
```

2000 × 1000 = 每拍 2×10⁶ 次因子更新。现有对象模型会实例化大约 200 万个 `FactorGraph`。滚动内核即使更新是 O(1)，仍然各自持有一条窗口 ring。按「每个因子 2 个滚动窗口、窗口 120、float32」估算：

```text
2000 × 1000 × 2 × 120 × 4 bytes ≈ 1.9 GB
```

再加上每个 `std::vector` 的堆分配，1 秒预算会先耗在对象数量、分配和缓存上，而不是耗在加法上。

---

## 2. 现在的实现

### 2.1 线程只并行到币

`factorengine/engine.py` 的 `_runtime_loop` 把一轮 bar 收成 `dict[symbol, BarData]`，一次调用 `push_bars`。

`InferenceEngine::push_bars`（`native/include/fe/runtime/inference_engine.hpp`）对每个币 `submit` 一个任务。线程池大小来自 `Engine(num_threads=...)`，Binance 示例里是 `NUM_THREADS = 8`。调用方用栅栏等到 `remaining == 0`。

一个任务的本体是该币的 `SymbolRunner::push_bar`：

```cpp
void push_bar(FeFloat close, FeFloat volume, ...) {
    for (std::size_t i = 0; i < graphs_.size(); ++i) {
        graphs_[i].push_bar(close, volume, open, high, low, ret,
                            vwap, buy_vol, sell_vol);
        outputs_[i] = graphs_[i].output();
    }
}
```

这里的 `i` 是因子，不是币。8 个线程是 8 个币各算各的。同一个币的几十到上千张图在一个线程里串行。

启动时每个币都会 `build_group()` 一次（`factorengine/engine.py` 的 `_add_runtime_symbol_with_factors`）。公式在 Python builder 里只有一份，运行时状态是「币 × 因子」份独立的 `FactorGraph`。

### 2.2 算子有两层，都不是截面 batch

以 `RollingMeanKernel` 为例（`native/include/fe/ops/rolling_mean.hpp`）：

```text
Layer 1  流式标量
  RollingMeanKernel::push(FeFloat x)
  一个内核对象 = 一个序列的窗口

Layer 2  时间向量
  rolling_mean(const FeFloat* x, FeFloat* out, int n, int window)
  新建一个内核，把同一根序列的 n 个历史点从头推完
```

`fe_ops.rolling_mean(x, window)` 的 `n` 是一根序列的时间长度。把 2000 个币这一拍的 `close[2000]` 塞进去是错的：那 2000 个数会被当成同一个窗口里的前后相邻点。

无状态算子（`add` / `log` / `div`）的数组接口同样是「一根序列的时间轴」，不是「多个币的同一拍」。

### 2.3 图节点只持有一份状态

`FactorGraph` 每个节点在 `compile()` 时 `allocate_kernel()` 一次，`push_bar` 用 `switch (op)` 把一个 float 推进去。节点的 `values_[i]` 也是一个标量。

因此：

```text
一次 push_bars
  = 200 个任务
  = 每个任务里 53 次标量图遍历（以 okx_perp_trades 为例）
  ≠ 一个算子扫过 close[200]
```

外面那一层是「一批任务」。计算本身仍是一币一数。

---

## 3. 两种改法

### 3.1 方案 A：因子图仍分开，截面做成 batch

一个因子一次处理 `n_symbols` 个币。状态排成 `state[node][symbol]`。8 个线程切币的区间，而不是一人领一个币再走进标量图。

收益：

```text
热循环是连续内存，可以 SIMD
对象数从「币 × 因子」降到「因子（或节点）」
和现有 fe_ops 的数组内核同一方向
```

代价：

```text
因子之间相同的 Ma(vwap, 60) 仍会各算各的
图执行器要从标量 push_bar 改成吃数组
```

### 3.2 方案 B：每个币一张大图，节点共享

把一个币上的 1000 个因子合成一张 DAG。相同输入、相同窗口的节点只留一份。

收益：

```text
公共子表达式只算一次
因子定义仍可以逐个写，编译期再合并
```

代价：

```text
每个币仍是标量走图
2000 份拓扑、2000 份堆分配还在
共享省掉的是重复计算，省不掉按币跳转的缓存失败
rank / corr 这类重算子一多，标量走图更难进 1 秒
```

### 3.3 结论

执行模型用方案 A。节点共享用方案 B 的编译结果，但不要做成「每个币一张物理大图」。

```text
1000 个因子
  -> 编译成一张逻辑图，公共节点只留一份
  -> 运行时只有一份拓扑
  -> 状态是 SoA：state[node][symbol]
  -> 每个节点的内核对币做连续循环
  -> 线程按币分段，每段把整张图跑完
```

对象数跟节点数走，不跟「币 × 因子」走。

不能把 2000 个币并进一个标量图来省状态。每个币的滚动窗口必须分开存。一张图跨币，只适合真正的横截面因子（同一拍要同时看很多币）。当前 `okx_perp` / `okx_perp_trades` 都是单币时间序列，没有这种依赖。

---

## 4. 目标运行时

### 4.1 数据形状

一拍输入，字段为行、币为列。币的下标在本轮内固定。

```text
symbols:  [0 .. N)
fields:   close[N], volume[N], open[N], high[N], low[N],
          ret[N], vwap[N], buy_vol[N], sell_vol[N]
```

Python 侧仍由 `_runtime_loop` 算 `ret`。组 batch 时写成连续数组，而不是 `dict[symbol, BarData]` 再在 C++ 里一个个拷贝。

输出：

```text
factor_values[factor_id][N]
```

缺币、前向填充的规则不变：空桶已经在 ClockDriver / accumulator 里填成一根 bar。推理侧收到的就是对齐后的 N 个值，其中价格可能是上一根 close，成交量可能是 0。某个币从启动起从未有过价格时，该列保持 NaN，内核按现有 NaN 语义处理。

### 4.2 逻辑图

编译产物是一张多输出 DAG，不是 1000 个互不相干的小图。

```text
输入节点     close / volume / open / high / low / ret / vwap / buy_vol / sell_vol
计算节点     一元、二元、滚动、双变量
输出节点     每个因子一个，保留 factor_id
```

公共子表达式在编译期合并。判定两个滚动节点相同的键：

```text
(op, input_node, window)                  # 一元滚动
(op, input_a, input_b, window)            # corr / beta / cov
(op, input_node, window, lag_or_scalar)   # autocorr / 标量运算
```

输入节点按字段名合并。`T0001` 和 `T0003` 都读 `buy_vol` 时，全图只有一个 `buy_vol` 输入。

第一期可以先不做合并，每个因子仍是独立子图，但执行已经是截面 batch。合并是第二期，避免第一期同时改正确性和调度。

### 4.3 状态

每个有状态节点持有 N 份窗口，N = 注册的币数。

第一期用「现成标量内核的数组」，保证和现在逐币结果一致：

```cpp
struct BatchRollingMean {
    std::vector<RollingMeanKernel> lanes;   // size = N
    void push_symbols(const FeFloat* x, FeFloat* out, int n);
};
```

`push_symbols` 只是：

```cpp
for (int i = 0; i < n; ++i)
    lanes[i].push(x[i]);
    out[i] = lanes[i].output();
```

第二期把热算子收成 SoA，同一条指令处理多个币：

```text
sum[N], sum_sq[N], count[N], nan_count[N]
ring[window * N]     // 同一时间下标的 N 个币连续存放
```

冷门算子（中位数、treap rank）可以停留在第一期的 lane 数组。它们不是 1 秒预算里的大头时，不值得先改布局。

### 4.4 调度

```text
线程 0    symbols[0, N/8)
线程 1    symbols[N/8, 2N/8)
...
每个线程把逻辑图从输入走到全部输出
一段币的状态互不交叉，不需要在节点之间加锁
```

不要再「一个币一个 task」。2000 个任务的提交、加锁、栅栏本身就会吃掉预算。固定 8 段（或 `num_threads` 段）连续区间即可。

动态增删币不要在一拍计算的中途改 N。沿用现在的约定：币种 diff 走 `bar_queue` 的控制消息，在两拍之间应用。SoA 变长时，在拍与拍之间按 symbol index 增删列，并保持其余列的窗口状态。

---

## 5. 算子改造

### 5.1 新增第三入口，不改前两个

| 入口 | 轴 | 状态 | 用途 |
|------|----|------|------|
| `Kernel::push(float)` | 一个点 | 一份窗口 | 现有流式图，继续保留 |
| `rolling_*(const float* x, float* out, int n, int window)` | 时间 | 函数内部新建一份，走完即弃 | `fe_ops` 离线整段数组 |
| `push_symbols(const float* x, float* out, int n)` | 币 | 调用之间保留 N 份窗口 | 新的在线推理 |

第三入口的 `x[i]` 是第 i 个币的**本拍新值**，不是第 i 个历史点。

无状态算子没有窗口，第三入口就是一层循环，直接调用现有标量函数（`fe::ops::add`、`log_op` 等）。

有状态算子第一期不改数学，只包一层 lane 数组。数值必须和「N 次 `push(x[i])`」逐位一致，包括 NaN、warmup 未满时的输出。

### 5.2 建议落地顺序

按现有因子实际用到的算子排，不要一次改完全部 `Op`。

```text
P0  无状态
    NEG ABS LOG SQR INV SIGN SQRT ADD SUB MUL DIV
    以及因子里用到的 SLOG1P / SIGN_SQRT / FMAX / FMIN

P1  滚动，O(1) 更新，因子里最多
    MA / SUM / STD / ZSCORE / DELAY / EMA
    DIFF / PCT

P2  因子里次多、状态更重
    RANK MIN MAX WMA SKEW KURT
    RSI CCI SUM_POS

P3  双变量
    CORR COV BETA INTERCEPT COS
```

每一类改完就用现有对齐测试的标量结果做对照，再接到 batch 图上。

### 5.3 明确不要做的事

```text
不要把 close[N] 传入现在的 rolling_mean(x, out, n, window)
不要为了 batch 重写一套公式
不要在第一期用 SIMD 改数值顺序，导致和标量路径对不齐
不要让 fe_ops 的时间向量接口改变语义
```

---

## 6. 图与引擎改造

### 6.1 新类型，旧类型留着

不直接把 `FactorGraph::push_bar(float, ...)` 改成数组。那个接口是现有测试、单币脚本和 `SymbolRunner` 的契约。

新增：

```text
BatchFactorGraph
  构造：由若干已 compile 的 FactorGraph 合成，或由一份合并后的节点表生成
  resize(n_symbols)
  push_bars(const BatchBar& bar, float* outputs, int n)
      bar.close 等每个字段是长度 n 的指针
      outputs 布局为 [factor][symbol]，factor 顺序与 factor_ids 一致

BatchInferenceEngine
  持有一份 BatchFactorGraph
  持有 symbol index <-> name
  push(BatchBar) 按 num_threads 分段
  get_outputs() 仍返回 Python 现在要的
      dict[symbol, dict[factor_id, float]]
```

`SymbolRunner` 和标量 `InferenceEngine` 先不动。新引擎走通、数值对齐之后，再让 `factorengine.engine.Engine` 选一条路径。

### 6.2 Python 组 batch 的位置

仍在 `_runtime_loop`，ClockDriver 之前的代码不动。

```text
round_bars, round_trades
  -> 按固定 symbol index 填连续数组
  -> 缺席的币写 NaN（动态币种未激活）或沿用现有前向填充结果
  -> BatchInferenceEngine.push
  -> 把列向量拆回 dict，写入 signal_deque
```

`signal_deque`、`get_factor_outputs()`、审计 JSONL 的对外形状不变。审计继续记 bar，不记因子。

### 6.3 因子注册

`FactorRegistry.build_group()` 仍然返回「一个因子一张标量图」。Batch 引擎负责把这些图编译成一张 batch 图：

```text
第一期：不合并节点。每张标量图的每个有状态节点变成 N 个 lane。
第二期：按第 4.2 节的键合并节点，再生成 lane / SoA。
```

这样因子作者继续写现在的 `@register_factor`，不要求先手写一张 1000 因子的大图。

---

## 7. 分阶段

### 阶段 0：文档与基准

本文。另外在改造前先记一笔现状耗时，避免以后没有对照。

```text
用仿真 bar，不连交易所
币数：200，以及能撑住的更大值（500 / 2000，视内存而定）
因子：okx_perp_trades 全组
线程：8
记录：push_bars 墙钟时间、每拍 p50 / p99、进程 RSS
```

这只作为改造前后的标尺，不把某一次数字写死成验收门槛。

### 阶段 1：lane 数组，数值对齐

```text
BatchRolling* 包住现有 Kernel
BatchFactorGraph 吃连续数组
单线程即可
与「每个币一个标量 FactorGraph」逐因子、逐币、逐拍比较
容差与 tests/kernels 里现有对齐测试一致
warmup 未完成、全 NaN、中间出现 NaN、空桶前向填充都要覆盖
```

此阶段不承诺 2000 × 1000 进 1 秒。它只证明截面 batch 没有改数学。

### 阶段 2：分段并行，接到 Engine

```text
num_threads 段连续币区间
Engine 增加 runtime = "scalar" | "batch"，默认 scalar
batch 路径的 get_factor_outputs() 与 scalar 路径同形
动态增删币发生在两拍之间
```

用阶段 0 的同一组仿真重复计时。

### 阶段 3：公共节点合并

```text
编译期按 (op, inputs, window) 合并
合并前后输出逐因子一致
统计合并掉的节点数，确认共享是实的
```

如果现有 53 个 trades 因子的重复节点很少，这一阶段对当前因子组收益有限，但对 1000 个手写因子是必要的。先做统计再决定要不要上 SoA。

### 阶段 4：热算子 SoA

只改阶段 1 计时里最贵的有状态算子。验收仍然是和标量路径逐位或按现有容差对齐，再加上阶段 0 的耗时对比。

rank、median、treap 保持 lane 数组，除非计时证明它们占了大头。

---

## 8. 测试

沿用 `tests/kernels` 的对齐方式，增加 batch 维度。

```text
同一条随机序列
  标量 Kernel 推 N 次
  BatchKernel.push_symbols 推一次长度 N 的截面
  输出一致

同一组因子、同一串 bar、两个币
  两个 SymbolRunner
  一个 BatchFactorGraph（N=2）
  每个因子的输出一致

N=1 的 batch 路径与今天的 push_bar 一致

空桶前向填充后的 bar 推进去，ret ≈ 0，窗口前进一格
从未成交的币，价格列是 NaN，不污染相邻币的 lane
```

动态币种：

```text
N 从 2 变到 3：新币窗口是冷的，旧币窗口不重置
N 从 3 变到 2：被删币的列不再出现在输出里，其余币的窗口继续
```

不把「1 秒」写成单元测试的时间断言。耗时放在阶段 0 / 阶段 2 的基准脚本里，环境不同不能当 CI 门槛。

---

## 9. 验收

功能：

```text
默认 Engine 行为不变
显式打开 batch 后，okx_perp_trades 与标量引擎对齐
get_factor_outputs() 的 dict 形状不变
审计仍只写聚合 bar
```

性能（仿真，8 线程，作为观察项而不是一次就卡死的门禁）：

```text
先报告当前 okx_perp_trades × 200 币的单拍耗时
再报告 batch 阶段 2 的同规模耗时
最后单独报告 2000 币 × 扩充到接近 1000 因子时的单拍耗时和 RSS
```

2000 × 1000 能否稳定小于 1 秒，取决于 rank / corr / median 的占比。阶段 4 之前先拿出算子耗时分解，再决定要不要改那些重内核。

---

## 10. 不在这次范围

```text
ClockDriver、TradesAccumulator、KlinePublisher 的线程模型
把 WebSocket 收包改成多线程
改因子公式或因子命名
把审计扩展成因子落盘
用时间向量接口冒充截面 batch
第一期就替换全部算子为手写 SIMD
横截面因子（一个节点同时读所有币）
```
