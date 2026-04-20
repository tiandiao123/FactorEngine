# FactorKernel(DAG) — 流式执行流程说明（2026-04-13）

## 0. 文档目的

说明 **单币种 `FactorKernel`** 在装载 **因子 DAG（有向无环图）** 后，**从初始化到每根 bar 的 `push` → 输出** 的完整逻辑。便于实现 C++ / Python kernel 模拟器 / 调度器时对齐语义。

**相关文档**

- `skydiscover/skygen/factorgen/cryptokbar/docs/TODO/06_cpp_kernel_translation_design.md` — DAG→kernel、增量算法、研产对齐。
- `FactorEngine/docs/20260412/factor_engine_cpp_runtime_plan.md` — 工程布局与迁移阶段。
- `FactorEngine/docs/20260412/native_cpp_engine_in_process_design.md` — 终局进程模型（可选）。

---

## 1. 术语

| 术语 | 含义 |
|------|------|
| **FactorKernel** | 单个因子在 **单个 symbol** 上的流式执行器；内部持有一棵 **DAG** 及每个节点的运行时对象。 |
| **DAG 节点** | 算子实例，如 `Ma(close,30)`、`Sub(n1,n2)`。 |
| **Stateful 节点** | 维护窗口 / 累加量，每 bar 需 **`push` + `output()`**。 |
| **Stateless 节点** | 仅依赖上游 **当前 tick** 的输出，无跨 bar 状态。 |
| **拓扑序** | 节点求值顺序：任意边的起点先于终点被更新。 |
| **`Bar`** | 单根 K 线结构体或定长向量（`ts_ms, open, high, low, close, vol, ...`），列定义以项目 **单一真相源** 为准。 |

---

## 2. 生命周期总览

```
构造 / from_json(DAG)
        │
        ├─ 拓扑排序节点列表
        ├─ 为每个 stateful 节点分配 kernel（MaKernel, TsStdKernel, …）
        ├─ 解析 input：特征名 → Bar 列索引；节点 id → node_outputs 下标
        └─ 记录 output_node_idx、warmup 策略
        │
        ▼
   [可选] reset()          # 新会话、换币、回放 seek
        │
        ▼
   对每根 incoming Bar:
        push(bar)  ─────►  见 §4
        │
        ▼
   ready() ? ──否──► 输出 NaN 或不写入 snapshot
        │
        是
        ▼
   返回 node_outputs[output_node] 作为本因子本 bar 的标量值
```

---

## 3. 初始化阶段（`from_json` 或等价构造）

### 3.1 输入：DAG IR

典型字段（与计划文档一致，实现可微调）：

- `nodes[]`：`{ id, op, inputs[], params{}, stateful }`
- `output_node`：最终输出的节点 id
- `input_features[]`：需要从 `Bar` 读取的列名集合（如 `close`, `volume`）

### 3.2 拓扑排序

对 DAG 做 **Kahn 或 DFS 后序逆序**，得到数组 `order[]`，保证：  
对每条边 `u → v`，**`index(u) < index(v)`**。

### 3.3 节点工厂

对每个节点：

- 查 **`op`** 与 **`params`**（如 `window=30`）。
- 若 **`stateful`**：构造对应 **`TsKernelNode`** 子类（或组合对象），例如 `MaKernel(30)`。
- 若 **非 stateful**：可不分配 heap 对象，求值时走 **内联函数** 或小型 `std::function`。

### 3.4 运行时缓冲区

- **`node_outputs`**：`std::vector<double>` 或 `float`，长度 = `nodes.size()`，存 **本 tick 每个节点标量输出**。
- **（可选）`node_ready`**：若各节点 `ready` 语义不同，可逐节点记录；常见做法是 **只在输出节点聚合 `ready()`**（见 §5）。

### 3.5 输入绑定

- **特征输入**：`"close"` → 从 `Bar` 按列枚举读取的 **getter**。
- **节点输入**：`"n1"` → 在 `nodes` 表里查到 `n1` 的 **数组下标** `idx`，求值时读 `node_outputs[idx]`。

---

## 4. 每根 bar：`push(bar)` 的逐步逻辑

以下假设 **单线程** 调用 `push`；多 symbol 时 **每个 symbol 一个 `FactorKernel` 实例**，可并行调用各自的 `push`。

### Step A — 预处理（可选）

- 从 `bar` 派生 **本 bar 用的一次量**：如 `ret = close / prev_close - 1` 若 DAG 需要 `ret` 且不在 `Bar` 内 —— **实现策略二选一**：  
  - 在 **feed 前** 由上游写好 `Bar.ret`；或  
  - 在 **Kernel 内** 维护 `prev_close` 标量。

### Step B — 按拓扑序 `for i in order`

对节点 `i`：

1. **解析输入列表** `inputs = [u1, u2, ...]`  
   - 若 `uk` 是 **特征名** → 得到标量 **`x_k = feature(bar, uk)`**。  
   - 若 `uk` 是 **节点 id** → **`x_k = node_outputs[idx(uk)]`**（**本 tick 内** 已算出的值）。

2. **分支：Stateless**

   - 例：`Sub(n1,n2)` → `node_outputs[i] = x1 - x2`（注意 **NaN 传播规则** 与研测一致）。

3. **分支：Stateful**

   - 单输入：`Ma(close,30)` → **`kernels[i]->push(x)`**，然后 **`node_outputs[i] = kernels[i]->output()`**。  
   - **未满窗**：`output()` 通常为 **`NaN`**（与 pandas `min_periods=window` 对齐时）。

4. **分支：多输入 Stateful**

   - 例：`Corr(x, y, w)`：实现可以是  
     - **一个 `CorrKernel`** 在一次调用里吃 **`(x,y)`**（推荐，避免两流不同步），或  
     - 两个底层 ring + 一个组合更新步 —— **对调度器仍是一个 DAG 节点**。

5. **组合算子**

   - **`TsZscore(x, w)`**：在实现上常 **展开** 为对 `x` 的 `Ma` + `TsStd` + 除法；对 **外部 DAG** 可以表现为 **一个节点**（内部子图）或 **三个显式节点**（便于复用 kernel）。

### Step C — 输出

- **`y = node_outputs[output_node_idx]`**  
- 若实现 **`FactorKernel::value()`** 与 **`push` 分离**：`push` 只更新状态，**读**时返回缓存的最后输出。

---

## 5. `ready()` 与 warmup

### 5.1 语义

- **`ready() == true`**：表示 **输出节点** 上的值 **对下游有意义**（已满最小样本、非歧义 NaN 策略等）。
- 常见定义：  
  **`ready() = output_node 及其所有 transitive 依赖的 stateful 节点均已满足各自窗口`**。

### 5.2 与研测对齐

- 研究端：`rolling(...).mean()` 在 pandas 默认下前 `window-1` 行为 NaN —— **流式 `output()` 应对齐同一规则**。
- **`warmup_bars`**：可在 DAG 编译期 **静态计算** 最长依赖链（见 06 文档），写入 JSON 供监控与断言。

---

## 6. 多币种与截面后处理（不在单 Kernel 内）

| 阶段 | 谁负责 | 说明 |
|------|--------|------|
| **单币时序** | **每个 symbol 一个 `FactorKernel`** | 只消费该币的 `Bar` 序列。 |
| **截面** | **`CrossSectionRuntime` 或 Python 薄层** | 收集 `snapshot[symbol] = kernel.output()`，做 `zscore` / `rank` / `clean`。 |

**原则**：`FactorKernel` **不读其他 symbol**；截面 **天然不是 DAG 里的一类 Ts 节点**（除非显式建模「全市场节点」，一般不这么做）。

---

## 7. 示例 1 — `Ma` + `Sub` + `TsZscore`（与 06 文档一致）

**DAG**

1. `n1 = Ma(close, 30)`  
2. `n2 = Ma(close, 120)`  
3. `n3 = Sub(n1, n2)`  
4. `n4 = TsZscore(n3, 60)` → **输出**

**Bar #t 到达时**

| 顺序 | 节点 | 动作 | `node_outputs[·]` |
|------|------|------|---------------------|
| 1 | n1 | `Ma30.push(close_t)` | `o1`（可能 NaN） |
| 2 | n2 | `Ma120.push(close_t)` | `o2` |
| 3 | n3 | `o3 = o1 - o2` | `o3` |
| 4 | n4 | `TsZ60.push(o3)` | `o4`（因子 raw 值） |

**`ready()`**：当 n4 内部窗口满 **60 个有效的 `o3` 样本**（且 n1/n2 已各自满窗，否则 `o3` 为 NaN 导致链式 NaN）时为真；具体 **是否与 pandas 完全一致** 需在 **对齐测试** 中固化。

---

## 8. 示例 2 — `TsRank` + `Corr`（接近 `alpha_pool/factors/3`）

**DAG（单币，概念上）**

1. `r1 = TsRank(close, 30)`  
2. `r2 = TsRank(volume_quote, 30)`  ← `volume_quote` 映射到 `Bar` 的 `volCcyQuote` 列  
3. `c = Corr(r1, r2, 120)` → **输出**

**每 bar**

- 先 **`TsRankKernel.push(close_t)`** → `r1_t`  
- 再 **`TsRankKernel.push(volq_t)`** → `r2_t`  
- 最后 **`CorrKernel.push(r1_t, r2_t)`**（或内部维护两组 ring）→ `c_t`

**注意**：`Corr` 的输入是 **两个随时间变化的序列**，**必须同一 bar 对齐 push**，不能先算完所有 `r1` 再算 `r2` 跨 bar 混用。

---

## 9. `reset()` 与回放 / 换币

| 场景 | 行为 |
|------|------|
| **回放 seek 到 t0** | 所有 stateful kernel **`reset()`**，从 t0 重新 `push`。 |
| **换 symbol** | 换 **新的 `FactorKernel` 实例** 或 **`reset()` 后复用**（推荐新实例，避免状态泄漏）。 |
| **热更新 DAG** | 原子替换 **`std::shared_ptr<const FactorKernel>`**；旧实例丢弃。 |

---

## 10. 与外层调度（`Scheduler` / `multi_symbol_runtime`）的关系

- **`FactorKernel`**：**不关心** wall-clock、不关心其他币；只回答 **「给定这根 bar，我现在的输出是多少」**。  
- **外层**：决定 **何时** 调 `push`（每根 merged bar / 每 eval tick）、**按 symbol 分片** 并行、以及 **截面** 与 **写入 `FactorSnapshot`**。

这样 **内核逻辑** 与 **系统调度** 解耦，便于单测。

---

## 11. 实现检查清单（评审用）

- [ ] 拓扑序在 **含 `TsZscore` 展开** 后仍正确  
- [ ] 每个 **Stateful** 节点 **独立状态**（两个 `Ma(close,30)` 用于不同分支 → 两个实例）  
- [ ] **双输入**算子 **同一 bar 对齐**  
- [ ] **`ready()` / NaN** 与 pandas / `kernel_sim` 对齐有 golden  
- [ ] **`Bar` 列缺失** 时：编译期拒收或运行期明确 NaN 策略  
- [ ] **截面不在 Kernel 内**（除非刻意扩展架构）

---

## 12. 小结

**`FactorKernel(DAG)` 的执行逻辑**可以概括为：

1. **初始化**：DAG 拓扑排序 + 实例化 stateful kernels + 绑定输入。  
2. **每 bar**：按拓扑序，**先取输入（特征或已算节点）→ stateless 直接算 / stateful 先 `push` 再 `output`**，写入 `node_outputs`。  
3. **输出**：取 **`output_node`**；用 **`ready()`** 控制是否对外可见。  
4. **多币与截面**：**外层**多实例 + 后处理，不塞进单个 Kernel。

---

*实现命名（`FactorKernel` / `TsKernelNode`）可与仓库最终 C++ 命名空间一致；本文描述的是 **语义契约**。*
