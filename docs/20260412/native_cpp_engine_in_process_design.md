# 进程内纯 C++ Engine（路线 B）— 长期架构设计（2026-04-12）

## 0. 文档定位

- **路线 A（渐进）**：Python `Engine` / `Scheduler` + **pybind11** 调 C++ `FactorKernel`（见同目录 `factor_engine_cpp_runtime_plan.md`）。
- **路线 B（本文）**：**单进程、热路径无 Python、无 pybind 参与 eval** —— 面向 **延迟稳定、部署简单、长期维护** 的终局形态。

本文回答：**若选 B，`Engine` 在 C++ 里长什么样、与数据流/调度/因子如何拼装、Python 退到哪一层、与当前 FactorEngine 仓库如何演进。**

---

## 1. 「纯 C++ Engine」的精确定义

在路线 B 下，下列能力 **必须落在同一 C++ 进程、同一调度域内**（可多线程，但 **无 Python runtime 参与 tick 路径**）：

| 组件 | 是否纳入 C++ Engine | 说明 |
|------|---------------------|------|
| 行情接入（WS / 回放文件 / SHM 消费者） | **是** | 二选一或组合：自研 WS 客户端，或只消费 **C/C++ 写的 feeder** 写入的 lock-free ring |
| Bar / Trade / Book **环形缓存** | **是** | 与当前 `BarCache` 语义等价；列布局与 `Bar` struct 单一真相源 |
| **调度器**（固定间隔 tick / 事件驱动） | **是** | 对应 `Scheduler` + `FactorRuntime` 的终局 |
| **因子求值**（DAG / kernel） | **是** | 见 `06_cpp_kernel_translation_design.md` |
| 截面后处理（zscore / rank / clean） | **建议是** | 若仍放 Python 则 **不是**严格 B；长期应在 C++ 内向量化 |
| 配置加载 | **建议是** | `yaml`/`json` + 校验；避免 tick 路径读盘 |
| 日志 / 指标 / 慢查询 | **是** | `spdlog`、可选 Prometheus；与热路径解耦 |

**明确排除（在严格 B 下）**

- 在 **`on_bar` / `evaluate_tick`** 内调用 **libpython**、**pybind11::object**、**嵌入解释器**。
- **Numba / NumPy** 作为因子执行后端。

**Python 仍可存在的位置（进程外或非热路径）**

- **研发**：因子代码 → AST → `dag.json` 的 **离线编译管线**（仓库仍在 `skydiscover/skygen`）。
- **CI**：对齐测试、回放对比、生成 golden。
- **运维**：启动脚本 **仅 `exec` C++ 二进制** + 传参；或旁路容器里跑监控，**不参与**引擎进程内的 eval。

---

## 2. 总体架构（单进程）

```
                    ┌──────────────────────────────────────┐
                    │           fe::Engine                  │
                    │  lifecycle: init / run / stop         │
                    └───────────────┬──────────────────────┘
                                    │
     ┌──────────────────────────────┼──────────────────────────────┐
     │                              │                              │
     v                              v                              v
┌─────────────┐              ┌───────────────┐              ┌─────────────────┐
│ MarketData  │              │ MarketState  │              │ EvaluationCore  │
│ Pipeline    │──writes──▶   │ (per symbol) │◀──reads──    │ Scheduler+      │
│ WS / Replay │              │ RingBuffers  │              │ FactorRuntime   │
└─────────────┘              └───────────────┘              └────────┬────────┘
                                                                    │
                                                                    v
                                                          FactorSnapshot
                                                          (flat buffer /
                                                           shared struct)
```

### 2.1 线程模型（推荐默认）

| 线程 / 执行器 | 职责 |
|---------------|------|
| **Net I/O**（如 `asio::io_context` 单线程或多 `poll_one` worker） | WS 收包、解码、**仅写** per-symbol 的 **单写者 ring** 或 **无锁队列** |
| **Bar 聚合**（若协议仍是 1s→Ns） | 与现 `BarAggregator` 等价；可在 Net 线程或独立 **serialize** 线程完成 |
| **Eval 线程池** | 每个 **tick**：从 `MarketState` **快照指针或双缓冲** 读一致视图，跑 `FactorRuntime` |
| **可选 Admin 线程** | 配置热更、HTTP 健康检查（**不**与 eval 共享锁） |

**关键**：tick 上读到的 bar 窗口必须是 **原子一致的**（双缓冲 / seqlock / 每 tick 拷贝一份小窗口到 scratch —— 按延迟预算选）。

### 2.2 与「Python Engine」的语义对齐

当前 Python：`Engine` → `DataflowManager` / `SimDataflowManager` → `get_bar_snapshot()`。

C++：`fe::Engine` 暴露等价能力（**不一定**同名 API）：

- `subscribe_symbols(std::vector<std::string>)`
- `start()` / `stop()`
- 内部 **`MarketState::bar_matrix(symbol)`** 或 **只读 `std::span<const Bar>`** 最近 N 根。

对上游策略/风控：输出 **`FactorSnapshot`**（`tick_id`, `ts_eval_ms`, `flat_values` 或 `symbol × factor` 稠密矩阵）。

---

## 3. 目录与构建（建议）

在 FactorEngine 仓库内长期可维护的一种布局：

```
FactorEngine/
  native/                          # 或 cpp/engine/，名称团队自定
    CMakeLists.txt
    include/fe/
      types.hpp                    # Bar, SymbolId, TickId
      market_state.hpp             # 环形缓存、双缓冲视图
      market_pipeline.hpp          # WS / replay 驱动接口
      scheduler.hpp
      factor_runtime.hpp           # DAG kernel 调度
      engine.hpp
    src/
      engine.cpp
      io/                          # asio WS, reconnect, subscribe batch
      agg/                         # 1s→Ns 若保留
      factor/                      # from_json, kernels...
    apps/
      fe_run.cpp                   # main(): 解析 CLI + yaml，启动 Engine
    tests/
      ...
  docs/20260412/
    factor_engine_cpp_runtime_plan.md   # 路线 A + 共有因子设计
    native_cpp_engine_in_process_design.md  # 本文
```

**依赖选型（需在 POC 阶段冻结）**

- **网络**：`Boost.Asio` 或 **standalone Asio** + `Beast` WebSocket（OKX WSS）。
- **配置**：`yaml-cpp` + `simdjson`（若 DAG 用 JSON）。
- **日志**：`spdlog`。
- **单测**：`Catch2` / `GoogleTest`。

---

## 4. 行情接入：两条子路线

### 4.1 子路线 B1 — **全栈 C++ 接入**（真·单进程）

- 在 **`fe::io::OkxWsClient`** 内完成：连接、订阅分片、心跳、重连、candle1s 解析。
- **优点**：无跨语言、无第二进程；延迟路径最短。
- **缺点**：需 **重写** 现 Python `livetrading/okx/*` 行为；OKX 协议变更要跟进。

### 4.2 子路线 B2 — **C++ Engine + C++ Feeder 双模块（仍单进程）**

- 动态链接 **`libfe_io.so`** 与 **`libfe_core.so`** 同进程；或静态链接合一 **可执行文件**。
- 适用于：先把 **eval** 做稳，**IO** 仍用另一小组件但 **同为 C++**。

**不推荐（若坚持名实相符的 B）**：Python feeder + SHM 写入、C++ 只读 —— 那是 **混合进程模型**，不是「纯 C++ Engine」，但可作为 **迁移跳板**（文档 `cpp_runtime_integration.md` 已讨论）。

---

## 5. 因子与配置如何进入 C++（无 Python）

### 5.1 启动参数与文件

示例：

```bash
./fe_run --config engine.yaml \
         --factors-dir /path/to/alpha_pool_dags/ \
         --symbols BTC-USDT-SWAP,ETH-USDT-SWAP
```

- **`engine.yaml`**：`tick_interval_ms`、`bar_window`、`max_workers`、`log_level`、OKX keys（或指向 secrets 文件路径）。
- **`alpha_pool_dags/`**：每个因子一个 **`factor_<id>.json`**（`FactorDAG` IR），由 **离线 Python 管线生成**（不在运行时依赖 Python）。

### 5.2 运行时加载

1. 启动时 **`FactorRegistry::load_from_directory`**：解析 JSON → 构建 `std::vector<std::unique_ptr<FactorKernel>>`。
2. **校验**：`warmup_bars`、所需列 ⊆ `Bar` schema、算子版本与 **编译进二进制的算子库版本** 匹配（版本号写在 JSON 头）。

### 5.3 热更新（长期）

- **因子**：`SIGHUP` 或控制面 HTTP **reload registry**（原子替换 `shared_ptr<const FactorRegistry>`）；下一 tick 切换。
- **符号列表**：需与交易所 **订阅表** 同步策略（全量 / 白名单）。

---

## 6. `FactorRuntime` / `Scheduler` 在 C++ 中的职责

与 `cpp_runtime_integration.md` §4 对齐，但 **全部 C++ 化**：

1. **`Scheduler`**：维护 `tick_id`、`next_eval_ts`；可选 **与 wall-clock 对齐** 或 **跟 last bar ts** 对齐。
2. **`FactorRuntime`**：
   - 输入：`MarketState` 只读视图 + 当前 `symbol` 集合；
   - 对每个 `(symbol, factor)` 调 `kernel.push` 或 **批量** `evaluate_symbol(symbol)`（取决于 DAG 是否共享跨因子中间量）。
3. **输出**：`FactorSnapshot` —— 建议 **SoA 布局**（`factor_major` 或 `symbol_major`）便于 BLAS/后续 GPU；或固定 **flat `std::vector<double>` + 元数据表** 便于 IPC 给下游 C++ 策略库。

**Worker pool**：按 **symbol 分片** 或 **factor 分片**；避免 **单 symbol 多锁竞争**（每 symbol 一队列或 shard-local state）。

---

## 7. 观测、调试与回放（无 Python 前提下）

| 需求 | 做法 |
|------|------|
| 线上排障 | **结构化日志** + **可选 ring 文件** dump 最近 N tick 的 bar 摘要 |
| 与研测对齐 | **离线 `fe_replay`**：读 NAS/CSV **同一 C++ 代码路径** push bar，对比 golden |
| 性能剖析 | `tracy` / `perf`；tick 内 **分段计时**（md → agg → eval） |
| 配置错误 | 启动期 **fail-fast**；运行期 schema 不匹配 → **单因子 disable** + 告警 |

---

## 8. 从当前仓库迁到 B 的阶段建议

| 阶段 | 目标 | 与现 Python 关系 |
|------|------|------------------|
| **B0** | 冻结 `Bar`、`FactorSnapshot`、DAG JSON schema | Python 仍为主 |
| **B1** | `fe_run` + **回放模式** 只跑 **MarketState + 单因子 kernel**（无 WS） | 与 `simulation` 语义对拍 |
| **B2** | C++ **Scheduler + 全因子** + 回放 | Python 只做 golden 对比 |
| **B3** | C++ **OKX WS** 接入 + 与现 Python livetrading **影子并行** 对拍 | 灰度 |
| **B4** | 生产切换；Python Engine **标记 deprecated** | 文档与 CI 切换 |

**影子并行**：同一机器上 Python 与 C++ **各订一套 WS**（注意限流）或 **共享 PCAP/录制流** 对比 bar 与因子输出。

---

## 9. 风险与决策记录

| 风险 | 缓解 |
|------|------|
| OKX WS 在 C++ 重写成本高 | B0–B2 用回放验证；B3 专人维护 `io/`；保留协议层单测 |
| 团队调试 C++ 慢于 Python | 投资 **回放 + golden**；强制 **每因子 DAG 可单测** |
| 因子迭代频繁 | **DAG JSON** 与二进制 **解耦**；算子 **向前兼容** 版本策略 |
| 截面/清洗与研测不一致 | C++ 实现 **与 Python `clean_factor` 同规格文档** + 双轨测试 |
| 二进制体积与链接时间 | 算子 **头文件 + 显式模板实例化** 或 **分库 lazy link** |

---

## 10. 与路线 A 的关系（建议的长期策略）

1. **短期**：路线 **A** 降低风险 —— C++ 先只做 **kernel**，Python 管 IO 与调度。
2. **中期**：把 **调度 + 截面** 迁入 C++，Python 变薄。
3. **长期**：路线 **B** —— **单二进制** 上生产；Python 退至 **离线工具链**。

**不建议跳过 A 直接全量 B**，除非团队已有成熟 C++ 行情栈与运维经验。

---

## 11. 参考文档索引

| 文档 | 内容 |
|------|------|
| `docs/20260412/factor_engine_cpp_runtime_plan.md` | DAG、算子矩阵、pybind 渐进路径 |
| `docs/20260408/cpp_runtime_integration.md` | C++/Python 职责、迁移哲学 |
| `skydiscover/.../06_cpp_kernel_translation_design.md` | kernel 数学与对齐 |

---

## 12. 小结（执行层一句话）

**路线 B = 一个（或一组静态链接的）C++ 二进制**：内嵌 **行情 → 状态机 → 调度 → DAG 因子 → 快照输出**；**Python 不参与进程内 eval**；因子以 **离线生成的 DAG JSON** 注入；用 **回放 + 影子并行** 控迁移风险。

---

*本文随 POC 进展应更新：至少补充「选定 Asio/Beast 版本」「Bar 列最终定稿」「fe_run CLI 真实 flag」三节。*
