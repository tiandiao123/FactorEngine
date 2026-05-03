# Kline v2 — Round Batching 设计文档

> 日期: 2026-05-03
> 范围: 改造 `KlinePublisher`, 实现多 symbol 攒批推送
> 前置: `docs/20260503/kline_v1/okx_kline_streaming_refactor.md` (v1 已完成)
> 状态: 设计中, 待确认

---

## 1. 问题

### 1.1 v1 的行为

v1 中每个 symbol 的 bar 独立推入 `bar_queue`:

```text
t=0ms    BTC 5s bar 到达 → bar_queue.put({"BTC": bar})
t=120ms  ETH 5s bar 到达 → bar_queue.put({"ETH": bar})
t=200ms  SOL 5s bar 到达 → bar_queue.put({"SOL": bar})
```

`_runtime_loop` 每次 `get()` 只拿到 1 个 symbol, `push_bars` 也只算 1 个 symbol 的因子。

### 1.2 导致的问题

| 问题 | 影响 |
|------|------|
| 因子计算效率低 | 3 个 symbol → 每轮调 3 次 `push_bars`, 每次只算 1 个 symbol |
| 因子结果不对齐 | `signal_deque[-1]` 只包含最后一个 symbol 的最新因子, 其他 symbol 是旧值 |
| `bar_index` 语义被破坏 | 1 轮 5s 里 `bars_pushed` 增加 3 次, 与 simulation 的 1 次不一致 |
| 跨品种因子失效 | 如果未来有 cross-sectional 因子 (如 BTC/ETH ratio), partial push 会导致结果不完整 |

### 1.3 simulation 的正确行为 (对照)

```python
# SimBarWorker._run()
round_bars = {}
for symbol in symbols:
    bar = generator.next_bar(ts_ms)
    round_bars[symbol] = bar
bar_queue.put(round_bars)      # 所有 symbol 凑齐后一次性推
```

`_runtime_loop` 每次 `get()` 拿到完整的 `{BTC: bar, ETH: bar, SOL: bar}`,
`push_bars(batch)` 一次性算所有 symbol 的因子。

---

## 2. 目标

改造 `KlinePublisher`, 使 live 模式下 `bar_queue.put()` 的行为与 simulation 一致:

```text
# 改造前
bar_queue 内容:  {"BTC": bar}, {"ETH": bar}, {"SOL": bar}    ← 3 次 put

# 改造后
bar_queue 内容:  {"BTC": bar, "ETH": bar, "SOL": bar}        ← 1 次 put
```

---

## 3. 设计

### 3.1 核心思路: 在 KlinePublisher 内攒批

```text
publish("BTC", bar) ──→ _pending_round["BTC"] = bar
                         检查: 到齐? → NO, 等待

publish("ETH", bar) ──→ _pending_round["ETH"] = bar
                         检查: 到齐? → NO, 等待

publish("SOL", bar) ──→ _pending_round["SOL"] = bar
                         检查: 到齐? → YES!
                         bar_queue.put({"BTC": bar, "ETH": bar, "SOL": bar})
                         清空 _pending_round
```

### 3.2 超时机制

OKX 同一秒内多个 symbol 的 candle 通常在几十到几百毫秒内全部到达,
但网络抖动可能导致某个 symbol 延迟。需要超时机制:

```text
第 1 个 symbol 到达 → 记录 _round_start_time = time.monotonic()
后续 symbol 到达   → 检查是否到齐
超时判定           → 每次 publish() 时检查: now - _round_start_time > timeout?
```

**超时时长**: `min(freq_seconds * 0.8, 4.0)` 秒

| freq | 超时 | 理由 |
|------|------|------|
| 1s | 0.8s | 1s 频率下最多等 0.8s |
| 5s | 4.0s | 5s 频率下最多等 4s, 留 1s 给下一轮 |
| 10s | 4.0s | cap 在 4s, 更长频率没必要等更久 |
| 60s | 4.0s | cap 在 4s |

### 3.3 超时时缺失 symbol 的处理: forward-fill

超时触发时, 缺失的 symbol 用**上一轮的 bar** 填充:

```text
_last_round = {"BTC": bar_t-1, "ETH": bar_t-1, "SOL": bar_t-1}

本轮 _pending_round = {"BTC": bar_t, "ETH": bar_t}     ← SOL 超时未到

flush:
  round_bars = {
      "BTC": bar_t,         ← 本轮数据
      "ETH": bar_t,         ← 本轮数据
      "SOL": bar_t-1,       ← forward-fill 上一轮
  }
  bar_queue.put(round_bars)
  _last_round = round_bars   ← 保存本轮结果
```

为什么选 forward-fill 而不是跳过:

| 策略 | 优点 | 缺点 |
|------|------|------|
| **forward-fill** | 因子引擎始终收到完整 round, 滚动窗口连续 | 缺失 symbol 用旧值, 因子有微小偏差 |
| 跳过缺失 symbol | 不引入虚假数据 | partial push, 回到 v1 的问题 |
| 整轮跳过 | 最安全 | 一个 symbol 拖累所有, 可能频繁跳轮 |

实际场景中 OKX 同一轮 candle 的到达间隔通常 < 500ms, 超时概率极低。
forward-fill 是最务实的兜底策略。

### 3.4 第一轮的特殊处理

系统启动后第一轮没有 `_last_round` 可用于 forward-fill。两种选择:

**方案 A: 等第一轮全部到齐 (推荐)**

第一轮不设超时, 必须等所有 symbol 都到齐后才推送。理由:
- 因子引擎第一次 `push_bars` 确定了内部状态初始化
- 第一轮等几秒完全可以接受 (因子需要几百根 bar 的 warmup 才有意义)
- 逻辑简单: `_last_round is None → 不触发超时`

**方案 B: 第一轮也超时, 缺失的 symbol 不推**

- 推 partial round: `{"BTC": bar, "ETH": bar}` (SOL 缺失就不包含)
- 这和 v1 行为类似, 但至少不是每个 symbol 独立推

→ **选择方案 A**, 第一轮必须等齐。

### 3.5 重叠轮次处理

如果一个 symbol 提前到达了下一轮的数据 (ts 跳变), 需要:

1. 先 flush 当前的 pending round (forward-fill 缺失的 symbol)
2. 然后开始新一轮, 把提前到的 bar 放入新的 pending round

判定依据: 新 bar 的 `ts` 与当前 pending round 的 `ts` 不一致。

```text
pending_round 的 ts = 12:00:05.000
新到的 bar ts       = 12:00:10.000   ← 属于下一轮!

→ flush 当前 round (forward-fill 缺失)
→ 开始新 round, 放入这个 bar
```

---

## 4. 接口变更

### 4.1 KlinePublisher 构造函数

```python
# v1
class KlinePublisher:
    def __init__(self, bar_queue, debug_cache=None, expected_interval_ms=5000):

# v2 — 新增 symbols 和 batch_timeout_s
class KlinePublisher:
    def __init__(
        self,
        bar_queue: queue.Queue,
        symbols: list[str],                    # 新增: 完整 symbol 列表
        debug_cache=None,
        expected_interval_ms: int = 5000,
        batch_timeout_s: float | None = None,  # 新增: 超时, None=自动计算
    ):
```

### 4.2 KlinePublisher.publish() 返回值

```python
# v1: 立即推送, 返回 bool
def publish(self, symbol: str, bar: np.ndarray) -> bool:

# v2: 攒批, 可能触发 flush, 返回 flush 结果
def publish(self, symbol: str, bar: np.ndarray) -> bool:
    """收集一个 symbol 的 bar, 可能触发 round flush.

    Returns True if a complete round was flushed to bar_queue.
    """
```

### 4.3 新增方法

```python
def flush(self) -> bool:
    """强制 flush 当前 pending round (超时或 stop 时调用).

    缺失的 symbol 用 forward-fill. 如果是第一轮且不完整, 不 flush.
    Returns True if a round was flushed.
    """
```

### 4.4 新增统计指标

```python
def stats(self) -> dict[str, int]:
    return {
        "bars_published": ...,        # 已有
        "rounds_flushed": ...,        # 新增: 成功推入 queue 的完整轮数
        "forward_fill_count": ...,    # 新增: forward-fill 的 symbol 次数
        "dup_count": ...,             # 已有
        "gap_count": ...,             # 已有
        "drop_count": ...,            # 已有
    }
```

### 4.5 KlineWorker 变更

只需在构造 `KlinePublisher` 时传入 `symbols`:

```python
# v1
self._publisher = KlinePublisher(
    bar_queue=bar_queue,
    debug_cache=debug_cache,
    expected_interval_ms=freq_seconds * 1000,
)

# v2
self._publisher = KlinePublisher(
    bar_queue=bar_queue,
    symbols=symbols,                          # 新增
    debug_cache=debug_cache,
    expected_interval_ms=freq_seconds * 1000,
    batch_timeout_s=min(freq_seconds * 0.8, 4.0),
)
```

### 4.6 Engine 无需改动

`_runtime_loop` 只关心 `bar_queue.get()` 返回 `dict[str, np.ndarray]`。
v2 推入的数据格式与 simulation 完全一致, Engine 无需任何修改。

---

## 5. 内部状态

```python
class KlinePublisher:
    # 攒批状态
    _expected_symbols: set[str]       # 需要等待的完整 symbol 集合
    _pending_round: dict[str, ndarray] # 当前轮已收集的 bars
    _round_start_time: float | None   # 当前轮第一个 symbol 到达的 monotonic 时间
    _last_round: dict[str, ndarray] | None  # 上一轮完整 round (用于 forward-fill)
    _batch_timeout_s: float           # 超时阈值

    # 去重状态 (保留 v1)
    _last_pushed_ts: dict[str, int]   # per-symbol 最新 ts, 去重用

    # 统计
    _rounds_flushed: int
    _forward_fill_count: int
```

---

## 6. publish() 伪代码

```python
def publish(self, symbol: str, bar: np.ndarray) -> bool:
    ts = int(bar[0])

    # ── 去重 (保留 v1 逻辑) ──
    with self._lock:
        last_ts = self._last_pushed_ts.get(symbol)
        if last_ts is not None and ts <= last_ts:
            self._dup_count += 1
            return False

    # ── gap 检测 (保留 v1 逻辑) ──
    with self._lock:
        if last_ts is not None:
            actual_gap = ts - last_ts
            if actual_gap > self._expected_interval_ms * 2:
                self._gap_count += 1

    # ── 轮次边界检测 ──
    # 如果新 bar 的 ts 与当前 pending round 不同, 先 flush 旧轮
    if self._pending_round:
        existing_ts = int(next(iter(self._pending_round.values()))[0])
        if ts != existing_ts:
            self._flush_round()

    # ── 攒批 ──
    self._pending_round[symbol] = bar
    if self._round_start_time is None:
        self._round_start_time = time.monotonic()

    # debug cache (每个 bar 都写, 不等凑齐)
    if self._debug_cache is not None:
        self._debug_cache.append(symbol, bar)

    # ── 检查是否到齐 ──
    if set(self._pending_round.keys()) >= self._expected_symbols:
        return self._flush_round()

    # ── 检查超时 ──
    if self._last_round is not None:  # 第一轮不超时
        elapsed = time.monotonic() - self._round_start_time
        if elapsed >= self._batch_timeout_s:
            return self._flush_round()

    return False


def _flush_round(self) -> bool:
    """组装完整 round, forward-fill 缺失, 推入 queue."""
    round_bars = dict(self._pending_round)

    # forward-fill
    if self._last_round is not None:
        for sym in self._expected_symbols:
            if sym not in round_bars:
                round_bars[sym] = self._last_round[sym]
                self._forward_fill_count += 1

    # 第一轮不完整 → 不推送
    if self._last_round is None and len(round_bars) < len(self._expected_symbols):
        return False

    # 更新去重 ts
    with self._lock:
        for sym, bar in round_bars.items():
            self._last_pushed_ts[sym] = int(bar[0])

    # 推入 queue
    try:
        self._bar_queue.put(round_bars, block=False)
    except queue.Full:
        self._drop_count += 1
        self._pending_round.clear()
        self._round_start_time = None
        return False

    self._last_round = round_bars
    self._rounds_flushed += 1
    self._bars_published += len(round_bars)

    self._pending_round.clear()
    self._round_start_time = None
    return True
```

---

## 7. 改动范围

### 7.1 需要修改的文件

| 文件 | 改动 |
|------|------|
| `dataflow/okx/kline/publisher.py` | 重写核心逻辑: 攒批 + 超时 + forward-fill |
| `dataflow/okx/kline/worker.py` | 构造 publisher 时传入 `symbols` |
| `tests/dataflow/test_okx_kline_publisher.py` | 重写/新增测试覆盖攒批场景 |
| `tests/dataflow/test_okx_kline_e2e.py` | 更新 e2e 测试验证完整 round |

### 7.2 不需要改动的文件

| 文件 | 理由 |
|------|------|
| `factorengine/engine.py` | `_runtime_loop` 只读 `dict[str, ndarray]`, 格式不变 |
| `dataflow/okx/kline/collector.py` | 不涉及攒批逻辑 |
| `dataflow/okx/kline/adapter.py` | 不涉及攒批逻辑 |
| `dataflow/simulation/**` | 完全独立 |

---

## 8. 测试计划

### 8.1 单元测试 (KlinePublisher)

| 测试 | 验证 |
|------|------|
| `test_all_symbols_arrive_triggers_flush` | 3 个 symbol 全到齐 → flush, queue 里有 1 个完整 round |
| `test_timeout_triggers_flush_with_forward_fill` | 2/3 到齐 + 超时 → flush, 缺失的用 forward-fill |
| `test_first_round_waits_for_all` | 第一轮只到 2/3 + 超时 → 不 flush (没有 last_round 可用) |
| `test_first_round_completes_then_flush` | 第一轮全到齐 → flush |
| `test_ts_mismatch_flushes_previous_round` | 新 ts 到达时, 自动 flush 旧轮 |
| `test_dedup_still_works` | 重复 ts 被过滤, 不进入 pending |
| `test_gap_detection_still_works` | gap 检测逻辑不受影响 |
| `test_forward_fill_count_tracked` | 统计指标准确 |
| `test_backpressure_drop` | queue 满时 drop round, 计数正确 |

### 8.2 集成测试 (E2E)

| 测试 | 验证 |
|------|------|
| `test_3_symbols_batched_correctly` | 模拟 3 symbol candle 流, queue 里每个 round 包含所有 symbol |
| `test_runtime_loop_receives_full_rounds` | `_runtime_loop` 的 `batch` 包含所有 symbol |
| `test_factors_aligned_across_symbols` | `get_factor_outputs()` 的所有 symbol 因子基于同一轮数据 |

---

## 9. 影响分析

### 9.1 性能

| 指标 | v1 | v2 |
|------|-----|-----|
| `push_bars` 调用次数 / 轮 | N (symbol 数) | 1 |
| `get_all_outputs` 调用次数 / 轮 | N | 1 |
| `signal_deque.append` 次数 / 轮 | N | 1 |
| bar_queue 消息数 / 轮 | N | 1 |

3 个 symbol 时 push_bars 调用量减少 66%, 100 个 symbol 时减少 99%。

### 9.2 延迟

攒批引入的额外延迟 = 等待最后一个 symbol 到达的时间。
实测 OKX 同一秒内多个 symbol 的到达间隔通常 < 300ms, 可忽略。
最坏情况: 超时阈值 (4s), 但这意味着 WS 有严重问题, forward-fill 是合理的降级。

### 9.3 向后兼容

- `bar_queue` 的数据格式不变: `dict[str, np.ndarray]`
- 但从 partial (`{sym: bar}`) 变成 full (`{sym1: bar, sym2: bar, ...}`)
- `Engine._runtime_loop` 本身就支持 multi-symbol dict, 不需要改

---

## 10. 一句话总结

改造 `KlinePublisher`: 把逐 symbol 推送改成攒批推送, 等所有 symbol 到齐
(或超时后 forward-fill) 再一次性 `bar_queue.put()`, 与 simulation 行为对齐,
消除因子结果不对齐和重复计算的问题。
