# Exchange-Time Trades Aggregation 开发规划

> 日期：2026-05-27
> 状态：开发路线图
> 关联设计：
> - `docs/20260526/exchange_time_trades_aggregation_allowed_lateness.md`
> - `docs/20260526/exchange_time_trades_aggregation_development_plan.md`
>
> 目标：按照可验证、可回滚、默认兼容的顺序，实现 trades 的 exchange-time 分桶与 `allowed_lateness`。

---

## 1. 总体原则

本次开发不要从 `Engine` 或 `ClockDriver` 开始，而是先把最核心、最容易出错的时间语义做成独立组件并用单元测试锁住。

需要严格区分三种时间：

```text
trade_ts_ms    = 交易所成交时间，用于决定进入哪个 bucket
receive_ts_ms  = 本地收到 trade 的时间，用于判断是否超过 allowed_lateness
now_ms         = ClockDriver tick 时间，用于判断哪些 bucket 成熟可 flush
```

开发原则：

```text
默认保持 receive_time 行为不变
exchange_time 必须显式开启
每一步都要有单测或集成测试证明语义
先实现 data_source="trades"，暂不默认开放 kline_trades + exchange_time
不做 runtime rollback，超迟到 trade 第一版 drop + metrics
```

---

## 2. 推荐开发顺序

### Step 1：实现 EventTimeTradesAccumulator

这是第一步，也是最重要的一步。

修改文件：

```text
dataflow/trades/accumulator.py
```

新增测试：

```text
tests/dataflow/trades/test_event_time_accumulator.py
```

交付物：

```text
TradeBucket
TradeBucketSnapshot
EventTimeTradesAccumulator.update(...)
EventTimeTradesAccumulator.flush_ready(...)
EventTimeTradesAccumulator.pop_ready_bucket(...)
```

必须覆盖的语义：

```text
trade_ts_ms 决定 bucket_start_ms
receive_ts_ms 决定 late-drop
now_ms 决定 mature flush
同一 symbol 内允许同时存在多个 bucket
指定 bucket flush 后从 map 删除
超迟到 trade 不重建已删除 bucket
```

接口语义调整：

```text
EventTimeTradesAccumulator 内部仍维护 _buckets: dict[bucket_start_ms, TradeBucket]
但第一版不要求 accumulator 每次主动吐出所有 mature bucket
TradesWorker 会维护全局 target_bucket_start_ms
accumulator 需要支持按指定 bucket_start_ms pop 一个 ready bucket
```

推荐新增：

```python
def pop_ready_bucket(
    self,
    bucket_start_ms: int,
    now_ms: int,
    *,
    active: bool = True,
) -> TradeBucketSnapshot | None:
    ...
```

语义：

```text
如果 bucket_start_ms + freq_ms > now_ms - allowed_lateness_ms:
  返回 None，target bucket 尚未 mature

如果真实 bucket 存在:
  pop -> snapshot -> 更新 last_close -> 返回 snapshot

如果真实 bucket 不存在，但 active=True 且 last_close 已知:
  返回 forward-fill snapshot

如果真实 bucket 不存在，且无 last_close 或 inactive:
  返回 None
```

验收标准：

```text
所有新增 accumulator 单测通过
不修改现有 TradesAccumulator 默认行为
现有 receive-time 相关测试不受影响
```

---

### Step 2：接入 OKX / Binance TradesWorker

在 accumulator 语义稳定后，再把它接入两个 trades worker。

修改文件：

```text
dataflow/okx/trades/worker.py
dataflow/binance/trades/worker.py
```

新增配置：

```python
trade_time_mode: str = "receive_time"
allowed_lateness_ms: int = 1000
freq_seconds: float | None = None
```

核心改动：

```text
receive_time:
  保持 acc.update(px, sz, side) + snapshot_and_reset()

exchange_time:
  _on_trade 使用 collector 传入的 ts_ms 作为 trade_ts_ms
  _on_trade 记录 receive_ts_ms = int(time.time() * 1000)
  acc.update(px, sz, side, trade_ts_ms, receive_ts_ms)
  flush(now_ms) 按 worker 的 target_bucket_start_ms 返回一根 finalized bucket
```

#### Step 2.1：TradesWorker 作为 bucket synchronizer

第一版不让每个 symbol 的 accumulator 自己决定“本轮吐哪些 bucket”。否则如果某个 symbol 在不同 ClockDriver flush 中补发同一个 `bucket_ts_ms`，可能导致同一时间桶被拆成两批 payload。

因此 exchange-time 模式下，`TradesWorker` 需要维护一个全局游标：

```python
self._target_bucket_start_ms: int | None
```

含义：

```text
当前 worker 准备 finalize 的 exchange-time bucket
所有 active symbols 都围绕这个 target bucket 取数据
每次 worker 成功返回后，target_bucket_start_ms += freq_ms
```

推荐流程：

```text
TradesWorker.flush(now_ms):
  1. 若 target_bucket_start_ms 尚未初始化：
       用当前 tick 推导第一个 mature target，或等第一批 trade 初始化

  2. 判断 target bucket 是否 mature：
       target_bucket_end_ms <= now_ms - allowed_lateness_ms
       不 mature 则返回 None / 空结果

  3. 固定 active_symbols 快照

  4. 对每个 symbol:
       snap = acc.pop_ready_bucket(target_bucket_start_ms, now_ms, active=True)
       有 snap 则加入 bars/trades
       无 snap 则该 symbol 本轮缺席

  5. 若 bars/trades 非空:
       返回 (target_bucket_start_ms, bars, trades)
       然后 target_bucket_start_ms += freq_ms

  6. 若 bars/trades 为空:
       仍然推进 target_bucket_start_ms，避免一直卡在无数据 bucket
       但不向 ClockDriver 返回 payload
```

这个设计的关键 invariant：

```text
同一个 bucket_ts_ms 最多被 TradesWorker emit 一次
后续才到达该 bucket 的 trade 只能 late-drop 或 correction audit，不能补发第二个 runtime payload
ClockDriver 收到的永远是 worker 已经 finalized 的完整 bucket round
```

正常路径：

```text
ClockDriver 每 tick 一次
TradesWorker 每次 finalize 一个 target bucket
bar_queue 每次收到一个 bucket payload
```

异常卡顿路径：

```text
如果 ClockDriver 卡顿，target bucket 可能积压
第一版每次 flush 只补一个 target bucket
这样会增加追赶延迟，但能保证不会把同一个 bucket_ts_ms split 成多批 payload
```

验收标准：

```text
receive_time 默认路径行为不变
exchange_time 路径确认 ts_ms 不再被丢弃
OKX / Binance worker 返回结构一致
late-drop 计数能从 worker stats 看到
同一个 bucket_ts_ms 不会被 worker emit 两次
```

---

### Step 3：改造 ClockDriver 支持 tick now_ms 与 finalized bucket 输出

worker 接入后，再处理统一 flush 驱动。

修改文件：

```text
dataflow/clock_driver.py
```

核心改动：

```text
_flush_and_aggregate() 获取 now_ms
调用 worker.flush(now_ms)
兼容旧返回格式
识别 event-time 返回的 (bucket_ts_ms, bars, trades)
对 finalized bucket 写 audit / bar_queue / after_flush_commit
```

第一版策略：

```text
旧 receive-time worker 返回格式不变
event-time trades worker 第一版每次 flush 输出 0 / 1 个 finalized bucket
每个 finalized bucket 单独 build payload
每个 finalized bucket 单独调用 after_flush_commit(bucket_ts_ms)
```

验收标准：

```text
旧 ClockDriver 单 tick payload 测试继续通过
event-time worker 返回 None / 空结果时不写 bar_queue
event-time worker 返回一个 finalized bucket 时，bar_queue payload 使用该 bucket_ts_ms
bucket_ts_ms 不从任意 bar 猜，而是使用 event-time worker 返回的 bucket_ts_ms
```

---

### Step 4：接入 Engine 配置

ClockDriver 能处理 event-time 后，再开放 Engine 参数。

修改文件：

```text
factorengine/engine.py
```

新增参数：

```python
trade_time_mode: str = "receive_time"
allowed_lateness_ms: int = 1000
```

第一版限制：

```text
data_source="trades" + trade_time_mode="exchange_time" 允许
data_source="kline" 忽略 trade_time_mode
data_source="kline_trades" + trade_time_mode="exchange_time" 暂时抛 ValueError
```

原因：

```text
kline worker 输出的是当前 tick bucket
exchange-time trades worker 输出的是 allowed_lateness 之前的 mature bucket
两者 bucket_ts_ms 可能不同
第一版不能隐式把不同时间的 kline 与 trades 合并
```

验收标准：

```text
默认 Engine 初始化行为兼容
trades + exchange_time 可以启动
kline_trades + exchange_time 有明确错误信息
OKX / Binance 参数传递一致
```

---

### Step 5：补充 Audit / Metrics

核心功能跑通后，再扩展观测字段。

建议字段：

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

第一版可以先把字段放进 `round_trades[sym]`，后续再决定是否扩展 audit writer schema。

验收标准：

```text
能通过 stats / audit 看到 late-drop 情况
verifier 后续可以区分 receive_time 与 exchange_time bucket
```

---

### Step 6：集成测试与示例

最后补 end-to-end 入口，证明完整链路可用。

建议新增或更新：

```text
examples/engine/engine_rt_trades_exchange_time.py
tests/factorengine/test_engine_exchange_time_trades.py
```

验收标准：

```text
Engine(data_source="trades", trade_time_mode="exchange_time") 能跑通
runtime payload 结构与现有 _runtime_loop 兼容
bar[0] 使用 exchange bucket_start_ms
默认 receive_time 示例不变
```

---

## 3. Step 1 详细计划

第一步只做 accumulator，不接 worker。

原因：

```text
分桶、迟到、flush 是最核心语义
这些逻辑可以完全脱离网络和 Engine 测试
先把边界用单测锁死，后续接线风险小
```

### 3.1 数据结构

```text
EventTimeTradesAccumulator
  symbol 维度外部管理，一个 symbol 一个 accumulator
  内部维护 _buckets: dict[int, TradeBucket]

TradeBucket
  一个 exchange-time bucket 的 O(1) 聚合状态
```

整体结构：

```text
TradesWorker
  _accumulators: dict[symbol, EventTimeTradesAccumulator]

BTC-USDT -> EventTimeTradesAccumulator
              _buckets[10:00:00.000] -> TradeBucket
              _buckets[10:00:01.000] -> TradeBucket

ETH-USDT -> EventTimeTradesAccumulator
              _buckets[10:00:00.000] -> TradeBucket
```

### 3.2 第一批单测

必须先写这些测试：

```text
test_update_assigns_bucket_by_trade_ts_not_receive_ts
test_late_trade_within_allowed_lateness_is_accepted
test_trade_after_allowed_lateness_is_dropped
test_flush_ready_emits_only_mature_buckets
test_flush_ready_emits_multiple_buckets_in_order
test_out_of_order_trades_fix_open_and_close_by_trade_ts
test_forward_fill_empty_bucket_after_last_close
test_no_empty_bucket_before_first_trade
test_emitted_bucket_is_deleted_and_late_trade_does_not_recreate_it
```

### 3.3 Step 1 完成定义

Step 1 完成需要同时满足：

```text
EventTimeTradesAccumulator API 稳定
新增单测全部通过
现有 TradesAccumulator 测试全部通过
没有修改 worker / ClockDriver / Engine 行为
```

---

## 4. 风险与控制

### 风险 1：把 receive_ts_ms 用来分桶

后果：

```text
实现退化为 receive-time 聚合，和官方 kline 仍不对齐
```

控制：

```text
单测必须覆盖本地 10:00:01.xxx 收到 trade_ts=10:00:00.990 的场景
```

### 风险 2：flush 后迟到 trade 重建旧 bucket

后果：

```text
audit / runtime 已输出的 bar 被静默修改，状态不一致
```

控制：

```text
update() 入口必须先做 late-drop，再 setdefault bucket
```

### 风险 3：kline_trades 隐式错合并

后果：

```text
同一个 payload 里 kline 是当前 bucket，trades 是 mature old bucket
因子输入时间错位
```

控制：

```text
第一版禁止 kline_trades + exchange_time
后续实现 bucket_ts_ms-aware aggregation 后再开放
```

### 风险 4：空桶无限生成

后果：

```text
从未有 trade 的 symbol 也持续输出 NaN bucket
```

控制：

```text
_next_emit_bucket_start_ms 在第一笔 trade 后才初始化
last_close 为 NaN 时不生成 forward-fill snapshot
```

---

## 5. 执行节奏

建议每一步单独 commit：

```text
commit 1: add event-time trades accumulator
commit 2: wire event-time trades workers
commit 3: support event-time flush rounds in ClockDriver
commit 4: expose Engine exchange-time trades config
commit 5: add audit metrics and examples
```

每个 commit 的要求：

```text
只改当前层相关文件
保留默认 receive-time 兼容
新增或更新对应测试
不要同时重构无关模块
```

---

## 6. 当前结论

下一步应开始 Step 1：

```text
实现 EventTimeTradesAccumulator
新增 tests/dataflow/trades/test_event_time_accumulator.py
只验证 accumulator 语义，不接 worker
```

这个顺序可以把最复杂的时间语义先独立验证，再逐层接入 worker、ClockDriver 和 Engine。
