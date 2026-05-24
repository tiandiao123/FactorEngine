# Streaming Audit / Replay 代码重构方案

> 日期：2026-05-24
> 状态：实施设计草案
> 关联文档：`docs/20260523/stream_audit_replay_design.md`
> 目标：结合当前代码结构，说明如何最小侵入地增加流式数据落盘、回放和聚合校准能力

---

## 1. 当前代码架构回顾

当前 FactorEngine 的实时链路已经是比较清晰的 v4 ClockDriver 架构。

### 1.1 Engine 层

入口文件：

```text
factorengine/engine.py
```

当前职责：

1. 解析 `symbols / exchange / data_source / factor_group`。
2. 创建 OKX / Binance kline worker。
3. 创建 OKX / Binance trades worker。
4. 创建 `ClockDriver`。
5. 创建 C++ `fe_runtime.InferenceEngine`。
6. 启动 runtime thread，把 `bar_queue` 里的 bar 推给 C++。

关键代码形态：

```python
if data_source in ("trades", "kline_trades"):
    if exchange == "okx":
        from dataflow.okx.trades.worker import TradesWorker
        self._trades_worker = TradesWorker(symbols=symbols)
    elif exchange == "binance":
        from dataflow.binance.trades.worker import BinanceTradesWorker
        self._trades_worker = BinanceTradesWorker(symbols=symbols, testnet=self._testnet)
    self._trades_worker.set_freq(self.data_freq_seconds)
```

ClockDriver 创建：

```python
self._clock_driver = ClockDriver(
    freq_seconds=self.data_freq_seconds,
    bar_queue=self._bar_queue,
)
```

### 1.2 Trades Worker 层

OKX：

```text
dataflow/okx/trades/worker.py
dataflow/okx/trades/collector.py
```

Binance：

```text
dataflow/binance/trades/worker.py
dataflow/binance/trades/collector.py
```

两边 worker 结构几乎一致：

```text
Collector websocket thread
  -> _on_trade(symbol, px, sz, side, ts_ms)
  -> TradesAccumulator.update(px, sz, side)

ClockDriver thread
  -> worker.flush()
  -> accumulator.snapshot_and_reset()
  -> returns (round_bars, round_trades)
```

当前 `_on_trade()`：

```python
def _on_trade(self, symbol: str, px: float, sz: float, side: str, ts_ms: int) -> None:
    acc = self._accumulators.get(symbol)
    if acc is not None:
        acc.update(px, sz, side)
        self._trade_count += 1
```

当前 `flush()`：

```python
snap = acc.snapshot_and_reset()
round_trades[sym] = snap
round_bars[sym] = np.array([
    ts_ms,
    snap["open"],
    snap["high"],
    snap["low"],
    snap["close"],
    snap["volume"],
    NaN,
    NaN,
])
```

### 1.3 ClockDriver 层

文件：

```text
dataflow/clock_driver.py
```

当前职责：

1. 每个 wall-clock boundary 调所有 worker 的 `flush()`。
2. `_aggregate()` 合并 kline/trades。
3. 把 `(merged_bars, merged_trades)` 推入 `bar_queue`。

关键代码：

```python
def _flush_and_aggregate(self) -> None:
    raw = {}
    for name, worker in self._workers.items():
        raw[name] = worker.flush()

    merged_bars, merged_trades = self._aggregate(raw)

    if not merged_bars:
        return

    payload = (merged_bars, merged_trades if merged_trades else None)
    self._bar_queue.put(payload, block=False)
```

### 1.4 Accumulator 层

文件：

```text
dataflow/trades/accumulator.py
```

当前 `TradesAccumulator` 是在线聚合器：

```python
update(px, sz, side)
snapshot_and_reset()
```

它不保存每笔 trade，只保存当前 bucket 的滚动状态。

这意味着：如果没有旁路落盘，事后无法重建 bucket。

---

## 2. 需要新增的能力

我们不改变主实时路径，只增加旁路审计路径。

第一版目标链路：

```text
ClockDriver
  -> realtime: bar_queue.put(...)
  -> audit: AggregateBarLog
```

第一版不做：

1. raw websocket 全量落盘。
2. normalized trades 逐笔落盘。
3. replay / reconcile。
4. parquet / zstd。
5. C++ factor node 级 trace。
6. live rollback。
7. REST historical 校准。

第一版只做最小审计能力：

```text
AggregateBarLog
```

原因：逐笔 trades 体量大，默认全量落盘会给磁盘、CPU、队列和后续查询带来明显成本。当前阶段先保存 ClockDriver 已经聚合好的信息，至少能事后知道线上每个 tick 实际推给 runtime 的 bar 是什么。

---

## 3. 新增目录结构

建议新增：

```text
dataflow/audit/
  __init__.py
  config.py
  schema.py
  writer.py
```

测试：

```text
tests/audit/
  test_writer.py
  test_clockdriver_audit.py
```

---

## 4. Phase 1：Audit Schema

新增文件：

```text
dataflow/audit/schema.py
```

### 4.1 AggregateBarEvent

```python
from dataclasses import asdict, dataclass


@dataclass(slots=True)
class AggregateBarEvent:
    bucket_ts_ms: int
    flush_ts_ms: int
    exchange: str
    data_source: str
    freq_seconds: float
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float
    buy_vol: float
    sell_vol: float
    trade_count: int
    source: str = "online_clockdriver"

    def to_dict(self) -> dict:
        return asdict(self)
```

### 4.2 为什么第一版只保留 AggregateBarEvent

`AggregateBarEvent` 对应当前系统中最重要的审计点：ClockDriver 每个 tick 聚合后准备推给 runtime 的结果。

第一版不保存 `NormalizedTradeEvent` 和 `RawWsEvent`：

1. 逐笔 trades 数据量大，不适合默认全量保存。
2. Raw websocket 格式差异更大，接入点更靠 collector，会扩大改动面。
3. 当前最迫切的问题是事后知道线上聚合结果是什么，而不是立刻重放每笔成交。

后续如果发现某个窗口的 aggregate 异常，再对 selected symbols / debug window 打开 normalized trades 审计。

---

## 5. Phase 2：AuditConfig

新增文件：

```text
dataflow/audit/config.py
```

建议：

```python
from dataclasses import dataclass


@dataclass(slots=True)
class AuditConfig:
    enabled: bool = False
    root_dir: str = "data/audit"
    write_aggregates: bool = True
    queue_size: int = 100_000
    flush_interval_seconds: float = 1.0
    rotate_interval_seconds: int | None = None
```

第一版可以不实现 rotate，保留参数但文档标注未启用。

---

## 6. Phase 3：AuditWriter

新增文件：

```text
dataflow/audit/writer.py
```

### 6.1 设计原则

1. 主线程只做 `put_nowait()`。
2. 写盘在独立线程完成。
3. queue 满了 drop audit event，不阻塞实时路径。
4. writer 出错只增加 `write_errors`，不影响 collector/clock。

### 6.2 建议接口

```python
class AuditWriter:
    def __init__(self, config: AuditConfig): ...

    def start(self) -> None: ...
    def stop(self) -> None: ...

    def write_aggregate_bar(self, event: AggregateBarEvent) -> None: ...
    def write_aggregate_batch(...) -> None: ...

    def stats(self) -> dict: ...
```

### 6.3 内部事件包装

可以用简单 tuple：

```python
AuditRecord = tuple[str, dict]
```

其中 `kind`：

```text
aggregate_bar
```

### 6.4 第一版文件路径

第一版不用复杂 partition，直接按启动时间创建文件：

```text
data/audit/
  aggregate_bars_20260524_101500.jsonl
```

第二版再改成：

```text
data/audit/aggregate_bars/exchange=binance/data_source=trades/freq=1s/date=.../hour=.../part.jsonl
```

原因：第一版重点是验证闭环，不是做数据湖。

---

## 7. Phase 4：Engine 接入 AuditWriter

修改文件：

```text
factorengine/engine.py
```

### 7.1 `Engine.__init__` 增加参数

```python
from dataflow.audit.config import AuditConfig

def __init__(
    ...
    audit_config: AuditConfig | None = None,
):
```

初始化：

```python
self._audit = None
if audit_config is not None and audit_config.enabled:
    from dataflow.audit.writer import AuditWriter
    self._audit = AuditWriter(audit_config)
```

### 7.2 传给 ClockDriver

```python
self._clock_driver = ClockDriver(
    freq_seconds=self.data_freq_seconds,
    bar_queue=self._bar_queue,
    audit=self._audit,
    exchange=self._exchange,
    data_source=self._data_source,
)
```

第一版不把 `audit` 传给 trades worker，因为不做逐笔 trades 落盘。

### 7.3 生命周期

`start()` 里：

```python
if self._audit is not None:
    self._audit.start()
```

应该放在 worker start 之前。

`stop()` 里：

```python
if self._audit is not None:
    self._audit.stop()
```

应该放在 worker/clock/runtime 停止之后，确保队列里剩余记录能 flush。

### 7.4 stats

新增：

```python
@property
def audit_stats(self) -> dict:
    if self._audit is None:
        return {}
    return self._audit.stats()
```

---

## 8. Phase 5：ClockDriver 写 AggregateBarLog

修改文件：

```text
dataflow/clock_driver.py
```

### 8.1 构造函数增加参数

```python
def __init__(
    self,
    freq_seconds: float,
    bar_queue: queue.Queue,
    *,
    audit=None,
    exchange: str | None = None,
    data_source: str | None = None,
):
    self._audit = audit
    self._exchange = exchange
    self._data_source = data_source
```

### 8.2 `_flush_and_aggregate()` 增加写 aggregate

位置：

```python
merged_bars, merged_trades = self._aggregate(raw)

if not merged_bars:
    return

# audit here

payload = (merged_bars, merged_trades if merged_trades else None)
self._bar_queue.put(payload, block=False)
```

建议：

```python
if self._audit is not None:
    self._write_audit_aggregates(merged_bars, merged_trades)
```

### 8.3 新增 helper

```python
def _write_audit_aggregates(
    self,
    merged_bars: dict[str, np.ndarray],
    merged_trades: dict[str, dict],
) -> None:
    flush_ts_ms = int(time.time() * 1000)
    for sym, bar in merged_bars.items():
        trades = merged_trades.get(sym, {}) if merged_trades else {}
        self._audit.write_aggregate_bar(
            AggregateBarEvent(
                bucket_ts_ms=int(bar[0]),
                flush_ts_ms=flush_ts_ms,
                exchange=self._exchange or "",
                data_source=self._data_source or "",
                freq_seconds=self._freq,
                symbol=sym,
                open=float(bar[1]),
                high=float(bar[2]),
                low=float(bar[3]),
                close=float(bar[4]),
                volume=float(bar[5]),
                vwap=float(trades.get("vwap", float("nan"))),
                buy_vol=float(trades.get("buy_vol", float("nan"))),
                sell_vol=float(trades.get("sell_vol", float("nan"))),
                trade_count=int(trades.get("trade_count", 0)),
            )
        )
```

### 8.4 注意

ClockDriver 不应该知道落盘路径、json 格式、文件 rotate。

它只创建 `AggregateBarEvent` 并交给 `AuditWriter`。

---

## 9. 第一版查询 / 检查方式

第一版只存 aggregate，因此不做 replay。最小检查方式是直接读取 `aggregate_bars_*.jsonl`：

```bash
head data/audit/aggregate_bars_20260524_101500.jsonl
```

后续可以补一个轻量命令：

```bash
python -m dataflow.audit.inspect \
  --input data/audit/aggregate_bars_20260524_101500.jsonl \
  --symbol BTC-USDT \
  --start "2026-05-24T10:00:00Z" \
  --end "2026-05-24T10:10:00Z"
```

这个命令只做过滤、统计和导出，不做逐笔 replay。

---

## 10. 文件修改清单

### 10.1 新增

```text
dataflow/audit/__init__.py
dataflow/audit/config.py
dataflow/audit/schema.py
dataflow/audit/writer.py
tests/audit/test_writer.py
tests/audit/test_clockdriver_audit.py
```

### 10.2 修改

```text
factorengine/engine.py
dataflow/clock_driver.py
```

### 10.3 暂不修改

```text
dataflow/okx/trades/collector.py
dataflow/binance/trades/collector.py
dataflow/okx/trades/worker.py
dataflow/binance/trades/worker.py
dataflow/trades/accumulator.py
native/
factorengine/factors/
```

collector 不改的原因：

1. 第一版不保存 raw message。
2. 第一版不传 trade_id。
3. 避免扩大回归面。

trades worker 不改的原因：

1. 第一版不保存逐笔 normalized trades。
2. 只在 ClockDriver 聚合后落盘。
3. 避免在高频 trade callback 上增加额外开销。

accumulator 不改的原因：

1. 在线聚合逻辑保持稳定。
2. 第一版不改变聚合语义。

---

## 11. 关键风险和取舍

### 11.1 audit drop

audit queue 满时会 drop。

这意味着 audit log 不是强一致证据链。

但这是有意取舍：实时链路优先。

需要在 report 里展示：

```text
audit_dropped > 0 时，本时间段 aggregate audit log 不完整
```

### 11.2 online bucket timestamp

当前 `TradesWorker.flush()` 用 `time.time()` 对齐 bucket：

```python
ts_ms = now - (now % freq_ms)
```

这意味着 online aggregate 的 bucket timestamp 来自 flush wall clock，而不是每笔 trade 的 `event_ts_ms`。

这点非常关键。

第一版只保存这个 online bucket timestamp，不重新定义聚合语义。如果未来要严格按照 event time 聚合，需要重构 `TradesAccumulator`：

```text
当前:
  accumulator 是一个当前 bucket

未来:
  accumulator 按 event_ts_ms 分 bucket
  flush(boundary_ts_ms) 时只 flush 已关闭 bucket
```

这属于更深层的聚合语义升级，不建议和 audit 第一版混在一起做。

### 11.3 forward-fill

在线 `TradesAccumulator.snapshot_and_reset()` 会在空 bucket forward-fill OHLC + vwap。

第一版 AggregateLog 会如实保存 forward-fill 后的 online aggregate。也就是说，这份日志记录的是“系统实际推给 runtime 的值”，不是“交易所原始成交重建出来的值”。

---

## 12. 建议实施顺序

### Step 1：纯新增 audit 基础设施

新增：

```text
config.py
schema.py
writer.py
tests/audit/test_writer.py
```

不改现有实时链路。

### Step 2：ClockDriver 写 aggregate

修改：

```text
dataflow/clock_driver.py
factorengine/engine.py
```

测试：

```text
tests/audit/test_clockdriver_audit.py
```

这一步风险较小，因为只在 aggregate 后旁路写。

### Step 3：小规模 live dry-run

用 Binance trades 1s，少量 symbol：

```python
Engine(
    symbols=["BTC-USDT", "ETH-USDT"],
    exchange="binance",
    data_source="trades",
    data_freq="1s",
    factor_group="okx_perp_trades",
    audit_config=AuditConfig(enabled=True),
)
```

跑 10 分钟：

```text
检查 audit_stats
检查 jsonl 行数
抽查 aggregate_bars jsonl 内容
```

---

## 13. 第一版完成标准

第一版可以认为完成，当满足：

1. `Engine(audit_config=...)` 可启动。
2. audit disabled 时现有测试不受影响。
3. audit enabled 时能写 aggregate bar jsonl。
4. aggregate bar 字段包含 OHLCV、vwap、buy_vol、sell_vol、trade_count。
5. audit queue 满不阻塞实时链路。
6. `engine.audit_stats` 可查看写入和 drop 情况。

---

## 14. 后续升级

第一版跑通后，再考虑：

1. selected symbols 的 NormalizedTradeLog。
2. collector 层 RawWsEvent。
3. callback 增加 `trade_id` 和更精确 `recv_ts_ms`。
4. hourly rotate。
5. zstd 压缩。
6. parquet compaction。
7. replay / reconcile。
8. REST historical trades 校准。
9. event-time bucket accumulator。
10. kline audit。
11. factor output audit。

---

## 15. 一句话结论

这次改造应该按“旁路审计”而不是“重写实时聚合”来做。第一版只新增 `dataflow/audit`，并在 `ClockDriver._flush_and_aggregate()` 旁路写 aggregate bars。暂时不存逐笔 trades，不做 replay/reconcile。这样代码改动最小、数据体量可控，同时能先保留线上实际推给 runtime 的聚合结果，满足当前事后核查的第一层需求。
