# FactorEngine C++ Runtime — 与 SkyGen Alpha 池对齐的实施计划（2026-04-12）

## 0. 文档目的与读者

本文在 **FactorEngine** 仓库内描述：如何把 **SkyGen / CryptoKbar** 已挖出的因子（`alpha_pool/factors/*/code.py` + `factorlib/operators` 白名单算子）落到 **实盘/仿真引擎** 的 **C++ 热路径** 上，并与现有 **Python 原型**（`Engine` → `FactorRuntime` / `Scheduler`）平滑衔接。

**前置阅读（skydiscover 侧）**

- `skygen/factorgen/cryptokbar/docs/TODO/06_cpp_kernel_translation_design.md` — 算子 DAG → 有状态 kernel、研产对齐、Python kernel 模拟器思路。

**前置阅读（FactorEngine 侧）**

- `docs/20260408/cpp_runtime_integration.md` — C++ / Python 职责切分、迁移节奏。

本文 **不重复** 06 文档中所有算子数学细节，而是把它们 **映射到 FactorEngine 的工程边界、数据契约、目录规划与交付顺序**。

---

## 1. 现状盘点（你已有的是什么）

### 1.1 因子资产：`alpha_pool/factors/*/code.py`

典型形态（研究/回测）：

- 入口：`create_indicator(timestamp: int, kbar: pd.DataFrame) -> pd.Series`
- `kbar`：多币种长表，含 `time, coin, open, high, low, close, volume, ...`（与 `CryptoKbarLoader` 一致）。
- 计算：**先 `pivot_table` 成面板**（`index=time, columns=coin`），再在 **每列（单币时间序列）** 上调用 `Ma / TsRank / Corr / Ema / ...`，最后 **`iloc[-1]`** 取最后一根 bar 的截面，再 `clean_factor(cross_sectional_zscore(...))`。

**含义拆成两层：**

| 层次 | 研究端（当前 Python） | 生产 / FactorEngine 目标 |
|------|------------------------|----------------------------|
| **A. 单币时序** | 对 `close[coin]` 等一整段 Series 做 rolling | **每根新 bar 增量更新** → 与 06 文档「kernel」一致 |
| **B. 截面** | `cross_sectional_zscore` 对多币最后一行 | **在固定 tick** 上对所有币向量做一次 zscore（可仍在 Python 或后续 SIMD） |

C++ runtime **第一期应主攻 A**；B 可先用 **Python 薄层** 或 **固定小核**（纯向量运算）实现，避免第一期把整棵 DAG 跨进程都塞进 C++。

### 1.2 算子白名单：`factorlib/registry.py` + `factorlib/operators.py`

- **`TS_OPS_INFO`**：`Ma, Ema, TsStd, TsRank, Corr, Cov, TsZscore, Delay, TsDiff, ...` — **天然可映射为 DAG 节点**（有状态 / 无状态分类见 06 文档 §2–3）。
- **`AGG_OPS_INFO`**：`vol_upto, cumret_upto, vwap_upto, volume_herfindahl, ...` — **整窗聚合**，在流式场景需 **展开为等价增量子图**（06 文档 §7、§10）。
- **`ALLOWED_OPS`**：`set(AGG_OPS_INFO) | set(TS_OPS_INFO) | UTIL_OPS` — **可作为「可翻译子集」的权威列表**。

### 1.3 FactorEngine 当前 Python 原型

- **数据**：`Engine.get_data()` 等 → `dict[symbol, ndarray(N, C)]`（`C` 以 `dataflow/livetrading/events.py` 中 `BAR_NUM_FIELDS` 为准；仿真路径已可能扩展到 volCcy 等列）。
- **调度**：`factorengine/scheduler/` — `FactorSpec` 绑定 **Python `compute_fn(window: ndarray) -> float`**，与 SkyGen 因子 **不是同一表达**（当前是示例因子如 bar momentum、book imbalance）。

**缺口**：把 **`FactorSpec` + 手写 compute_fn`** 演进为 **`FactorSpec` + DAG JSON / kernel id + 参数`**, 或由 **C++ 暴露的统一 `evaluate(tick)`**。

---

## 2. 目标架构（分层）

```
┌─────────────────────────────────────────────────────────────┐
│  Orchestration (可保留 Python)                               │
│  Engine.start/stop, 配置, 监控, 回放, 截面后处理              │
└───────────────────────────┬─────────────────────────────────┘
                            │ 每 tick: bar 快照指针或拷贝
                            v
┌─────────────────────────────────────────────────────────────┐
│  C++ Factor Runtime (目标热路径)                              │
│  - 每因子 / 每因子组: FactorKernel(DAG)                      │
│  - 每 symbol: push_bar(Bar) → scalar（单币时序输出）          │
│  - optional: batch 截面层 zscore / rank                      │
└───────────────────────────┬─────────────────────────────────┘
                            │ FactorSnapshot 等价结构
                            v
┌─────────────────────────────────────────────────────────────┐
│  Downstream: 风控 / 下单 / 日志 / Python 绑定               │
└─────────────────────────────────────────────────────────────┘
```

**原则**（与 `cpp_runtime_integration.md` 一致）：**C++ 只做热路径**；接入、配置、非延迟敏感逻辑可留在 Python。

---

## 3. 数据契约：FactorEngine `Bar` ↔ CryptoKbar 列

### 3.1 列对齐

SkyGen 因子面板常用列：`close, high, low, open, volume, volCcyQuote`（及 loader 里的 `ret` 等）。

FactorEngine bar 行向量列顺序 **必须以单一真相源为准**（当前为 `dataflow/.../events.py` 的 `BAR_COLUMNS`）。

**实施要求**

1. 在文档/代码评审中维护一张 **「列索引表」**，明确 `Bar` struct 与 `ndarray` 下标。
2. 若生产只用到子集（例如 6 列），**DAG 的 `input_features` 只能引用存在的列**；需要在 **因子入 C++ 前** 做静态校验。
3. **`ret` 的处理**：研究端常在 loader 里算好；流式端可在 **push_bar 时** 用上一根 close 计算 `ret` 写入 `Bar`，或单独作为 kernel 输入特征。

### 3.2 时间戳

- 统一 **`int64_t ts_ms`**，与 OKX / NAS CSV 对齐。
- `FactorSnapshot.ts_eval_ms` 与 bar `ts` 的关系（用 bar close time 还是 exchange event time）应在配置层 **显式规定**。

---

## 4. 从 Alpha 代码到可执行 DAG（编译管线）

### 4.1 输入源优先级

| 优先级 | 来源 | 用途 |
|--------|------|------|
| P0 | **AST 解析** `create_indicator` 体中对 `operators` 的调用链 | 自动生成 `FactorDAG`（06 §4 路线 A） |
| P1 | **LLM / 元数据附带 DAG JSON**（若以后在 pool `metadata` 存） | 校验、回退、版本对齐（06 路线 B） |

**约束**（与 06 §8 一致，需写进校验器）：

- 仅 `ALLOWED_OPS` 内算子；
- 窗口参数 **编译期常量**；
- 禁止动态改变计算图结构的 **数据依赖分支**（或先不支持）；
- 因子逻辑 **不跨币读取**（截面运算在 `iloc[-1]` 之后 —— 拆到 **Kernel 后处理**）。

### 4.2 典型因子模式的 DAG 化

**例 A：`alpha_pool/factors/3/code.py`**

- 面板：`TsRank(close,30)`, `TsRank(volume,30)`, `Corr(close_rank, vol_rank, 120)` → `iloc[-1]` → 截面 zscore。
- DAG 核心：`TsRank`×2 → `Corr` → **输出为每币标量序列的最后值**；截面 zscore **不是 DAG 叶节点**，标记为 `post:cross_sectional_zscore`。

**例 B：`alpha_pool/factors/53/code.py`**

- 多步 `Ma/TsRank/TsMax/TsMin/Ema/Mul/Sub/Div` 组合，最大窗口需静态推导 **warmup_bars**（06 §4.3）。

### 4.3 IR 形态（建议）

与 06 文档 §4.2 对齐，建议最小 JSON Schema：

- `factor_id`, `version`, `input_features[]`
- `nodes[]`: `{ id, op, inputs[], params{}, stateful }`
- `output_node`
- `postprocessors[]`: e.g. `{ "op": "cross_sectional_zscore", "stage": "last_bar" }`
- `warmup_bars`, `state_bytes_estimate`（可选，供部署校验）

**产出物路径（建议）**

- 构建时：`alpha_pool/factors/<id>/dag.json`（与 `code.py` 同目录或 `artifacts/`）
- 运行时：C++ `FactorKernel::from_json(path)` 或嵌入二进制资源。

---

## 5. C++ 模块设计（FactorEngine 仓库内建议布局）

> 以下为 **建议目录**，实际可放在 `cpp/` 或独立 repo，由绑定方式决定。

```
FactorEngine/
  cpp/
    include/fe/
      bar.hpp              # Bar 布局, 列枚举
      dag_ir.hpp           # DagNode, FactorDAG
      kernel_node.hpp      # TsKernelNode 接口
      factor_kernel.hpp    # 拓扑求值 + push_bar
      runtime.hpp          # 多 symbol × 多因子调度
    src/
      kernels/
        ma.cpp ts_std.cpp ts_rank.cpp ema.cpp ...
        corr.cpp           # 双通道滚动
      runtime/
        factor_kernel.cpp
        multi_symbol_runtime.cpp
    bindings/
      pybind11_module.cpp  # 可选：先 Python 驱动 C++ kernel
    tests/
      test_ma_kernel.cpp
      ...
```

**与 Python 的两种集成路线**

| 路线 | 做法 | 优点 | 缺点 |
|------|------|------|------|
| **A. pybind11** | `CppFactorRuntime.evaluate(...)` 从 Python Scheduler 调用 | 渐进迁移、易对齐测试 | GIL、拷贝 ndarray |
| **B. 进程内纯 C++ Engine** | 未来 `factor_engine` 二进制 + IPC 给 Python | 延迟最优 | 工程量大 |

**推荐**：**Phase 1–3 走 pybind11**；对齐通过后再考虑 B。

---

## 6. 算子覆盖矩阵（对接 `registry.py`）

### 6.1 TS 算子（高优先级实现）

| 算子 | Kernel 难度 | 备注 |
|------|--------------|------|
| Ma, TsSum, Ema | L1 | 06 §3 |
| TsMin, TsMax, TsMinMaxDiff, … | L1 | 单调队列 / 组合 |
| TsStd, TsVari, TsZscore | L2 | Welford 或展开为 Ma+std |
| Delay, TsDiff, TsPct | L2 | ring buffer |
| Corr, Cov | L2 | 维护 Σx, Σy, Σxy… |
| TsRank, TsMed, TsMad | L3–L4 | 注意复杂度与近似策略 |
| TsSkew, TsKurt, Autocorr, TsIr, TsWMA | L2–L3 | 按 06 表分批 |

### 6.2 AGG 算子（次优先级 / 需展开）

- 必须在 IR 层提供 **`to_incremental_dag()`** 或等价表（06 §7、§10），否则无法进入流式 kernel。
- **入池因子扫描**：对 `alpha_pool` 做静态统计 —— **哪些因子引用 AGG_OPS**；优先为高频 agg 写展开。

### 6.3 截面工具

- `cross_sectional_zscore`, `cross_sectional_rank`, `clean_factor`：**第一期**可在 **Python** 对 C++ 输出的「最后一行 per coin 向量」执行；与研测完全一致后再考虑 C++ 向量化。

---

## 7. 研产对齐（必须做的测试流水线）

完全继承 **06 §6** 思路，在 **FactorEngine CI / 本地脚本** 中增加：

1. **Golden**：对同一 `coin` 的分钟序列，**Python 批量**（现有 `create_indicator` 截断到前缀 i）vs **Python kernel 模拟器**（逐 bar push）— 应先做到 **完全一致**。
2. **C++ vs Python kernel 模拟器**：通过 pybind 或独立 gtest 生成同一序列对比。
3. **回归因子集**：从 `alpha_pool` 抽 **N 个代表性因子**（含 `Corr+TsRank`、`Ema` 链、高窗口等）作为 **对齐回归套件**。

**通过标准**（可配置）：`max_abs_diff < 1e-5`（float64）或文档化容差；**NaN 对齐策略**与 `min_periods` 一致。

---

## 8. 与现有 `FactorRuntime` 的演进关系

### 8.1 当前

```text
FactorSpec(name, source, window, compute_fn)  # compute_fn 手写 Python
```

### 8.2 目标（中间态）

```text
FactorSpec(name, source, dag_path | dag_json, postprocess="none"|"xs_zscore")
```

- `evaluate()`：对每个 `symbol`，`kernel.push_bar(row)`；收集标量后在 Python 或 C++ 做截面。
- **向后兼容**：若 `dag_path` 为空，则仍调用 `compute_fn`（便于渐进迁移）。

### 8.3 目标（终态）

- `Scheduler` / `FactorRuntime` **C++ 实现**，Python 仅 **装配配置 + 取快照**。

---

## 9. 分阶段路线图（可执行）

### Phase 0 — 契约冻结（1 周内）

- [ ] 冻结 `Bar` 列布局与 `FactorSnapshot` JSON 字段。
- [ ] 列出 `ALLOWED_OPS` 与 **第一期必须支持的子集**（建议先覆盖 alpha_pool 中出现频率 Top-K 算子）。
- [ ] 脚本：扫描 `alpha_pool/*/code.py` 的 **import 算子频次** + **AGG 使用率**。

### Phase 1 — Python kernel 模拟器 + DAG IR（skydiscover 或共享子仓）

- [ ] 在 `factorlib/` 下按 06 §12 增加 `kernel_sim/`（若你希望单仓维护，也可只在 skydiscover 做，FactorEngine 子模块 git submodule —— **选型待定**）。
- [ ] `dag/ir.py` + `dag/parser.py`（白名单 AST）。
- [ ] **对齐测试**：批量 vs kernel_sim（**不接 C++**）。

### Phase 2 — C++ kernel 子集 + 单测（FactorEngine `cpp/`）

- [ ] `TsKernelNode` + `MaKernel`, `TsStdKernel`, `EmaKernel`, `DelayKernel`…
- [ ] `FactorKernel::from_json` 最小实现（仅支持 **线性拓扑 + 单输出**）。
- [ ] gtest 覆盖与 Python kernel_sim 对拍。

### Phase 3 — pybind11 绑定 + Python Scheduler 联调

- [ ] 暴露 `PyFactorKernel.push_bar` / `output` / `ready`。
- [ ] 在 `FactorRuntime.evaluate` 中 **可选路径**：DAG 因子走 C++，其余走 Python。
- [ ] **端到端**：`Engine`（simulation 或 live）→ C++ kernel → `FactorSnapshot`。

### Phase 4 — AGG 展开 + 全算子覆盖

- [ ] 为 `AGG_OPS_INFO` 高频算子写展开规则。
- [ ] 扩展 DAG 校验与 **「不可翻译因子」** 拒绝策略（回传 research-only 标记）。

### Phase 5 — 性能与部署

- [ ] 多线程：每 symbol 一队列或 shard by symbol group。
- [ ] 内存预算：按 06 §11 公式配置 `max_factors` / `lookback`。
- [ ] 观测：每 tick `duration_ms` P99 压测（对齐当前 Python 原型日志）。

---

## 10. 风险与决策点

| 风险 | 缓解 |
|------|------|
| pandas rolling 与增量 kernel 数值不一致 | 严格按 06 §6 + 双轨 golden；Ema、TsRank 等单独文档化公式 |
| 因子使用 `pd.DataFrame(1.0, ...)` 广播 | AST 归一化为常量节点或禁止模式 |
| AGG 算子展开工作量大 | 分期；研究端允许「仅 batch 可算」标记 |
| Bar 列数与 SkyGen 不一致 | 统一 `Bar` schema；不足列在适配层补或拒收 DAG |

---

## 11. 与外部文档的索引关系

| 文档 | 关系 |
|------|------|
| `skydiscover/.../06_cpp_kernel_translation_design.md` | **数学与 kernel 设计母版** |
| `FactorEngine/docs/20260408/cpp_runtime_integration.md` | **谁留在 Python、谁进 C++** |
| 本文 | **FactorEngine 工程落地顺序 + 与 alpha_pool/registry 对齐清单** |
| `docs/20260412/native_cpp_engine_in_process_design.md` | **终局：进程内纯 C++ Engine（路线 B）** |

---

## 12. 建议的下一步（本周可开任务）

1. **统计脚本**：`alpha_pool` 算子频次 + `AGG` 出现列表（输出 Markdown 表，附在 `docs/20260412/` 下作为数据附录亦可）。
2. **选 3 个因子**（如 id 3, 53 + 一个纯 Ma/TsZscore）作为 **Phase 1–3 的 golden 样板**。
3. 在 `FactorEngine` 开 `cpp/` 骨架 + `cmake` + 空 `FactorKernel::from_json`，CI 编译通过即可。

---

*本文随实现推进应更新版本号与勾选状态；实现细节以代码与 `FactorDAG` schema 为准。*
