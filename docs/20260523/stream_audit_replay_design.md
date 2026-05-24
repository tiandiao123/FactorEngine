# Streaming Data Audit / Replay / Reconciliation 设计思考

> 日期：2026-05-24
> 状态：设计草案
> 目标：为实时 websocket 数据采集与聚合增加落盘、回放、校准能力，支持事后核查聚合结果是否正确

---

## 1. 背景

当前 FactorEngine 的实时链路主要关注低延迟计算：

```text
Exchange WebSocket
  -> Collector
  -> Worker / Accumulator / Publisher
  -> ClockDriver
  -> InferenceEngine
  -> factor outputs
```

这条链路能实时产出 bars 和 factors，但缺少一个生产系统很关键的能力：

**事后无法完整回答：当时我们到底收到了什么数据、如何解析、如何聚合、为什么得到了这个 bar / factor。**

具体问题包括：

1. websocket 是否丢消息、断流、重复推送？
2. exchange raw message 到内部标准 trade/bar 的解析是否正确？
3. trades 聚合成 OHLCV / vwap / buy_vol / sell_vol 的逻辑是否正确？
4. ClockDriver 的 tick 边界是否造成 bucket 偏移？
5. 实时聚合结果和离线 replay 结果是否一致？
6. 实时 trades-derived bar 和交易所官方 kline 是否一致？

因此需要设计一个旁路的 **Audit / Replay / Reconciliation** 子系统。

---

## 2. 核心目标

### 2.1 必须支持

1. 采集过程中异步落盘，不阻塞实时 hot path。
2. 保存原始 websocket 消息，用于还原当时输入。
3. 保存标准化后的 event，用于核查解析逻辑。
4. 保存实时聚合后的 aggregate bar，用于和离线 replay 对比。
5. 支持按 symbol / exchange / stream / 时间范围回放。
6. 支持生成 reconciliation report，指出差异和可能原因。
7. 支持落盘统计：写入量、drop 数、flush 延迟、文件 rotate。

### 2.2 暂不追求

1. 完整事务级 exactly-once 存储。
2. 用 audit 写入反向阻塞 collector，保证一条不丢。
3. 第一版直接接入 Kafka / S3 / Iceberg。
4. 第一版支持所有数据源，包括 orderbook / funding。
5. 第一版在 C++ runtime 层记录每个 factor node 的中间值。

当前第一版目标应该是：**低风险旁路落盘 + 保存 online aggregate bars**。离线 replay / reconciliation 属于后续在 selected normalized trades 可用之后再做的能力。

---

## 3. 总体架构

### 3.1 实时链路保持不变

实时计算路径仍然是主路径：

```text
Exchange WS
  -> Collector
  -> Worker
  -> ClockDriver
  -> InferenceEngine
```

Audit 是旁路：

```text
Exchange WS
  -> Collector
       ├── realtime path
       │     -> Worker / Accumulator
       │
       └── audit path
             -> AuditQueue
             -> AuditWriter thread
             -> local files
```

ClockDriver 也可以旁路写 aggregate：

```text
ClockDriver tick
  -> merged_bars / merged_trades
       ├── bar_queue -> runtime
       └── AuditQueue -> AggregateLog
```

### 3.2 三层落盘

建议至少设计三层日志：

| 层级 | 内容 | 用途 |
|------|------|------|
| Raw WS Event Log | 交易所原始 websocket message | 还原输入、查断流/重复/字段变化 |
| Normalized Event Log | 内部标准化 trade/kline event | 核查解析和 symbol/side/time 映射 |
| Aggregate Bar Log | ClockDriver 实时聚合结果 | 和离线 replay / 官方 kline 对比 |

这三层解决的问题不同，不建议只保存最终 bar。

---

## 4. 为什么不能只保存 aggregate bar

如果只保存 ClockDriver 输出：

```text
bucket -> OHLCV / vwap / buy_vol / sell_vol
```

事后只能知道“当时结果是多少”，但无法知道：

1. 是 websocket 少了一条 trade？
2. 是 side 解析错？
3. 是 bucket 边界错？
4. 是 ClockDriver flush 太早？
5. 是交易所后来修正了历史数据？

因此至少需要保存 normalized event。Raw event 则用于追查更底层问题，比如交易所字段格式变化、collector parser bug。

---

## 5. Raw WS Event Log

### 5.1 内容

Raw log 尽量保存交易所原始消息，不做业务解释。

示例：

```json
{
  "recv_ts_ms": 1790000000123,
  "exchange": "binance",
  "stream": "aggTrade",
  "symbol": "BTC-USDT",
  "connection_id": "binance-trades-0",
  "sequence": 123456,
  "raw": {
    "e": "aggTrade",
    "E": 1790000000110,
    "s": "BTCUSDT",
    "a": 12345,
    "p": "65000.1",
    "q": "0.02",
    "m": false,
    "T": 1790000000100
  }
}
```

### 5.2 关键字段

| 字段 | 说明 |
|------|------|
| `recv_ts_ms` | 本机收到消息的时间 |
| `exchange` | `okx` / `binance` |
| `stream` | `trades` / `aggTrade` / `kline` |
| `symbol` | 内部统一 symbol |
| `connection_id` | 哪个 WS 连接收到 |
| `sequence` | 本地递增序号，便于排查写入顺序 |
| `raw` | 原始交易所 JSON |

Raw log 的目标不是高效查询，而是最大程度保留证据。

---

## 6. Normalized Event Log

### 6.1 Trades 标准事件

```json
{
  "recv_ts_ms": 1790000000123,
  "event_ts_ms": 1790000000100,
  "exchange": "binance",
  "stream": "aggTrade",
  "symbol": "BTC-USDT",
  "type": "trade",
  "trade_id": "12345",
  "price": 65000.1,
  "size": 0.02,
  "side": "buy",
  "is_aggregate": true
}
```

### 6.2 Kline 标准事件

```json
{
  "recv_ts_ms": 1790000060123,
  "event_ts_ms": 1790000060000,
  "exchange": "binance",
  "stream": "kline_1m",
  "symbol": "BTC-USDT",
  "type": "kline",
  "open": 65000.0,
  "high": 65100.0,
  "low": 64900.0,
  "close": 65050.0,
  "volume": 123.4,
  "quote_volume": 8020000.0,
  "confirmed": true
}
```

### 6.3 这层能核查什么

1. Binance `m` 字段是否正确映射成 `buy/sell`。
2. OKX symbol 是否正确保留 `-SWAP`。
3. event timestamp 和 recv timestamp 的延迟分布。
4. trade_id 是否重复。
5. 标准事件数量是否和 raw message 数量对应。

---

## 7. Aggregate Bar Log

### 7.1 内容

Aggregate log 保存 ClockDriver 每个 tick 推给 runtime 的结果。

```json
{
  "bucket_ts_ms": 1790000000000,
  "flush_ts_ms": 1790000001003,
  "exchange": "binance",
  "data_source": "trades",
  "symbol": "BTC-USDT",
  "open": 65000.0,
  "high": 65010.0,
  "low": 64990.0,
  "close": 65005.0,
  "volume": 10.5,
  "vwap": 65003.2,
  "buy_vol": 6.2,
  "sell_vol": 4.3,
  "trade_count": 128,
  "source": "online_clockdriver"
}
```

### 7.2 保存位置

Aggregate log 的写入点建议放在 `ClockDriver._flush_and_aggregate()` 之后：

```text
worker.flush()
  -> ClockDriver._aggregate()
  -> audit_writer.write_aggregate(merged_bars, merged_trades)
  -> bar_queue.put(...)
```

注意：audit 写入必须异步排队，不应阻塞 `bar_queue.put()`。

---

## 8. 时间口径

这是最关键的设计点。

### 8.1 三种时间

| 时间 | 来源 | 用途 |
|------|------|------|
| `event_ts_ms` | 交易所事件时间 | replay 聚合的主时间 |
| `recv_ts_ms` | 本机收到 WS 时间 | 延迟、断流、clock skew 分析 |
| `flush_ts_ms` | ClockDriver flush 时间 | 实时系统行为分析 |

### 8.2 推荐 bucket 标准

离线 replay 和实时 trades 聚合都应尽量使用同一个 bucket 语义：

```text
[bucket_start_ms, bucket_start_ms + freq_ms)
```

例如 1s：

```text
[10:00:00.000, 10:00:01.000)
[10:00:01.000, 10:00:02.000)
```

第一版建议：

- trades 聚合归桶优先使用 `event_ts_ms`。
- 落盘同时保存 `recv_ts_ms`。
- 如果 `recv_ts_ms - event_ts_ms` 超过阈值，记录 late/skew 指标。
- ClockDriver 可以继续按本机 wall clock flush，但 replay 对比时按 `event_ts_ms` 重建 bucket。

### 8.3 需要明确的边界规则

必须写进代码和测试：

```text
bucket_start_ms = event_ts_ms - (event_ts_ms % freq_ms)
```

边界事件：

```text
event_ts = 10:00:01.000
```

应归入：

```text
[10:00:01.000, 10:00:02.000)
```

而不是上一秒。

---

## 9. Replay 设计

### 9.1 Replay 输入

完整 replay 能力可以从两种输入开始：

1. Normalized Event Log：推荐后续 selected symbols / debug window 使用。
2. Raw WS Event Log：用于 parser 也要重放的高级校验。

后续 replay trades：

```text
NormalizedTradeLog
  -> sort by event_ts_ms, sequence
  -> bucket by event_ts_ms
  -> OfflineTradesAccumulator
  -> OfflineAggregateBar
```

### 9.2 Replay 输出

```json
{
  "bucket_ts_ms": 1790000000000,
  "exchange": "binance",
  "symbol": "BTC-USDT",
  "open": 65000.0,
  "high": 65010.0,
  "low": 64990.0,
  "close": 65005.0,
  "volume": 10.5,
  "vwap": 65003.2,
  "buy_vol": 6.2,
  "sell_vol": 4.3,
  "trade_count": 128,
  "source": "offline_replay"
}
```

### 9.3 Replay 和线上聚合对比

对比键：

```text
(exchange, data_source, symbol, bucket_ts_ms)
```

对比字段：

```text
open
high
low
close
volume
vwap
buy_vol
sell_vol
trade_count
```

容忍度：

```python
price_abs_tol = 1e-8
volume_abs_tol = 1e-8
vwap_abs_tol = 1e-8
```

输出 summary：

```json
{
  "total_buckets": 3600,
  "matched": 3598,
  "missing_online": 1,
  "missing_replay": 0,
  "value_mismatch": 1,
  "max_close_diff": 0.0,
  "max_volume_diff": 0.02
}
```

---

## 10. 和交易所历史数据校准

你提到“从线上拉 websocket 的历史记录和实时聚合比对”，这里需要拆开理解。

大多数交易所不会提供完整 websocket 历史记录，但会提供 REST 历史数据，例如：

- historical trades
- aggTrades
- official klines

因此可以做三种校验。

### 10.1 WS Raw/Normalized vs REST Historical Trades

目的：检查我们 websocket 是否漏消息或重复消息。

```text
REST historical trades
  vs
NormalizedTradeLog
```

可发现：

1. WS 断流。
2. subscribe 漏 symbol。
3. collector 重连期间缺数据。
4. trade_id 缺口或重复。

### 10.2 Offline Replay vs Online Aggregate

目的：检查我们实时聚合实现是否正确。

```text
NormalizedTradeLog -> Offline Replay Aggregate
  vs
ClockDriver AggregateLog
```

可发现：

1. `TradesAccumulator` bug。
2. ClockDriver tick 边界 bug。
3. forward-fill 逻辑和 replay 不一致。
4. online flush 时机导致 bucket 混入错误数据。

### 10.3 Trades-derived Bar vs Official Kline

目的：检查我们的 trades-derived OHLCV 和交易所官方 kline 口径差异。

```text
Offline Replay Aggregate from trades
  vs
Exchange official kline
```

注意：这两个不一定完全一致。原因可能包括：

1. Binance `aggTrade` 和 raw trade 口径不同。
2. 交易所 kline 使用撮合内部数据，REST trades 有分页/聚合限制。
3. volume 单位不同。
4. bucket 边界或时区口径不同。
5. 官方 kline 可能有修正。

因此官方 kline 对比更适合作为 sanity check，不应默认要求 bitwise equal。

---

## 11. 落盘格式

### 11.1 第一版建议：JSONL + zstd

优点：

1. 简单。
2. 方便手工排查。
3. 一行一事件，适合 append。
4. 压缩后体积可控。
5. 不引入复杂依赖。

目录结构：

```text
data/audit/
  raw_ws/
    exchange=binance/
      stream=aggTrade/
        date=2026-05-24/
          hour=10/
            part-000001.jsonl.zst
  normalized/
    type=trade/
      exchange=binance/
        date=2026-05-24/
          hour=10/
            part-000001.jsonl.zst
  aggregate/
    exchange=binance/
      data_source=trades/
        freq=1s/
          date=2026-05-24/
            hour=10/
              part-000001.jsonl.zst
```

### 11.2 第二版：Parquet

当数据量大、需要批量查询时，后台 compaction：

```text
jsonl.zst -> hourly parquet
```

Parquet 更适合：

1. 按 symbol/time 查询。
2. pandas / duckdb 分析。
3. 长期归档。
4. reconciliation 批处理。

建议不要第一版直接强依赖 parquet，因为 writer 会复杂很多。

---

## 12. AuditWriter 设计

### 12.1 异步旁路写

关键原则：

**audit 不能阻塞实时链路。**

```text
Collector thread:
  audit_queue.put_nowait(event)
  realtime_process(event)

AuditWriter thread:
  event = audit_queue.get()
  buffered_write(event)
  rotate_if_needed()
  flush_periodically()
```

### 12.2 队列满时怎么办

如果 audit queue 满了，第一版建议：

1. drop audit event。
2. `audit_drop_count += 1`。
3. 每 N 次 drop 打 warning。
4. 不阻塞 collector。

原因：实时系统优先级高于审计系统。如果为了落盘阻塞 WS 消费，会反过来造成真实数据处理延迟甚至断流。

### 12.3 Writer 配置

```python
AuditConfig(
    enabled=True,
    root_dir="data/audit",
    write_raw=True,
    write_normalized=True,
    write_aggregate=True,
    queue_size=100_000,
    flush_interval_seconds=1.0,
    rotate_interval_seconds=3600,
    compression="zstd",
)
```

### 12.4 Writer stats

```python
{
    "enabled": true,
    "queued": 1200,
    "written": 123456789,
    "dropped": 0,
    "last_flush_ts": 1790000000000,
    "current_files": 3,
    "write_errors": 0
}
```

---

## 13. 建议目录结构

```text
dataflow/audit/
  __init__.py
  schema.py          # RawWsEvent, NormalizedTradeEvent, AggregateBarEvent
  writer.py          # AuditWriter, file rotation, buffering
  hooks.py           # collector/clockdriver audit hooks
  replay.py          # replay normalized events into offline aggregates
  reconcile.py       # compare online aggregate vs replay/rest
  storage.py         # path layout, jsonl/zstd helpers
```

对应测试：

```text
tests/audit/
  test_writer.py
  test_schema.py
  test_replay_trades.py
  test_reconcile.py
  test_audit_integration.py
```

---

## 14. 接入点设计

### 14.1 Collector 接入 Raw / Normalized

collector 收到 raw message：

```python
if audit is not None:
    audit.write_raw(exchange, stream, symbol, raw, recv_ts_ms)
```

collector parse 成内部 trade：

```python
if audit is not None:
    audit.write_normalized_trade(event)
```

然后继续原实时路径：

```python
on_trade(symbol, price, size, side, event_ts_ms)
```

### 14.2 ClockDriver 接入 Aggregate

```python
merged_bars, merged_trades = self._aggregate(raw)

if self._audit is not None:
    self._audit.write_aggregate(
        exchange=self._exchange,
        data_source=self._data_source,
        freq_seconds=self._freq,
        bars=merged_bars,
        trades=merged_trades,
    )

self._bar_queue.put(payload, block=False)
```

### 14.3 Engine API

```python
engine = Engine(
    symbols=["BTC-USDT", "ETH-USDT"],
    exchange="binance",
    data_source="trades",
    factor_group="okx_perp_trades",
    audit_config=AuditConfig(
        enabled=True,
        root_dir="data/audit",
        write_raw=True,
        write_normalized=True,
        write_aggregate=True,
    ),
)
```

状态查询：

```python
engine.audit_stats
```

---

## 15. Reconciliation Report

### 15.1 命令行示例

```bash
python -m dataflow.audit.reconcile \
  --audit-root data/audit \
  --exchange binance \
  --data-source trades \
  --symbols BTC-USDT,ETH-USDT \
  --freq 1s \
  --start "2026-05-24T10:00:00Z" \
  --end "2026-05-24T11:00:00Z"
```

### 15.2 输出示例

```text
Reconciliation Report
exchange: binance
data_source: trades
freq: 1s
range: 2026-05-24T10:00:00Z ~ 2026-05-24T11:00:00Z

BTC-USDT:
  total buckets:      3600
  matched:            3598
  missing online:     1
  missing replay:     0
  value mismatches:   1
  max volume diff:    0.02000000
  max vwap diff:      0.00000000

Top mismatches:
  2026-05-24T10:12:31Z volume online=10.50 replay=10.52 diff=0.02
  2026-05-24T10:40:00Z missing online bucket
```

### 15.3 差异分类

| 类型 | 含义 |
|------|------|
| `missing_online` | replay 有，线上 aggregate 没有 |
| `missing_replay` | 线上 aggregate 有，replay 没有 |
| `value_mismatch` | 两边都有，但字段值不一致 |
| `late_event` | event_ts 属于旧 bucket，但 recv_ts 晚到 |
| `duplicate_trade` | 同一 trade_id 重复 |
| `clock_skew` | recv_ts 和 event_ts 偏差过大 |

---

## 16. Rollback / Backfill 语义

这里的 “rollback” 不建议理解为实时系统回滚状态。实时系统状态回滚会非常复杂，尤其 C++ rolling window 已经前进。

建议第一版定义为：

1. **Replay**：用历史事件重新生成 aggregate。
2. **Reconcile**：对比 replay aggregate 和 online aggregate。
3. **Backfill**：如果 online aggregate 确认错误，将 offline aggregate 写入离线数据集或报告。
4. **No live rollback**：不尝试修改已经发给策略的历史 factor output。

如果未来需要线上修正，可以设计独立的 correction stream：

```json
{
  "type": "aggregate_correction",
  "symbol": "BTC-USDT",
  "bucket_ts_ms": 1790000000000,
  "old": {...},
  "new": {...},
  "reason": "replay_reconciliation"
}
```

但这属于后续更高阶能力。

---

## 17. 实施阶段

### Phase 0：文档和口径确定

- 明确 bucket 边界。
- 明确 event_ts / recv_ts / flush_ts 使用方式。
- 明确 forward-fill 是否写入 aggregate log。
- 明确 replay 是否复用 `TradesAccumulator` 还是单独实现 offline accumulator。

### Phase 1：AggregateLog

先从 ClockDriver 写 aggregate log。

优点：

1. 接入点少。
2. 风险低。
3. 可以先观察线上 bar 输出。

缺点：

1. 还不能解释聚合为何如此。
2. 不能重放。

### Phase 2：Selected NormalizedTradeLog

在 trades collector / worker 侧对 selected symbols 或 debug window 落标准 trade event。

然后实现：

```text
NormalizedTradeLog -> OfflineReplayAggregate
```

这一步完成后，就可以校验实时聚合是否正确。

### Phase 3：Replay + Reconcile

实现 `dataflow.audit.replay` 和 `dataflow.audit.reconcile`。

最小可用能力：

```text
读取 normalized trades
生成 offline aggregate
读取 online aggregate
按 bucket 对比
输出 report
```

### Phase 4：Raw WS Event Log

保存 raw message，用于更底层 parser/collector 排查。

### Phase 5：REST Historical 校准

接入交易所 REST historical trades / klines：

```text
REST trades vs NormalizedTradeLog
REST kline vs OfflineReplayAggregate
```

---

## 18. 测试计划

### 18.1 AuditWriter

- 写 jsonl。
- rotate 文件。
- queue 满时 drop 并计数。
- writer 异常不影响主流程。
- flush/stop 能写完剩余 buffer。

### 18.2 Replay

- 单 symbol trades 聚合 OHLCV。
- 多 symbol 按 bucket 聚合。
- 边界时间归桶。
- vwap 计算。
- buy_vol/sell_vol 计算。
- 空 bucket forward-fill 口径。

### 18.3 Reconcile

- 完全一致。
- missing online。
- missing replay。
- 单字段 mismatch。
- 浮点 tolerance。
- 输出 summary 和 top mismatches。

### 18.4 集成测试

- fake collector -> audit normalized -> accumulator -> aggregate log。
- replay normalized 后与 aggregate log 一致。
- audit queue 满不阻塞 fake collector。

---

## 19. 当前推荐第一版最小方案

当前决策是：第一版只保存 ClockDriver 聚合后的 aggregate bars，不默认保存逐笔 normalized trades。

```text
AuditConfig
  -> AuditWriter(JSONL, async queue)
  -> ClockDriver AggregateLog
```

约束：

1. 只在 `ClockDriver._flush_and_aggregate()` 后落盘。
2. 不修改 trades collector / worker。
3. 不保存逐笔 normalized trades。
4. 不做 replay / reconcile。
5. 先用本地 `jsonl`，后续再做 zstd/parquet。
6. audit queue 满时 drop audit event，不阻塞实时路径。

原因：

1. 聚合信息体量远小于逐笔 trades。
2. 当前首要需求是事后知道线上实际推给 runtime 的 bar 是什么。
3. 逐笔 trades 可以作为后续 selected symbols / debug window 能力，而不是默认开启。

---

## 20. 一句话结论

实时流式系统需要一条旁路的审计链路。完整形态可以保存 normalized events 和 online aggregates，并用离线 replay 做校准；但当前第一版先只保存 online aggregate bars。这样数据体量可控、改动最小，并且能先回答“线上当时实际推给 runtime 的聚合结果是什么”。
