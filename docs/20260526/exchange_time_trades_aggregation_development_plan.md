# Exchange-Time Trades Aggregation 开发实施文档

> 日期：2026-05-28
> 状态：开发文档，待实现
> 关联设计：`docs/20260526/exchange_time_trades_aggregation_allowed_lateness.md`
> 目标：在现有 FactorEngine trades dataflow 中落地 exchange-time 分桶与 `allowed_lateness`，默认保持 receive-time 兼容

---

## 1. 开发目标

当前代码是 receive-time 聚合：

```text
Collector.on_trade(..., ts_ms)
  -> TradesWorker._on_trade(..., ts_ms)
  -> TradesAccumulator.update(px, sz, side)   # ts_ms 当前被丢弃
  -> ClockDriver tick
  -> worker.flush()
  -> snapshot_and_reset()
```

目标是新增 exchange-time 聚合：

```text
trade 到达
  -> 用 exchange trade_ts_ms 计算 bucket_start_ms
  -> 若 bucket_end_ms <= receive_ts_ms - allowed_lateness_ms，则 drop
  -> 否则 update buckets[bucket_start_ms]

ClockDriver tick
  -> 用 now_ms - allowed_lateness_ms 计算 watermark
  -> 输出所有 bucket_end_ms <= watermark 的成熟 bucket
```

第一版必须满足：

```text
默认 trade_time_mode = "receive_time"，现有行为不变
exchange_time 必须显式开启
OKX / Binance trades worker 共用同一套 event-time accumulator
测试覆盖分桶、迟到、flush、空桶、乱序和多 bucket 输出
```

---

## 2. 当前代码接口

### 2.1 Accumulator

文件：

```text
dataflow/trades/accumulator.py
dataflow/okx/trades/accumulator.py  # re-export shim
```

当前接口：

```python
class TradesAccumulator:
    def update(self, px: float, sz: float, side: str) -> None: ...
    def snapshot_and_reset(self) -> dict: ...
```

当前特点：

```text
每个 symbol 一个 accumulator
只有一个 current bucket
update() 不接收 trade_ts_ms / receive_ts_ms
snapshot_and_reset() 按本地 flush 边界切桶
空桶时用 _last_close forward-fill OHLC + vwap
```

### 2.2 TradesWorker

文件：

```text
dataflow/okx/trades/worker.py
dataflow/binance/trades/worker.py
```

当前接口：

```python
def flush(self) -> tuple[dict[str, np.ndarray], dict[str, dict]]:
    ...

def _on_trade(self, symbol: str, px: float, sz: float, side: str, ts_ms: int) -> None:
    acc.update(px, sz, side)  # ts_ms 当前未使用
```

当前 flush 行为：

```text
ts_ms = int(time.time() * 1000)
若 set_freq() 设置了 _freq_ms，则按本地时间对齐
对 active_symbols 逐个 snapshot_and_reset()
返回 (round_bars, round_trades)
```

### 2.3 ClockDriver

文件：

```text
dataflow/clock_driver.py
```

当前限制：

```text
FlushableWorker.flush() 不接收 tick now_ms
_flush_and_aggregate() 每 tick 只构造一个 payload
_bucket_ts_ms() 从 merged_bars 任意一根 bar 取 bar[0]
audit.write_aggregate_batch() 每 tick 写一个 aggregate batch
after_flush_commit(bucket_ts_ms) 每 tick 调一次
```

### 2.4 Engine

文件：

```text
factorengine/engine.py
```

当前缺失：

```text
无 trade_time_mode
无 allowed_lateness_ms
无 event-time audit 字段
```

---

## 3. 新增配置

在 `Engine.__init__` 增加：

```python
trade_time_mode: str = "receive_time"
allowed_lateness_ms: int = 1000
```

校验：

```python
_VALID_TRADE_TIME_MODES = ("receive_time", "exchange_time")

if trade_time_mode not in self._VALID_TRADE_TIME_MODES:
    raise ValueError(...)
if allowed_lateness_ms < 0:
    raise ValueError(...)
```

使用规则：

```text
data_source == "kline":
  trade_time_mode 不影响 kline worker

data_source == "trades":
  trades worker 根据 trade_time_mode 选择 accumulator

data_source == "kline_trades":
  第一版建议禁止 trade_time_mode="exchange_time"
  原因是 kline 当前 tick bucket 与 trades mature bucket 可能不是同一个 bucket_ts_ms
```

第一版推荐限制：

```python
if data_source == "kline_trades" and trade_time_mode == "exchange_time":
    raise ValueError(
        "exchange_time trades with kline_trades requires bucket_ts_ms-aware "
        "ClockDriver aggregation; use data_source='trades' in phase 1"
    )
```

---

## 4. 新增 EventTimeTradesAccumulator

建议在 `dataflow/trades/accumulator.py` 中新增，供 OKX / Binance 共用。

推荐接口：

```python
@dataclass(frozen=True)
class TradeBucketSnapshot:
    bucket_start_ms: int
    bucket_end_ms: int
    vwap: float
    buy_vol: float
    sell_vol: float
    trade_count: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    first_trade_ts_ms: int | None
    last_trade_ts_ms: int | None
    first_receive_ts_ms: int | None
    last_receive_ts_ms: int | None
    is_complete: bool = True


class EventTimeTradesAccumulator:
    def __init__(self, *, freq_ms: int, allowed_lateness_ms: int) -> None: ...
    def update(self, px, sz, side, trade_ts_ms, receive_ts_ms) -> bool: ...
    def flush_ready(self, now_ms: int, *, active: bool = True) -> list[TradeBucketSnapshot]: ...
```

内部状态：

```text
_buckets: dict[int, TradeBucket]
_last_close: float
_next_emit_bucket_start_ms: int | None
_late_trade_count: int
_dropped_late_trade_count: int
_max_lateness_ms: int
_lock: threading.Lock
```

`update()` 返回：

```text
True  = trade 被接受并 update 到某个 bucket
False = trade 超过 allowed_lateness，被 drop
```

### 4.1 TradeBucket 聚合规则

实时主路径必须 O(1)，不要保存 raw trades list。

```text
bucket_start_ms / bucket_end_ms
open / high / low / close
sum_v / sum_pv
buy_vol / sell_vol
trade_count
first_trade_ts_ms / last_trade_ts_ms
first_receive_ts_ms / last_receive_ts_ms
```

OHLC 按 exchange trade time 定义：

```python
if trade_count == 0:
    open = high = low = close = px
else:
    high = max(high, px)
    low = min(low, px)

if first_trade_ts_ms is None or trade_ts_ms < first_trade_ts_ms:
    open = px
    first_trade_ts_ms = trade_ts_ms

if last_trade_ts_ms is None or trade_ts_ms >= last_trade_ts_ms:
    close = px
    last_trade_ts_ms = trade_ts_ms
```

同一 bucket 内乱序 trade 可以修正 `open` / `close`；同一毫秒多笔 trade 时，`close` 使用后到达的同毫秒 trade。

### 4.2 update() 语义

```python
def update(self, px, sz, side, trade_ts_ms, receive_ts_ms) -> bool:
    bucket_start_ms = trade_ts_ms - (trade_ts_ms % self._freq_ms)
    bucket_end_ms = bucket_start_ms + self._freq_ms
    receive_watermark_ms = receive_ts_ms - self._allowed_lateness_ms
    lateness_ms = receive_ts_ms - bucket_end_ms

    with self._lock:
        if bucket_end_ms <= receive_watermark_ms:
            self._late_trade_count += 1
            self._dropped_late_trade_count += 1
            self._max_lateness_ms = max(self._max_lateness_ms, lateness_ms)
            return False

        if self._next_emit_bucket_start_ms is None:
            self._next_emit_bucket_start_ms = bucket_start_ms

        bucket = self._buckets.setdefault(bucket_start_ms, TradeBucket(bucket_start_ms, bucket_end_ms))
        bucket.update(px, sz, side, trade_ts_ms, receive_ts_ms)
        if lateness_ms > 0:
            self._late_trade_count += 1
            self._max_lateness_ms = max(self._max_lateness_ms, lateness_ms)
        return True
```

必须保持：

```text
bucket_start_ms 只用 trade_ts_ms 计算
receive_watermark_ms 只用 receive_ts_ms / now_ms 计算
超过 allowed_lateness 的 trade 不允许重建已删除 bucket
```

### 4.3 flush_ready() 语义

map 只会自然保存有 trade 的 bucket。为了保持现有空桶 forward-fill 行为，需要 `_next_emit_bucket_start_ms`。

```python
def flush_ready(self, now_ms: int, *, active: bool = True) -> list[TradeBucketSnapshot]:
    watermark_ms = now_ms - self._allowed_lateness_ms
    latest_ready_start = watermark_ms - (watermark_ms % self._freq_ms) - self._freq_ms

    with self._lock:
        if self._next_emit_bucket_start_ms is None:
            return []

        snapshots = []
        while self._next_emit_bucket_start_ms <= latest_ready_start:
            bucket_start_ms = self._next_emit_bucket_start_ms
            bucket = self._buckets.pop(bucket_start_ms, None)

            if bucket is None:
                if not active or math.isnan(self._last_close):
                    self._next_emit_bucket_start_ms += self._freq_ms
                    continue
                snap = make_forward_fill_snapshot(bucket_start_ms, self._last_close)
            else:
                snap = bucket.snapshot(last_close=self._last_close)
                if snap.trade_count > 0:
                    self._last_close = snap.close

            snapshots.append(snap)
            self._next_emit_bucket_start_ms += self._freq_ms

        return snapshots
```

边界：

```text
从未见过 trade 且 _last_close 为 NaN 时，不生成无限 NaN 空桶
新增 symbol 从 accumulator 创建后的第一个成熟 bucket 开始输出
删除 symbol 时建议先 flush_ready，再移除 accumulator
```

---

## 5. Worker 改造

OKX 与 Binance worker 都增加：

```python
def __init__(
    self,
    symbols: list[str],
    *,
    trade_time_mode: str = "receive_time",
    allowed_lateness_ms: int = 1000,
    freq_seconds: float | None = None,
    ...
):
```

推荐让 `Engine` 构造 worker 时直接传 `freq_seconds`。`set_freq()` 保留给旧 receive-time 路径兼容，避免 event-time accumulator 在 `__init__` 时拿不到 `freq_ms`。

`_on_trade()` 改造：

```python
receive_ts_ms = int(time.time() * 1000)

if self._trade_time_mode == "exchange_time":
    accepted = acc.update(px, sz, side, ts_ms, receive_ts_ms)
    if accepted:
        self._trade_count += 1
    else:
        self._dropped_late_trade_count += 1
else:
    acc.update(px, sz, side)
    self._trade_count += 1
```

注意：

```text
ts_ms 是 exchange trade time，由 collector 提供
receive_ts_ms 是本地进入 worker callback 的时间
不要用 ts_ms 替代 receive_ts_ms 计算 watermark
```

`flush()` 改成兼容签名：

```python
def flush(self, now_ms: int | None = None):
    if self._trade_time_mode == "exchange_time":
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        return self.flush_ready(now_ms)
    return self._flush_receive_time()
```

event-time 返回：

```python
def flush_ready(self, now_ms: int) -> list[tuple[int, dict[str, np.ndarray], dict[str, dict]]]:
    ...
```

返回结构：

```text
[
  (bucket_ts_ms, round_bars_for_that_bucket, round_trades_for_that_bucket),
  ...
]
```

bar 构造：

```python
bar = np.array([
    snapshot.bucket_start_ms,
    snapshot.open,
    snapshot.high,
    snapshot.low,
    snapshot.close,
    snapshot.volume,
    _NAN,
    _NAN,
], dtype=np.float64)
```

`round_trades[sym]` 必须保留 runtime 当前读取字段：

```text
vwap
buy_vol
sell_vol
trade_count
open
high
low
close
volume
```

可额外增加 audit/debug 字段：

```text
bucket_start_ms
bucket_end_ms
bucket_time_basis = "exchange_time"
allowed_lateness_ms
first_trade_ts_ms / last_trade_ts_ms
first_receive_ts_ms / last_receive_ts_ms
late_trade_count / dropped_late_trade_count / max_lateness_ms
is_complete = True
```

---

## 6. ClockDriver 改造

当前 `worker.flush()` 不接收时间。event-time 需要 tick 时间，因此改成：

```python
now_ms = int(time.time() * 1000)
raw[name] = worker.flush(now_ms)
```

兼容两种返回：

```text
旧格式：
  kline: dict[sym, bar]
  trades receive-time: (bars, trades)

新格式：
  trades exchange-time: list[(bucket_ts_ms, bars, trades)]
```

第一版开发策略：

```text
如果 raw 中没有 event-time list，沿用当前单 payload 流程
如果 raw 中有 event-time list，按 bucket_ts_ms 升序逐个 aggregate、audit、bar_queue.put
每个 emitted bucket 调一次 after_flush_commit(bucket_ts_ms)
```

`kline_trades + exchange_time` 的注意事项：

```text
kline worker 每 tick 返回当前 bucket
trades worker 返回的是 allowed_lateness 之前的 mature bucket
二者 bucket_ts_ms 通常不一致
不要把不同 bucket_ts_ms 的 kline 和 trades 合并到同一个 payload
```

因此第一版推荐：

```text
只支持 data_source="trades" + trade_time_mode="exchange_time"
kline_trades + exchange_time 等 ClockDriver 按 bucket_ts_ms 分组能力完成后再开放
```

---

## 7. Engine 接入

`Engine.__init__` 保存：

```python
self._trade_time_mode = trade_time_mode
self._allowed_lateness_ms = allowed_lateness_ms
```

`_init_live` 创建 trades worker 时传入：

```python
trades_worker = TradesWorker(
    symbols,
    trade_time_mode=self._trade_time_mode,
    allowed_lateness_ms=self._allowed_lateness_ms,
    freq_seconds=self.data_freq_seconds,
)
```

Binance worker 同理。

runtime 侧尽量不改。`Engine._runtime_loop()` 当前只要求：

```text
bars: dict[str, ndarray[8]]
trades: dict[str, dict] | None
bar[0] 是 bucket timestamp
```

event-time 模式只要保持 payload 结构一致，runtime 可以继续工作。

---

## 8. Audit / Metrics

第一版 trades snapshot 建议增加字段：

```text
bucket_time_basis = "exchange_time"
allowed_lateness_ms
bucket_start_ms
bucket_end_ms
first_trade_ts_ms
last_trade_ts_ms
first_receive_ts_ms
last_receive_ts_ms
late_trade_count
dropped_late_trade_count
max_lateness_ms
is_complete
```

Worker `stats()` 增加：

```text
trade_count
accepted_trade_count
late_trade_count
dropped_late_trade_count
max_lateness_ms
flush_count
ready_bucket_count
```

如果 audit writer schema 当前不支持新增字段，第一版可先把字段放入 `round_trades[sym]`，后续 verifier 再消费。

---

## 9. 动态 Symbol 边界

当前 `apply_symbol_diff()` 会直接新增 / 删除 accumulator。

event-time 下建议：

```text
新增 symbol:
  创建新的 EventTimeTradesAccumulator
  _next_emit_bucket_start_ms 从第一笔 trade 所在 bucket 初始化

删除 symbol:
  在移除前先 flush_ready(now_ms)
  已 mature 的 bucket 正常输出
  未 mature 的 bucket 第一版可丢弃并记录 metric
```

更严格版本需要 `effective_from_ts_ms`，让 universe change 与 exchange bucket 对齐。第一版可以保持当前 post-flush commit 语义，但文档和 audit 要标记边界 bucket 可能 partial。

---

## 10. 测试计划

### 10.1 Accumulator 单测

新增测试文件建议：

```text
tests/dataflow/trades/test_event_time_accumulator.py
```

必须覆盖：

```text
同一秒正常聚合：OHLCV / vwap / buy_vol / sell_vol
同一 bucket 内乱序 trade：open / close 按 trade_ts_ms 修正
本地 10:00:01.xxx 收到 trade_ts=10:00:00.990：进 10:00:00 bucket
allowed_lateness 内迟到：接受
超过 allowed_lateness：drop，计数增加，不重建旧 bucket
flush_ready 一次返回多个成熟 bucket，按 bucket_start_ms 升序
空 bucket forward-fill：last_close 已知时生成 volume=0 的 snapshot
从未有 trade 时不生成无限 NaN 空桶
```

### 10.2 Worker 单测

OKX / Binance worker 至少覆盖：

```text
receive_time 默认路径仍调用 TradesAccumulator.update(px, sz, side)
exchange_time 路径把 collector 的 ts_ms 传入 EventTimeTradesAccumulator.update()
flush(now_ms) 在 exchange_time 下返回 list[(bucket_ts_ms, bars, trades)]
round_trades 保留 vwap / buy_vol / sell_vol 等 runtime 字段
```

### 10.3 ClockDriver 单测

覆盖：

```text
旧 worker 返回格式不受影响
event-time worker 返回 0 / 1 / 多个 bucket
多 bucket 按 bucket_ts_ms 升序 put 到 bar_queue
每个 emitted bucket 写一次 audit / 调一次 after_flush_commit
bar_queue 满时 drop_count 语义保持
```

### 10.4 Engine 集成测试

覆盖：

```text
默认 trade_time_mode="receive_time" 行为兼容
data_source="trades" + trade_time_mode="exchange_time" 能启动
data_source="kline_trades" + trade_time_mode="exchange_time" 第一版抛 ValueError
Binance / OKX worker 参数传递一致
```

---

## 11. 开发顺序

推荐按以下顺序提交：

```text
1. 新增 EventTimeTradesAccumulator + 单测
2. 改 OKX / Binance TradesWorker，保留 receive_time 默认路径
3. 改 ClockDriver：flush(now_ms) + event-time list 返回兼容
4. 改 Engine：新增配置、参数传递、组合限制
5. 补 audit / stats 字段
6. 增加 integration tests / examples
```

每一步都必须保持 receive-time 测试通过，避免把默认实时行为一起改掉。

---

## 12. 实现检查清单

```text
[配置] trade_time_mode 默认 receive_time
[配置] allowed_lateness_ms 非负校验
[分桶] bucket_start_ms 只依赖 trade_ts_ms
[迟到] update() 入口使用 receive_ts_ms - allowed_lateness_ms 判断 drop
[flush] flush_ready() 使用 ClockDriver tick now_ms - allowed_lateness_ms
[delete] emit 后 bucket 从 map 删除；迟到 trade 不重建旧 bucket
[多桶] 同一 symbol map 允许同时存在多个 bucket
[顺序] 多 bucket 输出按 bucket_start_ms 升序
[空桶] 只为 active symbol 且 last_close 已知时 forward-fill
[兼容] receive_time worker.flush() 返回格式不变
[runtime] payload bars/trades 结构保持 Engine._runtime_loop 可消费
[kline_trades] 第一版明确禁止或实现 bucket_ts_ms-aware 合并，不能隐式错合并
```
