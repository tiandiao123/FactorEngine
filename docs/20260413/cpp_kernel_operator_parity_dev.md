# C++ Kernel 与 SkyGen 算子对齐 — 开发文档（Phase 1）（2026-04-13）

## 0. 目标与范围

**目标**：在 FactorEngine 侧实现 **C++ 增量算子（kernel）** 与 **`skydiscover/skygen/factorgen/cryptokbar/factorlib/ops/ts_ops.py`**（经 `factorlib/operators.py` 导出）的 **数值与边界语义一致**，作为后续 **`FactorKernel(DAG)`** 与 **按币多线程调度** 的基础。

**本期范围（Phase 1）**

- **对齐**：`ts_ops.py` 中的 **时序 / 元素级 / 双序列滚动** 算子（见 §3 清单）。
- **暂不纳入本期对齐主目标**：`agg_ops.py`（`kbar → Series` 整窗聚合）—— 在 **Phase 2** 用「增量展开 + 独立 golden」处理（见 §9）。

**参考文档**

- `FactorEngine/docs/20260413/factor_kernel_dag_execution.md` — DAG / `push` 语义。
- `skydiscover/skygen/factorgen/cryptokbar/docs/TODO/06_cpp_kernel_translation_design.md` — 增量算法分级。

---

## 1. Python 侧「单一真相源」路径

| 用途 | 路径 |
|------|------|
| 时序算子实现 | `skydiscover/skygen/factorgen/cryptokbar/factorlib/ops/ts_ops.py` |
| 聚合算子实现 | `skydiscover/skygen/factorgen/cryptokbar/factorlib/ops/agg_ops.py` |
| 白名单与描述 | `skydiscover/skygen/factorgen/cryptokbar/factorlib/registry.py`（`TS_OPS_INFO` / `ALLOWED_OPS`） |
| 对外 import 面 | `skydiscover/skygen/factorgen/cryptokbar/factorlib/operators.py` |

**C++ 实现时**：每一个 kernel 的 **规格说明** 应能 **逐条映射** 到 `ts_ops.py` 中对应函数的 **pandas / numpy 调用与参数**（见 §4 全局常量）。

---

## 2. Phase 1 在 FactorEngine 仓库内的建议代码布局

> 目录名可与现有 `native/` / `cpp/` 规划统一；此处用 **`native/ops/`** 示意「算子层」。

```
FactorEngine/
  native/
    CMakeLists.txt
    include/fe/ops/
      spec.hpp              # EPS, dtype policy, NaN rules (文档化常量)
      kernel_concept.hpp    # push / output / reset / ready 接口
      unary.hpp             # Neg, Abs, Log, ...
      binary.hpp            # Add, Sub, Mul, Div
      shift.hpp             # Delay, TsDiff, TsPct
      rolling_mean.hpp      # Ma
      rolling_ema.hpp       # Ema
      rolling_std.hpp       # TsStd (ddof=0, Welford or sum/sq)
      rolling_minmax.hpp    # TsMin, TsMax, TsMinMaxDiff, ...
      rolling_rank.hpp      # TsRank
      bivariate.hpp         # Cov, Corr
      ...
    src/ops/
      (与上同名的 .cpp 或全头文件实现)
    tests/
      run_ma_golden.cpp     # 读 `.fegolden` 二进制 fixture，零第三方依赖
```

**Python 侧配套（已实现）**

- `FactorEngine/tools/export_ts_op_golden.py`：生成 **`native/tests/fixtures/*.fegolden`**（header `FEG1` + `float32` 的 `x[]` + `expected[]`）。
- C++ 侧 `fe_ma_golden_tests`（CMake `ctest`）逐 bar 对齐 `RollingMeanKernel`（`Ma`）；当前构建 **不拉取 googletest**（环境无 GitHub 时仍可 CI）。

---

## 3. Phase 1 算子清单与优先级

### 3.1 P0 — 无窗口或 O(1) 状态（先打通测试框架）

| Python | 文件位置（约） | C++ 注意点 |
|--------|----------------|------------|
| `Neg`, `Abs`, `Sqr`, `Sign`, `Tanh` | `ts_ops.py` §unary | `Sign` 与 `np.sign` 一致；`Tanh` 在 float32 上容差 |
| `Log`, `SLog1p`, `Inv` | §unary | `Log`: `x<=0 → NaN`；`Inv`: 分母 `x+EPS` |
| `Add`, `Sub`, `Mul`, `Div` | §binary | `Div`: **`b+EPS`**；标量广播与 pandas 一致 |

### 3.2 P1 — 单序列滚动（因子池最高频）

| Python | pandas 语义摘要 | C++ 对齐要点 |
|--------|-----------------|--------------|
| `Delay(x,t)` | `x.shift(t)`，`min_periods` 等价于前 `t` 行为 NaN | ring 存历史 |
| `TsDiff`, `TsPct` | 见 `ts_ops.py` | `TsPct` 分母 **`shift(t)+EPS`** |
| `Ma(x,t)` | `rolling(t, min_periods=t, center=False).mean()` | **满窗才非 NaN** |
| `TsSum` | 同 `min_periods=t` | 滑窗和 |
| `TsStd(x,t,ddof=0)` | rolling 在 **float64** 上算 std 再 cast `float32` | C++ 建议 **内部 f64 累加**，输出 **f32**；**ddof=0** |
| `Ema(x,t)` | `ewm(span=t, min_periods=t, adjust=False).mean()` | **必须与 pandas 初值与递推一致**（单独 golden） |
| `TsMin`, `TsMax` | `min_periods=t` | 单调队列 O(1) 摊销 |
| `TsVari` | `var(ddof=0)` | 与 `TsStd`² 对齐或独立 Welford |
| `TsZscore(x,t)` | `(x-Ma)/(TsStd+EPS)`，且 **`|s|<EPS` 或 `s` NaN → 输出 NaN** | 见 `ts_ops.py` L270-281 **mask 规则** |

### 3.3 P2 — 高成本或特殊 `min_periods`

| Python | 特殊规则 | 说明 |
|--------|----------|------|
| `TsRank(x,t)` | `t<=0` 全 NaN；**`t==1` 全 0**；否则 `rolling.rank(pct=True)` 或 `apply(_rank_last)` | C++ 必须与 **平均秩 `(rank-1)/(n-1)`** 分支一致 |
| `TsMed`, `TsMad` | `TsMad` 里 **median rolling 的 `min_periods=max(2,t//2)`** | **不是**简单的 `t` |
| `TsIr` | 同上 **`min_periods=max(2,t//2)`** | `mean/std` 同窗 |
| `TsSkew`, `TsKurt` | pandas `rolling.skew/kurt` 或 `apply` 回退 | 先做 golden 再选实现（增量矩或逐窗重算） |
| `TsWMA` | 权重 `1..t` 归一；**nan 处理见源码 L393-395** | 与 pandas `apply(dot)` 路径对齐 |
| `Cov`, `Corr` | `rolling(t, min_periods=t).cov/corr` | **双序列对齐**；DataFrame 列对齐在 **单币 kernel** 中不出现，可先 **Series 版** |
| `Autocorr(x,t,n)` | 见 L359-375 | 多子 rolling |
| `TsMinMaxDiff` | **`min_periods=1`**（与其它算子 **不一致**） | **必须单独测** |
| `TsMaxDiff`, `TsMinDiff` | `rolling(..., min_periods=1)` | 同上 |

---

## 4. 全局常量与 dtype 策略（必须与 Python 一致）

摘自 `ts_ops.py` 头部（实现前写进 `fe/ops/spec.hpp` 注释与单测）：

| 常量 | Python 值 | C++ 建议 |
|------|-------------|----------|
| `EPS` | `1e-8` | `1e-8f` 或 `double` 混合策略；**与 `Div`/`TsZscore`/`TsPct`/`TsIr` 一致** |
| 序列 dtype | **`np.float32`**（`DTYPE`） | 对外接口 **f32**；内部 `TsStd`/`rolling` 类运算可用 **f64 累加** 再 cast |
| `rolling` | **`center=False`** | 禁止 look-ahead |
| 默认 `min_periods` | **多数为 `t`** | `ready()`：**有效样本数 `< t` → `NaN`** |
| `TsStd` `ddof` | **默认 `0`** | 与 pandas 默认 population std 对齐 |

---

## 5. Golden 对齐测试（代码层必做）

### 5.1 单测形态（推荐）

1. **固定随机种子** 生成长度 `L=5000` 的 `float32` 序列 `x[]`（可含 **注入的 NaN**）。
2. Python：`pandas.Series(x).` 调 `ts_ops.Ma(s, t)` → 得到 `expected[]`（`float32`）。
3. C++：`MaKernel(t)` 对每个 `x[i]` 调用 `push` → `output()`，收集 `actual[]`。
4. 断言：`|actual[i]-expected[i]| <= tol`；**NaN 与 NaN** 用 `isnan` 双向匹配。

**容差**

- 纯整数窗 `Ma/TsSum`：**`tol=1e-5`**（f32）或更严。
- `TsStd/Ema/Corr`：**`tol=1e-4 ~ 1e-5`** 量级，或 **ulp 比较**；若分歧，**以 Python float64 rolling 再 cast 的结果为基准**（与 `TsStd` 实现一致）。

### 5.2 必测边界

- 前 **`t-1`** 根：`NaN`。
- 含 **NaN** 输入：pandas rolling 行为（`min_periods=t` 下 NaN 传播）逐算子确认。
- **`TsZscore`**：`|std|<EPS` 时 **输出 NaN** 的分位点。
- **`TsRank` `t==1`**：全 **0**。
- **`TsMinMaxDiff`**：`min_periods=1` 与 **`Ma` 的 `min_periods=t`** 对比，防抄错。

### 5.3 CI 集成

- **`ctest` + `fe_ma_golden_tests`**：fixtures **预生成并提交**（`*.fegolden`），CI **无需 Python / factorlib** 即可编译跑对齐。
- 后续若引入 **gtest**，可与现有 fixture 格式共用；当前默认避免 **FetchContent 拉 GitHub**（部分环境 403）。

---

## 6. Kernel 接口约定（与 DAG 文档衔接）

每个滚动算子 C++ 类建议满足（与 `factor_kernel_dag_execution.md` 一致）：

```text
void reset();
void push(float x);           // 或 push(float x, float y) 对 Corr
float output() const;         // 当前时刻输出（可能 NaN）
bool ready() const;           // 是否已满 min_periods
```

**`push` 的输入**：对 `Ma(close,30)`，每 bar 推 **`close`**；对 `TsZscore`，若 DAG 展开为子节点，则 **子 kernel 推中间值**。

---

## 7. 与「按币线程池 + 币内 one-by-one 因子」的关系

- **算子层对齐**（本文）**不依赖**调度策略；单线程 golden 即可。
- 调度上线后：**同一 `FactorKernel` 实例仅单线程 `push`**；多币并行 **不会**改变算子数学，只增加 **并发单测**（可选 stress）。

---

## 8. 交付物检查清单（Phase 1 Done 定义）

- [ ] `spec.hpp` 中 **EPS / dtype / NaN** 与 `ts_ops.py` **文档级一致**
- [ ] P0 全部 gtest green
- [ ] P1：`Ma, TsSum, TsStd, Ema, Delay, TsDiff, TsPct, TsZscore` golden green
- [ ] P2：按 **alpha_pool 频次** 排序，至少覆盖 **`TsRank`, `Corr`, `TsMin`, `TsMax`, `TsMinMaxDiff`**
- [ ] 一份 **「与 pandas 已知差异」** 附录（若有无法 bit-identical 的算子，必须写明容差与原因）

---

## 9. Phase 2 预告（`agg_ops`）

`agg_ops.py` 中函数接收 **`kbar: DataFrame`**（整 lookback），与流式 **单值 push** 不直接同构。对齐策略：

1. 为每个 agg 写 **`to_incremental_dag()`** 或 **独立 `AggKernel`**（见 06 文档 §7）。
2. **golden**：对同一 `kbar` 窗口，Python 批量 agg 的最后一行 vs C++ **逐步 push 最后一根** 对齐。

**本期不展开**，避免阻塞 Phase 1。

---

## 10. 建议的落地顺序（两周粒度示例）

| 周 | 任务 |
|----|------|
| W1 | 建 `native/ops` + gtest + `export_ts_op_golden.py`；完成 P0 + `Ma` + `TsStd` + `Delay` |
| W2 | `Ema`（重点）、`TsZscore`、`TsSum`、`TsMin/TsMax`；开始 `TsRank` |
| W3+ | `Corr/Cov`、`TsWMA`、`TsIr/TsMad`、异常 `min_periods` 算子 |

---

*本文应随 `ts_ops.py` 变更更新；若 pandas 版本升级导致 rolling 语义变化，需在 CI 锁定版本或更新 golden。*
