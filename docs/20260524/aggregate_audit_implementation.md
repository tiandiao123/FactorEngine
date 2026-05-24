# Aggregate Audit 落盘实现说明

> 日期：2026-05-24
> 状态：已实现
> 范围：只保存 ClockDriver 聚合后的 aggregate bars，不保存逐笔 trades / raw websocket

---

## 1. 这次实现了什么

本次实现的是 **aggregate-only audit logging**：

```text
ClockDriver._flush_and_aggregate()
  -> workers.flush()
  -> ClockDriver._aggregate()
  -> AuditWriter.write_aggregate_batch()
  -> bar_queue.put(...)
```

也就是说，每个 ClockDriver tick 聚合完成后，系统会把“即将推给 runtime 的聚合 bar”旁路写入 JSONL 文件。

第一版不做：

1. 不保存 raw websocket message。
2. 不保存逐笔 normalized trades。
3. 不做 replay / reconcile。
4. 不改变 trades/kline 聚合逻辑。
5. 不改 C++ runtime。

这份日志记录的是：**线上当时实际给 factor runtime 的输入是什么。**

---

## 2. 新增文件

```text
dataflow/audit/
  __init__.py
  config.py
  schema.py
  writer.py
```

测试：

```text
tests/audit/test_writer.py
tests/audit/test_clockdriver_audit.py
```

修改：

```text
factorengine/engine.py
dataflow/clock_driver.py
```

---

## 3. 调用链

### 3.1 用户侧开启

```python
from dataflow.audit import AuditConfig
from factorengine.engine import Engine

engine = Engine(
    symbols=["BTC-USDT", "ETH-USDT"],
    exchange="binance",
    data_source="trades",
    data_freq="5s",
    factor_group="okx_perp_trades",
    audit_config=AuditConfig(
        enabled=True,
        root_dir="data/audit",
    ),
)
engine.start()
```

如果 `audit_config` 不传，或者 `enabled=False`，现有行为不变。

### 3.2 Engine 初始化

文件：

```text
factorengine/engine.py
```

新增参数：

```python
audit_config: AuditConfig | None = None
```

初始化逻辑：

```python
self._audit = None
if audit_config is not None and audit_config.enabled:
    from dataflow.audit.writer import AuditWriter
    self._audit = AuditWriter(audit_config)
```

然后传给 `ClockDriver`：

```python
self._clock_driver = ClockDriver(
    freq_seconds=self.data_freq_seconds,
    bar_queue=self._bar_queue,
    audit=self._audit,
    exchange=self._exchange,
    data_source=self._data_source,
)
```

### 3.3 Engine 生命周期

`start()` 里先启动 audit writer：

```python
if self._audit is not None:
    self._audit.start()
```

`stop()` 里最后停止 audit writer：

```python
if self._audit is not None:
    self._audit.stop()
```

停止顺序是：

```text
ClockDriver / workers / runtime 停止
  -> AuditWriter.stop()
  -> drain queue
  -> flush file
  -> close file
```

这样可以尽量把队列里已接收的 audit records 写完。

---

## 4. ClockDriver 接入点

文件：

```text
dataflow/clock_driver.py
```

`ClockDriver.__init__` 新增：

```python
audit=None
exchange: str | None = None
data_source: str | None = None
```

核心写入点在 `_flush_and_aggregate()`：

```python
merged_bars, merged_trades = self._aggregate(raw)

if not merged_bars:
    return

if self._audit is not None:
    self._audit.write_aggregate_batch(
        exchange=self._exchange,
        data_source=self._data_source,
        freq_seconds=self._freq,
        bars=merged_bars,
        trades=merged_trades,
        flush_ts_ms=int(time.time() * 1000),
    )

if self._bar_queue is not None:
    self._bar_queue.put(payload, block=False)
```

注意：

1. Audit 写入发生在 `_aggregate()` 之后。
2. 写入的是 `merged_bars / merged_trades`，也就是线上实际聚合结果。
3. `AuditWriter` 内部是 `put_nowait()`，不会阻塞 ClockDriver。
4. 如果 audit 写入异常，ClockDriver 只打日志，仍继续推 `bar_queue`。
5. `bar_queue` 允许为 `None`，支持 audit-only 模式。

---

## 5. Audit-only 模式

现在支持只落盘、不跑 factor runtime：

```python
engine = Engine(
    symbols=["BTC-USDT", "ETH-USDT"],
    exchange="binance",
    data_source="trades",
    data_freq="5s",
    factor_group=None,
    audit_config=AuditConfig(enabled=True),
)
```

以前没有 `factor_group` 时不会创建 `bar_queue`，也不会创建 `ClockDriver`。

现在逻辑是：

```text
if self._bar_queue is not None or self._audit is not None:
    create ClockDriver
```

因此：

```text
有 factor_group:
  ClockDriver -> audit + bar_queue -> runtime

无 factor_group 但有 audit:
  ClockDriver -> audit only
```

---

## 6. 数据格式

schema 文件：

```text
dataflow/audit/schema.py
```

结构：

```python
@dataclass(slots=True)
class AggregateBarEvent:
    bucket_ts_ms: int
    flush_ts_ms: int
    exchange: str
    data_source: str
    freq_seconds: float
    symbol: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    vwap: float | None
    buy_vol: float | None
    sell_vol: float | None
    trade_count: int
    source: str = "online_clockdriver"
```

JSONL 示例：

```json
{"bucket_ts_ms":1790000000000,"flush_ts_ms":1790000001003,"exchange":"binance","data_source":"trades","freq_seconds":5.0,"symbol":"BTC-USDT","open":65000.0,"high":65010.0,"low":64990.0,"close":65005.0,"volume":10.5,"vwap":65003.2,"buy_vol":6.2,"sell_vol":4.3,"trade_count":128,"source":"online_clockdriver"}
```

如果字段是 `NaN` 或 `inf`，写入时会转成 JSON `null`。

例如 kline-only 模式没有 trades 信息：

```json
{"bucket_ts_ms":1790000000000,"flush_ts_ms":1790000001003,"exchange":"okx","data_source":"kline","freq_seconds":5.0,"symbol":"BTC-USDT-SWAP","open":65000.0,"high":65020.0,"low":64980.0,"close":65010.0,"volume":42.0,"vwap":null,"buy_vol":null,"sell_vol":null,"trade_count":0,"source":"online_clockdriver"}
```

---

## 7. Writer 规则

文件：

```text
dataflow/audit/writer.py
```

配置：

```python
AuditConfig(
    enabled=True,
    root_dir="data/audit",
    queue_size=100_000,
    flush_interval_seconds=1.0,
    flush_max_records=10_000,
    flush_max_bytes=4 * 1024 * 1024,
    rotate_interval_seconds=3600,
    rotate_max_bytes=512 * 1024 * 1024,
    fsync_interval_seconds=None,
)
```

### 7.1 产生频率

Audit record 的产生频率由 ClockDriver 决定。

例如：

```text
data_freq = 5s
symbols = 200
```

则大约：

```text
每 5 秒产生 200 行 aggregate records
```

### 7.2 flush 规则

AuditWriter 后台线程写文件，任一条件满足就 flush：

```text
flush_interval_seconds 到达
flush_max_records 到达
flush_max_bytes 到达
```

默认：

```text
最多约 1 秒 flush 一次
或累计 10000 records
或累计 4MB
```

### 7.3 rotate 规则

任一条件满足就切新文件：

```text
rotate_interval_seconds 到达
rotate_max_bytes 到达
```

默认：

```text
每 1 小时 rotate
或单文件超过 512MB rotate
```

当前第一版文件名：

```text
data/audit/aggregate_bars_YYYYMMDD_HHMMSS_part000001.jsonl
data/audit/aggregate_bars_YYYYMMDD_HHMMSS_part000002.jsonl
```

后续如果需要，可以改成 partition 目录：

```text
data/audit/aggregate_bars/
  exchange=binance/
    data_source=trades/
      freq=5s/
        date=2026-05-24/
          hour=15/
            part-000001.jsonl
```

### 7.4 queue 满的行为

`AuditWriter` 使用非阻塞 enqueue：

```python
queue.put_nowait(record)
```

如果队列满：

```text
drop audit record
dropped += 1
不阻塞实时路径
```

这意味着 audit log 是低延迟旁路证据，不是强一致事务日志。

---

## 8. 状态查询

`Engine` 新增：

```python
engine.audit_stats
```

示例：

```python
{
    "enabled": True,
    "queued": 0,
    "enqueued": 1200,
    "written": 1200,
    "dropped": 0,
    "write_errors": 0,
    "flush_count": 12,
    "last_flush_ts": 1790000000000,
    "current_file": "data/audit/aggregate_bars_20260524_150000_part000001.jsonl",
}
```

如果 audit 没开启：

```python
engine.audit_stats == {}
```

---

## 9. 测试

已新增：

```text
tests/audit/test_writer.py
tests/audit/test_clockdriver_audit.py
```

覆盖：

1. `AuditWriter` 写 JSONL。
2. `AuditWriter` queue 满时 drop。
3. `AuditWriter` 按时间 rotate。
4. `ClockDriver` 写 aggregate audit records。
5. audit 写入异常不影响 `bar_queue`。
6. 没有 `bar_queue` 的 audit-only 模式。

验证命令：

```bash
pytest -q tests/audit tests/dataflow/test_clock_driver.py
```

当前结果：

```text
18 passed
```

---

## 10. 后续方向

当前版本只做 aggregate audit。后续可以按需求增加：

1. selected symbols 的 normalized trades 落盘。
2. raw websocket debug 落盘。
3. partition 目录布局。
4. zstd 压缩。
5. parquet compaction。
6. replay / reconcile。
7. REST historical 校准。

建议顺序仍然是：

```text
aggregate audit
  -> dynamic symbol list
  -> selected trades audit / replay
```

因为 aggregate audit 可以先为后续 dynamic symbol list 提供事后核查依据。
