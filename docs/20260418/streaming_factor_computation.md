# 因子流式计算（Streaming Factor Computation）详解

> **Date:** 2026-04-18  
> **目的:** 深入理解流式因子推理的原理、流程与实现细节

---

## 1. 批量 vs 流式：两种计算范式

### 1.1 批量模式（Batch）

```
每根新 K 线到达时：

  ┌───────────────────────────────────────────────┐
  │   历史 K 线数组 (4320 根)                       │
  │   [bar₀, bar₁, bar₂, ..., bar₄₃₁₈, bar₄₃₁₉] │
  │                                    ▲ 新加入的  │
  └───────────────────┬───────────────────────────┘
                      │
                      ▼
  ┌───────────────────────────────────────────────┐
  │   pandas rolling 全量计算                      │
  │   close.rolling(120).mean()  → 计算 4320 次    │
  │   close.rolling(60).std()   → 计算 4320 次    │
  │   (close - ma) / std        → 计算 4320 次    │
  └───────────────────┬───────────────────────────┘
                      │
                      ▼
  ┌───────────────────────────────────────────────┐
  │   取最后一个值  .iloc[-1]                      │
  │   4320 次计算只用了 1 个结果                    │
  └───────────────────────────────────────────────┘
```

**问题：每次重算前 4319 行都是浪费的。**

### 1.2 流式模式（Streaming）

```
每根新 K 线到达时：

  ┌────────────────────────┐
  │   只有 1 个新值: bar₄₃₁₉ │
  └───────────┬────────────┘
              │
              ▼
  ┌───────────────────────────────────────────────────────┐
  │   有状态 Kernel（内存中维护滑动窗口的累积信息）          │
  │                                                         │
  │   Ma_kernel:  running_sum, ring_buffer[120]             │
  │   Std_kernel: running_sum, running_sum_sq, ring_buffer  │
  │                                                         │
  │   push(new_value) → O(1) 更新 → 产出当前值              │
  └───────────────────────┬─────────────────────────────────┘
                          │
                          ▼
  ┌───────────────────────────────────────────────────────┐
  │   直接得到最新时间点的因子值（不需要 .iloc[-1]）        │
  └───────────────────────────────────────────────────────┘
```

**核心：Kernel 自带记忆，不需要回看历史。**

### 1.3 计算量对比

假设 311 个因子，300 个标的，平均窗口 120：

| | 批量模式 | 流式模式 |
|---|---|---|
| 每 tick 计算次数 | 311 × 300 × 4320 ≈ **4 亿** | 311 × 300 × 15 ≈ **140 万** |
| 耗时 | ~2-5 秒 | ~60-100 毫秒 |
| 内存 | 完整 DataFrame（~500MB） | Kernel 状态（~50MB） |
| 随历史增长 | 线性变慢 | **恒定** |

---

## 2. 完整示例：从因子表达式到流式执行

### 2.1 选一个具体因子

Python 原始代码：

```python
# Factor 0001: 价格偏离度
close = kbar.pivot_table(index="time", columns="coin", values="close")
deviation = Sub(close, Ma(close, 120))
vol = TsStd(close, 60)
signal = Div(deviation, vol)
return clean_factor(signal)
```

含义：**(当前价格 - 120 均线) / 60 标准差**，衡量价格相对均值偏离了几个标准差。

### 2.2 解析为 DAG

```
                    close (输入)
                   ╱      ╲
                  │         │
                  ▼         ▼
            Ma(close,120)  TsStd(close,60)
                  │         │
                  ▼         │
          Sub(close, ma)    │
                  │         │
                  ▼         ▼
             Div(deviation, vol)
                  │
                  ▼
              clean_factor
                  │
                  ▼
               output
```

### 2.3 编译为拓扑序节点数组

调用 `FactorGraph` 的构建 API：

```cpp
FactorGraph g;
int close = g.add_input("close");                       // node[0]: INPUT
int ma120 = g.add_rolling(Op::MA, close, 120);           // node[1]: Ma(node[0], 120)
int dev   = g.add_binary(Op::SUB, close, ma120);         // node[2]: Sub(node[0], node[1])
int vol   = g.add_rolling(Op::TS_STD, close, 60);        // node[3]: TsStd(node[0], 60)
int sig   = g.add_binary(Op::DIV, dev, vol);              // node[4]: Div(node[2], node[3])
g.compile();
```

编译后的内部数据结构：

```
nodes_[] (拓扑序数组):
┌──────┬─────────────┬─────────┬─────────┬────────┬───────────────────┐
│ idx  │ op          │ input_a │ input_b │ window │ kernel_state      │
├──────┼─────────────┼─────────┼─────────┼────────┼───────────────────┤
│  0   │ INPUT_CLOSE │   -     │   -     │   -    │ null              │
│  1   │ MA          │   0     │   -     │  120   │ RollingMeanKernel │
│  2   │ SUB         │   0     │   1     │   -    │ null (无状态)     │
│  3   │ TS_STD      │   0     │   -     │   60   │ RollingStdKernel  │
│  4   │ DIV         │   2     │   3     │   -    │ null (无状态)     │
└──────┴─────────────┴─────────┴─────────┴────────┴───────────────────┘

values_[5]: [?, ?, ?, ?, ?]   ← 每个节点当前 tick 的输出值
warmup_bars_ = 120            ← 需要 120 根 K 线才能产出有效值
```

### 2.4 compile() 做了什么

```
compile() 的三个任务:

  1. 分配 kernel state
     ┌──────────────────────────────────────────┐
     │ node[1] Ma(120)  → new RollingMeanKernel │
     │   内部: ring_buffer[120], running_sum     │
     │                                           │
     │ node[3] TsStd(60) → new RollingStdKernel │
     │   内部: ring_buffer[60], sum, sum_sq      │
     │                                           │
     │ node[0,2,4] → null (无状态，不需要分配)   │
     └──────────────────────────────────────────┘

  2. 计算 warmup_bars
     ┌──────────────────────────────────────────────────┐
     │ 沿每条路径累加窗口:                                │
     │   路径 A: close → Ma(120) → Sub → Div  累计=120  │
     │   路径 B: close → TsStd(60)      → Div  累计=60  │
     │   warmup_bars = max(120, 60) = 120                │
     └──────────────────────────────────────────────────┘

  3. 分配 values_[] 数组
     values_ = new float[5]{NaN, NaN, NaN, NaN, NaN}
```

**compile() 之后图结构完全冻结，运行时只有 kernel 内部状态和 values_[] 在变。**

---

## 3. push_bar() 逐步执行流程

### 3.1 执行流程图

```
push_bar(close=105.3) 被调用
│
│  Step 1: 填入原始输入
│  ┌──────────────────────────────────────┐
│  │ values_[0] = 105.3   (INPUT_CLOSE)  │
│  └──────────────────────────────────────┘
│
│  Step 2: 按拓扑序遍历 node[1] → node[4]
│
│  ┌─ node[1]: Ma(window=120) ─────────────────────────────┐
│  │  input = values_[0] = 105.3                            │
│  │  kernel.push(105.3)                                    │
│  │    → ring_buffer 中 oldest 值 = 98.7                   │
│  │    → running_sum += 105.3 - 98.7 = +6.6                │
│  │    → output = running_sum / 120 = 102.15                │
│  │  values_[1] = 102.15                                   │
│  └────────────────────────────────────────────────────────┘
│
│  ┌─ node[2]: Sub(无状态) ────────────────────────────────┐
│  │  input_a = values_[0] = 105.3                          │
│  │  input_b = values_[1] = 102.15                         │
│  │  values_[2] = 105.3 - 102.15 = 3.15                   │
│  └────────────────────────────────────────────────────────┘
│
│  ┌─ node[3]: TsStd(window=60) ──────────────────────────┐
│  │  input = values_[0] = 105.3                            │
│  │  kernel.push(105.3)                                    │
│  │    → 更新 running_sum, running_sum_sq                  │
│  │    → std = sqrt(sum_sq/n - (sum/n)²) = 4.82           │
│  │  values_[3] = 4.82                                     │
│  └────────────────────────────────────────────────────────┘
│
│  ┌─ node[4]: Div(无状态) ────────────────────────────────┐
│  │  input_a = values_[2] = 3.15                           │
│  │  input_b = values_[3] = 4.82                           │
│  │  values_[4] = 3.15 / 4.82 = 0.6535                    │
│  └────────────────────────────────────────────────────────┘
│
│  Step 3: 输出
│  output_ = values_[4] = 0.6535
│  clean_factor: 不是 inf/NaN → 直接返回 0.6535
│
└─ 完成！耗时 ~200ns
```

### 3.2 伪代码

```cpp
void FactorGraph::push_bar(float close, float volume, ...) {
    // Step 1: 填入输入特征
    for (auto& node : nodes_) {
        if (node.op == Op::INPUT_CLOSE)  values_[&node - &nodes_[0]] = close;
        if (node.op == Op::INPUT_VOLUME) values_[&node - &nodes_[0]] = volume;
        // ... 其他输入
    }

    // Step 2: 按拓扑序遍历（跳过 INPUT 节点）
    for (int i = first_non_input_; i < num_nodes_; i++) {
        auto& node = nodes_[i];
        float a = values_[node.input_a];
        float b = (node.input_b >= 0) ? values_[node.input_b] : 0.0f;

        switch (node.op) {
            // P0 无状态算子
            case Op::NEG:  values_[i] = -a;     break;
            case Op::ADD:  values_[i] = a + b;   break;
            case Op::SUB:  values_[i] = a - b;   break;
            case Op::MUL:  values_[i] = a * b;   break;
            case Op::DIV:  values_[i] = a / b;   break;
            // ...

            // P1-P3 有状态算子 — 调用 kernel 的 push
            case Op::MA:
            case Op::TS_STD:
            case Op::TS_RANK:
            case Op::EMA:
            // ... 所有 rolling 算子
                values_[i] = node.kernel->push(a);
                break;

            // P2 双输入有状态算子
            case Op::CORR:
                values_[i] = node.kernel->push(a, b);
                break;
        }
    }

    // Step 3: clean_factor
    float v = values_[output_node_];
    output_ = (std::isinf(v) || std::isnan(v)) ? 0.0f : v;
    bars_seen_++;
}
```

---

## 4. Kernel 状态机详解

### 4.1 有状态 Kernel 的通用模型

每个有状态 Kernel 都是一个**微型状态机**：

```
                 ┌──────────────────────────────────────┐
                 │         Kernel 内部                   │
                 │                                       │
  push(value) ──►│  ring_buffer[window]  ← 存最近 t 个值 │
                 │  running_stats        ← 累积统计量    │
                 │  count                ← 已接收值的数量 │
                 │                                       │
                 │  if count < min_periods:              │
                 │      return NaN                       │
                 │  else:                                │
                 │      return computed_value             │
                 │                                       │
                 └──────────────────────────────────────┘
```

### 4.2 Ma(120) 的状态机（最简单的例子）

```
初始状态: running_sum = 0, count = 0, ring_buffer = [NaN × 120]

push(100.0):  count=1,   ring_buffer[0]=100.0, sum=100.0     → NaN (count < 120)
push(101.5):  count=2,   ring_buffer[1]=101.5, sum=201.5     → NaN
push(99.8):   count=3,   ring_buffer[2]=99.8,  sum=301.3     → NaN
  ...
push(102.3):  count=120, ring_buffer[119]=102.3, sum=12180.0 → 12180.0/120 = 101.5 ✅
  ↑ 窗口首次填满，开始产出有效值

push(105.3):  count=121
  → oldest = ring_buffer[0] = 100.0  (即将被挤出)
  → ring_buffer[0] = 105.3           (覆盖)
  → sum += 105.3 - 100.0 = +5.3
  → sum = 12185.3
  → output = 12185.3 / 120 = 101.54  ✅

push(103.1):  count=122
  → oldest = ring_buffer[1] = 101.5
  → ring_buffer[1] = 103.1
  → sum += 103.1 - 101.5 = +1.6
  → output = 12186.9 / 120 = 101.56  ✅

... 每次 push 只做 1 次加法 + 1 次减法 + 1 次除法 = O(1)
```

### 4.3 各算子的状态与复杂度

| Kernel | 内部状态 | push 复杂度 | 说明 |
|--------|---------|------------|------|
| Ma(t) | ring_buffer[t] + sum | O(1) | 加新减旧 |
| TsSum(t) | ring_buffer[t] + sum | O(1) | 同 Ma，不除以 t |
| TsStd(t) | ring_buffer[t] + sum + sum_sq | O(1) | Welford 在线算法 |
| TsVari(t) | ring_buffer[t] + sum + sum_sq | O(1) | 同 TsStd，不开根 |
| Ema(t) | prev_output (1 个 float) | O(1) | α × new + (1-α) × old |
| TsMin(t) | ring_buffer[t] + monotonic_deque | 均摊 O(1) | 单调递增 deque |
| TsMax(t) | ring_buffer[t] + monotonic_deque | 均摊 O(1) | 单调递减 deque |
| TsRank(t) | ring_buffer[t] + Fenwick Tree | O(log t) | 坐标压缩 + BIT |
| TsMed(t) | sorted_buffer[t] | O(t) | 排序插入/删除 |
| TsMad(t) | 两趟 sorted_buffer[t] | O(t) | 中位数后再求中位数 |
| TsWMA(t) | ring_buffer[t] + weights[t] | O(t) | 加权求和 |
| Corr(t) | ring_buffer_x[t] + ring_buffer_y[t] + 5 个 sum | O(1) | 在线 Pearson |
| Delay(t) | ring_buffer[t] | O(1) | 取 t 步前的值 |
| TsDiff(t) | ring_buffer[t] | O(1) | x - delay(x, t) |
| TsPct(t) | ring_buffer[t] | O(1) | x / delay(x, t) - 1 |
| Neg/Add/Sub/... | 无 | O(1) | 纯计算 |

---

## 5. Warmup 阶段详解

### 5.1 为什么需要 Warmup

流式 Kernel 启动时状态是空的。以 Ma(120) 为例：

```
时间线:
bar₀   bar₁   bar₂   ...   bar₁₁₈   bar₁₁₉   bar₁₂₀   bar₁₂₁   ...
│       │       │              │        │         │         │
▼       ▼       ▼              ▼        ▼         ▼         ▼
NaN     NaN     NaN    ...    NaN      101.5     101.54    101.56
                                         ▲
                                    窗口首次填满
                                    第一个有效输出

├──── warmup 阶段 (119 bars) ────┤├── 稳态推理 ──►
```

### 5.2 嵌套算子的 Warmup 传播

对于 `TsZscore(Ma(close, 120), 60)`:

```
                   Ma(close, 120)          TsZscore(_, 60)
                   
bar₀ ─────────►    NaN          ─────────► NaN
bar₁ ─────────►    NaN          ─────────► NaN
  ...
bar₁₁₈ ──────►    NaN          ─────────► NaN
bar₁₁₉ ──────►    101.5  ✅    ─────────► NaN  (TsZscore 刚收到第 1 个有效值)
bar₁₂₀ ──────►    101.54 ✅    ─────────► NaN  (第 2 个)
  ...
bar₁₇₈ ──────►    102.01 ✅    ─────────► 0.73 ✅  (第 60 个，窗口首次填满)

warmup_bars = 120 + 60 - 1 = 179
              ↑ Ma 需要的     ↑ TsZscore 需要的

├────── warmup 阶段 (178 bars) ──────────────┤├── 稳态推理 ──►
```

### 5.3 DAG 中的 Warmup 计算规则

对于任意 DAG，warmup 的计算方式是：**沿每条从输入到输出的路径，累加所有 rolling 窗口，取最大值**。

```
示例因子: Div(Sub(close, Ma(close, 120)), TsStd(close, 60))

DAG 中的路径:
  路径 A: close → Ma(120) → Sub → Div     累加窗口 = 120
  路径 B: close → Sub → Div                累加窗口 = 0
  路径 C: close → TsStd(60) → Div          累加窗口 = 60

warmup_bars = max(120, 0, 60) = 120
```

更复杂的例子：

```
示例: TsRank(Div(Ma(close,30), TsStd(close,60)), 120)

路径 A: close → Ma(30) → Div → TsRank(120)     累加 = 30 + 120 = 150
路径 B: close → TsStd(60) → Div → TsRank(120)  累加 = 60 + 120 = 180

warmup_bars = max(150, 180) = 180
```

---

## 6. 系统启动的完整流程

```
系统启动
│
│  Phase 0: 构建因子图
│  ┌──────────────────────────────────────────────────┐
│  │ for factor_fn in [build_0001, build_0002, ...]:  │
│  │     graph = factor_fn()   // 构建 DAG            │
│  │     graph.compile()       // 分配状态 + 算 warmup │
│  │     engine.add_factor_to_all(graph)              │
│  └──────────────────────────────────────────────────┘
│
│  Phase 1: Warmup — 喂历史数据
│  ┌──────────────────────────────────────────────────┐
│  │ 从数据库/缓存加载每个标的最近 N 根历史 K 线       │
│  │ N = max(所有因子的 warmup_bars) ≈ 480            │
│  │                                                   │
│  │ for i in range(N):                                │
│  │     bar = historical_bars[symbol][i]              │
│  │     engine.push_bar(symbol, bar)                  │
│  │     // kernel 状态在逐步积累                      │
│  │     // 大部分因子还在输出 NaN                      │
│  │                                                   │
│  │ warmup 完成！所有 kernel 状态就绪                  │
│  └──────────────────────────────────────────────────┘
│
│  Phase 2: 稳态推理 — 实时处理
│  ┌──────────────────────────────────────────────────┐
│  │ while True:                                       │
│  │     wait_for_new_bar()        // 等 1 分钟        │
│  │     bar = get_latest_bar()    // 获取新 K 线      │
│  │     engine.push_bar(symbol, bar)  // 只算这 1 个  │
│  │     signals = engine.collect()    // 收集信号     │
│  │     // 耗时 ~100ms，tick 间隔 60,000ms            │
│  └──────────────────────────────────────────────────┘
```

---

## 7. 端到端数值走查

用一个极简因子 `Div(Sub(close, Ma(close, 3)), Ma(close, 3))` 和 5 根 K 线做完整走查。

### 7.1 输入数据

```
bar₀: close = 100
bar₁: close = 103
bar₂: close = 101
bar₃: close = 105
bar₄: close = 102
```

### 7.2 DAG 结构

```
node[0]: INPUT_CLOSE
node[1]: Ma(node[0], window=3)         ← RollingMeanKernel
node[2]: Sub(node[0], node[1])         ← 无状态
node[3]: Div(node[2], node[1])         ← 无状态
warmup_bars = 3
```

### 7.3 逐 bar 执行

**bar₀: close = 100**

```
values_[0] = 100
node[1] Ma: push(100) → count=1, sum=100    → NaN (count < 3)
values_[1] = NaN
node[2] Sub: 100 - NaN = NaN
values_[2] = NaN
node[3] Div: NaN / NaN = NaN
output = NaN → clean_factor → 0.0
```

**bar₁: close = 103**

```
values_[0] = 103
node[1] Ma: push(103) → count=2, sum=203    → NaN (count < 3)
values_[1] = NaN
node[2] Sub: 103 - NaN = NaN
values_[2] = NaN
node[3] Div: NaN / NaN = NaN
output = NaN → clean_factor → 0.0
```

**bar₂: close = 101** (warmup 完成！)

```
values_[0] = 101
node[1] Ma: push(101) → count=3, sum=304    → 304/3 = 101.333
values_[1] = 101.333
node[2] Sub: 101 - 101.333 = -0.333
values_[2] = -0.333
node[3] Div: -0.333 / 101.333 = -0.00329
output = -0.00329 ✅ 第一个有效输出
```

**bar₃: close = 105**

```
values_[0] = 105
node[1] Ma: push(105) → oldest=100, sum=304-100+105=309 → 309/3 = 103.0
values_[1] = 103.0
node[2] Sub: 105 - 103.0 = 2.0
values_[2] = 2.0
node[3] Div: 2.0 / 103.0 = 0.01942
output = 0.01942 ✅
```

**bar₄: close = 102**

```
values_[0] = 102
node[1] Ma: push(102) → oldest=103, sum=309-103+102=308 → 308/3 = 102.667
values_[1] = 102.667
node[2] Sub: 102 - 102.667 = -0.667
values_[2] = -0.667
node[3] Div: -0.667 / 102.667 = -0.00649
output = -0.00649 ✅
```

### 7.4 对照验证（批量模式）

```python
import pandas as pd
close = pd.Series([100, 103, 101, 105, 102], dtype=float)
ma3 = close.rolling(3).mean()
result = (close - ma3) / ma3

# 输出:
# 0         NaN
# 1         NaN
# 2   -0.003289   ← 匹配！
# 3    0.019417   ← 匹配！
# 4   -0.006494   ← 匹配！
```

**流式结果与批量结果完全一致。** 这就是为什么我们之前要做 502 个对齐测试的原因 — 确保每个 kernel 的增量算法和 pandas rolling 产出完全相同的结果。

---

## 8. 总结

```
流式因子计算的三个关键词:

  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
  │   有状态      │    │   增量更新    │    │   单点输入    │
  │              │    │              │    │              │
  │ Kernel 内部   │    │ 每次 push    │    │ 每 tick 只   │
  │ 维护滑动窗口  │    │ O(1) 更新    │    │ 喂 1 个值    │
  │ 的累积信息    │    │ 累积统计量    │    │ 产出 1 个值  │
  └──────────────┘    └──────────────┘    └──────────────┘
        │                    │                    │
        └────────────────────┼────────────────────┘
                             │
                             ▼
              ┌──────────────────────────┐
              │  批量模式:  4320 次计算   │
              │  流式模式:     1 次计算   │
              │  结果:       完全一致     │
              └──────────────────────────┘
```
