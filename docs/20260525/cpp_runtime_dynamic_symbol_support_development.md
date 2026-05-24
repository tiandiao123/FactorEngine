# C++ Runtime Dynamic Symbol Support 开发说明

> 日期：2026-05-25
> 状态：开发设计
> 范围：补齐 dynamic symbol universe 在 C++ `InferenceEngine` / pybind / Python runtime loop 里的 add/reset/remove 语义

---

## 1. 背景

`docs/20260524/dynamic_symbol_list_support_development.md` 已经完成了 Python dataflow 侧的低频 dynamic symbol universe rebalance：

```text
SymbolProvider / manual update
  -> SymbolManager stage diff
  -> ClockDriver post-flush commit
  -> trades worker rebuild WS subscription
  -> bar_queue 发送 symbol_diff control message
  -> runtime output 按 active_symbols 过滤
  -> audit 写 symbol_version / symbol_diff event
```

这条链路能保证：

1. Engine 进程不中断。
2. ClockDriver 不阻塞在 WebSocket 重连上。
3. 被删除的 symbol 不再从 worker 输出 bars。
4. `get_factor_outputs()` 不再暴露 inactive symbol。
5. audit / verifier 能解释某个 symbol 后续缺 bar 是 inactive，而不是数据丢失。

但是这还不是完整的 C++ runtime dynamic support。

当前 removed symbol 在 Python 层变成 inactive，但 C++ `InferenceEngine` 里的 `SymbolRunner` 仍然保留，rolling state 也没有 reset 或删除。

---

## 2. 当前 C++ runtime 状态

当前 C++ 暴露的相关接口：

```cpp
class InferenceEngine {
public:
    void add_symbol(const std::string& symbol);
    void add_factor(const std::string& symbol,
                    const std::string& factor_id,
                    FactorGraph graph);
    void push_bars(const std::unordered_map<std::string, BarData>& bars);
    std::unordered_map<std::string, std::unordered_map<std::string, FeFloat>>
    get_all_outputs() const;
    std::vector<std::string> symbols() const;
    std::size_t num_symbols() const;
    void reset();
};
```

`SymbolRunner` 已经有：

```cpp
class SymbolRunner {
public:
    void reset();
};
```

pybind 当前暴露：

```cpp
.def("add_symbol", &InferenceEngine::add_symbol)
.def("add_factor", &InferenceEngine::add_factor)
.def("push_bars", ...)
.def("get_all_outputs", &InferenceEngine::get_all_outputs)
.def("symbols", &InferenceEngine::symbols)
.def("num_symbols", &InferenceEngine::num_symbols)
.def("reset", &InferenceEngine::reset)
```

当前缺少：

1. `has_symbol(symbol)`
2. `reset_symbol(symbol)`
3. `remove_symbol(symbol)`
4. Python runtime loop 在 `symbol_diff` control message 里调用 C++ add/reset/remove
5. re-add symbol 时重建或 reset factor graph state 的明确语义

---

## 3. 当前“删除 symbol”实际发生了什么

以当前实现为例：

```text
diff.removed = {"ETH-USDT"}
```

提交后：

1. `Engine._active_symbols` 删除 `ETH-USDT`。
2. trades worker 删除 `ETH-USDT` accumulator。
3. worker 重建 WS，不再订阅 `ETH-USDT`。
4. ClockDriver 后续 payload 的 `active_symbols` 不包含 `ETH-USDT`。
5. `Engine._runtime_loop()` 过滤输入和输出，不再把 `ETH-USDT` 放入 `signal_deque[-1]["factors"]`。

但 C++ 里仍然可能存在：

```text
InferenceEngine.runners_["ETH-USDT"]
```

所以现在的状态是：

```text
Python: inactive
Worker: removed
Audit: removed
C++: retained, stale state
Output: filtered away
```

这对第一版低频 rebalance 的业务输出是可接受的，但它有两个隐患：

1. **re-add 语义不干净**：如果 `ETH-USDT` 之后被重新加入，旧 rolling window state 可能继续被复用。
2. **长期内存增长**：如果 universe 长期轮换很多 symbol，C++ retained runners 会越来越多。

---

## 4. 目标语义

建议把 C++ runtime dynamic support 分成三层语义。

| 操作 | 第一优先级 | 目的 |
|------|------------|------|
| `has_symbol(symbol)` | 必须 | Runtime thread 判断 C++ 是否已有 runner |
| `reset_symbol(symbol)` | 必须 | re-add symbol 时清空旧 rolling state |
| `add_symbol_with_factors(symbol)` | Python 侧实现 | 新 symbol 第一次出现时创建 runner 并挂 factor graphs |
| `remove_symbol(symbol)` | 第二阶段 | 物理删除 runner，控制长期内存增长 |

第一阶段推荐不要马上依赖 physical remove。原因：

1. 当前 `push_bars()` 会把 runner 指针提交到线程池，删除 runner 必须严格保证没有并发 push 正在使用它。
2. `symbol_order_` 也要同步删除，不能只删 `runners_`。
3. 物理删除后 re-add 需要完整重新创建 factor graphs，Python 侧要能稳定重建每个 factor graph。
4. 现有输出过滤已经能保证 removed symbol 不对外暴露。

因此推荐第一阶段语义是：

```text
remove symbol:
  Python active set 删除
  worker 停止输出
  C++ runner retained but inactive

re-add symbol:
  if C++ runner exists:
      reset_symbol(symbol)
  else:
      add_symbol(symbol)
      attach all configured factors
```

这能先解决最重要的 correctness 问题：re-add 不复用旧 rolling window。

---

## 5. 推荐 runtime control flow

当前 queue 顺序已经正确：

```text
bars(v1)
symbol_diff(v2)
bars(v2)
```

需要把 `symbol_diff` 的处理从“只更新 Python active set”升级成“同步 C++ runtime 状态”。

建议：

```python
def _parse_runtime_payload(self, payload):
    if payload["type"] == "symbol_diff":
        diff = payload["diff"]
        self._apply_symbol_diff_to_runtime(diff)
        return None
```

`_apply_symbol_diff_to_runtime(diff)` 只在 runtime thread 调用：

```python
def _apply_symbol_diff_to_runtime(self, diff):
    self._last_symbol_diff = diff
    self._active_symbols = set(diff.target)
    self._symbol_version = diff.version

    if self._inference is None:
        return

    for sym in sorted(diff.added):
        if self._inference.has_symbol(sym):
            self._inference.reset_symbol(sym)
        else:
            self._add_runtime_symbol_with_factors(sym)

    for sym in sorted(diff.removed):
        # Phase 1: retained inactive, no physical delete.
        # Phase 2: optional self._inference.remove_symbol(sym)
        pass
```

这样保证 C++ runtime 结构修改和 `push_bars()` 都在同一个 runtime thread 的消息顺序里发生：

```text
Runtime thread:
  read bars(v1)
  -> push_bars(v1)
  read symbol_diff(v2)
  -> reset/add C++ runners
  read bars(v2)
  -> push_bars(v2)
```

ClockDriver thread 不直接触碰 C++。

---

## 6. 新 symbol 如何创建 factor graphs

当前 Engine 初始化时会为所有 startup symbols 创建 C++ runner 和 factors。动态新增 symbol 也需要复用同样 factor graph 构造逻辑。

推荐把现有 `_init_inference()` 中“给某个 symbol 添加所有 factors”的逻辑抽成 helper：

```python
def _add_runtime_symbol_with_factors(self, symbol: str) -> None:
    if self._inference is None:
        return
    self._inference.add_symbol(symbol)
    for factor_group in self._factor_groups:
        for factor_id, graph in build_factor_graphs(factor_group):
            self._inference.add_factor(symbol, factor_id, graph)
```

实际实现要注意：

1. `FactorGraph` 是 move-only 风格时，不能复用同一个 graph object 给多个 symbol。
2. 必须为每个 symbol 重新 build 一套 factor graphs。
3. factor id 顺序要和 startup path 保持一致。
4. `_factor_ids` 不应该因为每个 symbol 新增而重复 append。它是 factor schema，不是 per-symbol list。

如果当前 `_init_inference()` 没有清晰 helper，建议先重构为：

```text
_init_inference(groups, num_threads)
  -> create InferenceEngine
  -> resolve/build factor builders once
  -> for sym in initial symbols:
         _add_runtime_symbol_with_factors(sym)
```

---

## 7. C++ API 设计

### 7.1 `has_symbol`

```cpp
[[nodiscard]] bool has_symbol(const std::string& symbol) const {
    return runners_.find(symbol) != runners_.end();
}
```

pybind:

```cpp
.def("has_symbol", &InferenceEngine::has_symbol)
```

### 7.2 `reset_symbol`

```cpp
void reset_symbol(const std::string& symbol) {
    auto it = runners_.find(symbol);
    if (it == runners_.end())
        throw std::runtime_error("Symbol not registered: " + symbol);
    it->second.reset();
}
```

pybind:

```cpp
.def("reset_symbol", &InferenceEngine::reset_symbol, py::arg("symbol"))
```

### 7.3 `remove_symbol`，第二阶段

如果需要物理删除：

```cpp
bool remove_symbol(const std::string& symbol) {
    auto erased = runners_.erase(symbol);
    if (!erased) return false;

    symbol_order_.erase(
        std::remove(symbol_order_.begin(), symbol_order_.end(), symbol),
        symbol_order_.end()
    );
    return true;
}
```

但要先确认调用线程语义：

1. 只允许 runtime thread 调用。
2. 不允许和 `push_bars()` 并发。
3. 当前 `push_bars()` 会等待所有 submitted tasks 完成后返回，所以只要 `remove_symbol()` 不在 `push_bars()` 内部并发调用，就是安全的。

如果未来多个 Python 线程可能直接调用 C++ runtime，则需要 C++ 内部加 mutex。当前建议把 invariant 写清楚：**C++ runtime mutation only happens on Engine runtime thread**。

---

## 8. remove vs retained inactive 的取舍

### Retained inactive

优点：

1. 实现简单。
2. 不影响当前 `symbol_order_` / `runners_` 生命周期。
3. re-add 时只要 `reset_symbol()`，不用重新 attach factors。
4. 风险低，适合第一阶段。

缺点：

1. 长时间大量轮换 symbol 会增加 C++ runner 内存。
2. `self._inference.symbols()` 会包含 inactive symbols，需要 Python 层解释。

### Physical remove

优点：

1. 内存随 active universe 收敛。
2. `num_symbols()` 更符合物理 active runner 数量。

缺点：

1. re-add 必须重新 build factor graphs。
2. 删除时要同步 `symbol_order_`。
3. 错误调用时可能和 thread pool runner 指针生命周期冲突。
4. 测试面更大。

推荐：

```text
Phase A: retained inactive + reset on re-add
Phase B: optional physical remove with strict runtime-thread invariant
```

---

## 9. Python Engine 改动点

### 9.1 Runtime diff handling

当前：

```python
if payload["type"] == "symbol_diff":
    self._last_symbol_diff = diff
    self._active_symbols = set(diff.target)
    self._symbol_version = diff.version
    return None
```

目标：

```python
if payload["type"] == "symbol_diff":
    self._apply_symbol_diff_to_runtime(diff)
    return None
```

### 9.2 Runtime add helper

新增：

```python
def _add_runtime_symbol_with_factors(self, sym: str) -> None:
    ...
```

这个 helper 必须和 startup initialization 共用同一套 factor graph 构造逻辑。

### 9.3 Runtime stats

建议补充：

```python
symbol_stats = {
    "active_count": len(self._active_symbols),
    "runtime_symbol_count": self._inference.num_symbols(),
    "runtime_retained_inactive_count": (
        self._inference.num_symbols() - len(self._active_symbols)
    ),
    "runtime_remove_supported": False,
    "runtime_reset_symbol_supported": True,
}
```

如果实现 physical remove，再把 `runtime_remove_supported` 改成 true。

---

## 10. 测试计划

### 10.1 C++ / pybind tests

新增或扩展 runtime tests：

```python
def test_inference_engine_has_and_reset_symbol():
    engine = fe_runtime.InferenceEngine(1)
    engine.add_symbol("BTC-USDT")
    assert engine.has_symbol("BTC-USDT")
    assert not engine.has_symbol("ETH-USDT")

    # add factor, push bars, outputs non-zero / bars_pushed > 0
    engine.reset_symbol("BTC-USDT")
    # outputs reset, bars_pushed reset
```

如果实现 `remove_symbol`：

```python
def test_inference_engine_remove_symbol_updates_symbols_and_count():
    engine.add_symbol("BTC-USDT")
    engine.add_symbol("ETH-USDT")
    assert engine.remove_symbol("ETH-USDT") is True
    assert engine.has_symbol("ETH-USDT") is False
    assert engine.symbols() == ["BTC-USDT"]
```

### 10.2 Engine runtime tests

新增：

```python
def test_runtime_symbol_diff_adds_missing_cpp_symbol():
    engine = Engine(symbols=["BTC-USDT"], factor_group=..., enable_dynamic_symbols=True)
    diff = SymbolDiff(added={"ETH-USDT"}, ...)
    engine._bar_queue.put({"type": "symbol_diff", "diff": diff})
    # runtime loop consumes diff
    assert engine._inference.has_symbol("ETH-USDT")
```

新增 re-add reset 测试：

```python
def test_runtime_readd_resets_cpp_symbol_state():
    # add ETH, push bars, remove ETH, re-add ETH
    # after re-add, bars_pushed or factor state should be reset
```

### 10.3 Integration order test

验证 queue 顺序：

```text
bars(v1) -> symbol_diff(v2) -> bars(v2)
```

预期：

1. `bars(v1)` 仍按旧 active universe 处理。
2. `symbol_diff(v2)` 后 C++ runtime add/reset 已完成。
3. `bars(v2)` 可以推给新增 symbol。

---

## 11. 实施阶段

### Phase 1：C++ minimal API

目标：

1. `InferenceEngine::has_symbol`
2. `InferenceEngine::reset_symbol`
3. pybind 暴露 `has_symbol` / `reset_symbol`
4. C++/Python binding tests

不做：

1. `remove_symbol`
2. Engine runtime diff mutation

### Phase 2：Engine runtime add/reset

目标：

1. 重构 `_init_inference()`，抽出 `_add_runtime_symbol_with_factors()`
2. `symbol_diff.added`：
   - 已存在 runner：`reset_symbol`
   - 不存在 runner：`add_symbol` + add factors
3. `symbol_diff.removed`：
   - Phase 2 仍 retained inactive
4. `symbol_stats` 暴露 runtime retained inactive count
5. Engine runtime tests

### Phase 3：可选 physical remove

目标：

1. `InferenceEngine::remove_symbol`
2. pybind 暴露 `remove_symbol`
3. `symbol_order_` 同步删除
4. Engine 配置项：

```python
runtime_symbol_remove_mode: Literal["retain", "remove"] = "retain"
```

默认仍用 `retain`，只有确认长期 symbol churn 造成内存问题时再打开 `remove`。

### Phase 4：文档和 audit stats

目标：

1. 更新 `dataflow/README.md`
2. 更新 dynamic symbol 开发文档
3. audit 或 `symbol_stats` 记录：
   - active count
   - runtime physical count
   - retained inactive count
   - remove mode

---

## 12. 风险和注意事项

1. **FactorGraph 不能跨 symbol 复用同一个实例**：每个 symbol 必须重新 build graph。
2. **不要在 ClockDriver thread 里调用 C++ mutation**：所有 C++ runtime add/reset/remove 都应在 runtime thread 顺序处理。
3. **re-add 必须 reset**：否则 rolling window 会跨 inactive gap，输出会误导。
4. **physical remove 不是第一优先级**：先解决 correctness，再解决内存收敛。
5. **输出过滤仍然需要保留**：即使 C++ 支持 remove，payload 的 `active_symbols` 仍是每个 bar 对应 universe 的事实来源。
6. **`get_all_outputs()` 当前返回所有 runner**：只要 retained inactive 存在，Python 侧必须继续按 active set 过滤。

---

## 13. 一句话结论

当前 dynamic symbol list 已经完成 Python dataflow 和 Engine 控制链路，但 C++ runtime 仍是 retained inactive 语义。下一步不建议一上来物理删除 runner，而是先补 `has_symbol` / `reset_symbol`，并让 runtime thread 在消费 `symbol_diff` 时为新增 symbol 创建 runner、为 re-add symbol reset state；等 correctness 稳定后，再按配置支持 `remove_symbol` 做内存收敛。
