# Exchange-Time Trades Engine 工作原理教学文档

> 日期：2026-05-28
> 面向读者：需要理解或使用 `trade_time_mode="exchange_time"` 的开发者 / 策略研究者
> 相关代码：
> - `factorengine/engine.py`
> - `dataflow/trades/accumulator.py`
> - `dataflow/okx/trades/worker.py`
> - `dataflow/binance/trades/worker.py`
> - `dataflow/clock_driver.py`

---

## 1. 这套机制解决什么问题

旧的 trades 聚合是 receive-time 语义：

```text
trade 什么时候到本地
  -> 就进入当前本地 accumulator
```

这会在秒边界附近产生偏差：

```text
exchange trade time = 10:00:00.999
local receive time  = 10:00:01.020
```

旧 receive-time 模式下，这笔 trade 很可能进入 `10:00:01` bucket。

exchange-time 模式下，它会进入：

```text
bucket[10:00:00.000, 10:00:01.000)
```

原因是分桶只看交易所成交时间 `trade_ts_ms`，不看本地收到时间。

---

## 2. 如何开启

第一版只支持 trades-only：

```python
from factorengine.engine import Engine

engine = Engine(
    symbols=["BTC-USDT"],
    exchange="binance",
    mode="live",
    data_source="trades",
    data_freq="1s",
    factor_group="okx_perp_trades",
    trade_time_mode="exchange_time",
    allowed_lateness_ms=1000,
)
```

OKX 示例：

```python
engine = Engine(
    symbols=["BTC-USDT-SWAP"],
    exchange="okx",
    mode="live",
    data_source="trades",
    data_freq="1s",
    factor_group="okx_perp_trades",
    trade_time_mode="exchange_time",
    allowed_lateness_ms=1000,
)
```

当前限制：

```text
data_source="trades" + trade_time_mode="exchange_time" 允许
data_source="kline" + trade_time_mode="exchange_time" 不影响 kline
data_source="kline_trades" + trade_time_mode="exchange_time" 第一版禁止
```

禁止 `kline_trades + exchange_time` 的原因：

```text
kline worker 输出的是当前 wall-clock tick 的 kline bucket
exchange-time trades worker 输出的是已经过 allowed_lateness 的 mature bucket
两者 bucket_ts_ms 可能不同
第一版避免把不同时间的 kline 和 trades 隐式合并
```

---

## 3. 三个时间必须分清

exchange-time engine 里有三个时间：

```text
trade_ts_ms
  交易所成交时间
  用于决定 trade 属于哪个 bucket

receive_ts_ms
  本地收到 trade 并进入 worker callback 的时间
  用于判断 trade 是否超过 allowed_lateness

now_ms
  ClockDriver 当前 tick 的本地时间
  用于判断 target bucket 是否 mature，可以输出
```

不要混用它们。

正确关系：

```text
trade_ts_ms   -> bucket_start_ms
receive_ts_ms -> late-drop
now_ms        -> flush / mature
```

---

## 4. 总体数据流

完整链路：

```text
Exchange WebSocket
  -> Collector parses trade: symbol, px, sz, side, trade_ts_ms
  -> TradesWorker._on_trade(...)
  -> EventTimeTradesAccumulator.update(...)
  -> TradeBucket.update(...)

ClockDriver tick
  -> TradesWorker.flush(now_ms)
  -> acc.pop_ready_bucket(target_bucket_start_ms, now_ms)
  -> TradesWorker builds bars/trades for target bucket
  -> ClockDriver writes audit
  -> ClockDriver puts payload into bar_queue
  -> Engine._runtime_loop consumes payload
  -> C++ runtime push_bars
```

职责边界：

```text
EventTimeTradesAccumulator:
  管单个 symbol 的 bucket map

TradesWorker:
  管所有 symbols
  管全局 target_bucket_start_ms
  保证同一个 bucket_ts_ms 最多 emit 一次

ClockDriver:
  统一调 worker.flush(now_ms)
  统一写 audit
  统一塞 bar_queue

Engine:
  暴露配置
  管 runtime loop
```

---

## 5. EventTimeTradesAccumulator：单 symbol 的 bucket map

每个 symbol 有一个 accumulator：

```text
TradesWorker
  _accumulators = {
    "BTC-USDT": EventTimeTradesAccumulator(...),
    "ETH-USDT": EventTimeTradesAccumulator(...),
  }
```

每个 accumulator 内部有自己的 bucket map：

```text
EventTimeTradesAccumulator
  _buckets = {
    10:00:00.000 -> TradeBucket,
    10:00:01.000 -> TradeBucket,
  }
```

结构是：

```text
symbol -> EventTimeTradesAccumulator -> bucket_start_ms -> TradeBucket
```

不是所有 symbols 共用一个大 bucket map。

### 5.1 update 做什么

`EventTimeTradesAccumulator.update(...)` 负责：

```text
1. 用 trade_ts_ms 计算 bucket_start_ms
2. 用 receive_ts_ms 判断是否 late-drop
3. 找到或创建 TradeBucket
4. 调 TradeBucket.update(...) 做流式 O(1) 聚合
```

分桶公式：

```text
bucket_start_ms = trade_ts_ms - (trade_ts_ms % freq_ms)
bucket_end_ms   = bucket_start_ms + freq_ms
```

late-drop 条件：

```text
receive_watermark_ms = receive_ts_ms - allowed_lateness_ms

if bucket_end_ms <= receive_watermark_ms:
    drop
```

### 5.2 TradeBucket.update 是流式聚合

`TradeBucket` 不保存 raw trades list。

每来一笔 trade，只做 O(1) 更新：

```text
sum_pv += px * sz
sum_v += sz
buy_vol / sell_vol += sz
high / low 更新
open / close 按 trade_ts_ms 更新
trade_count += 1
```

这样内存不会随着 bucket 内 trade 数增长。

---

## 6. TradesWorker：全局 target bucket 同步器

这是 exchange-time 模式最重要的一层。

如果直接让每个 symbol 的 accumulator 自己吐 ready buckets，可能出现同一个 bucket 被拆成两次 payload 的风险：

```text
ClockDriver flush #1:
  A 返回 11:01
  B 返回 11:01
  C 没返回 11:01

ClockDriver flush #2:
  C 又返回 11:01

结果：
  11:01 被 split 成两批 payload
```

为了避免这种情况，`TradesWorker` 维护一个全局游标：

```python
self._target_bucket_start_ms
```

含义：

```text
当前 worker 准备 finalize 的 exchange-time bucket
```

每次 `TradesWorker.flush(now_ms)`：

```text
1. 看 target bucket 是否 mature
2. 如果未 mature，返回 None
3. 如果已 mature，遍历所有 active symbols
4. 对每个 symbol 调 acc.pop_ready_bucket(target_bucket_start_ms, now_ms)
5. 把拿到的 snapshots 组装成 bars/trades
6. target_bucket_start_ms += freq_ms
7. 返回 (bucket_ts_ms, bars, trades)
```

这样保证：

```text
同一个 bucket_ts_ms 最多被 TradesWorker emit 一次
后续才到达这个 bucket 的 trade 只能 drop，不会补发第二批 runtime payload
```

### 6.1 为什么不是 accumulator 主动 pop

`EventTimeTradesAccumulator` 没有线程，也没有 timer。

它不会主动在某个时间点 pop bucket。

真正触发 flush 的是：

```text
ClockDriver tick
  -> TradesWorker.flush(now_ms)
      -> acc.pop_ready_bucket(...)
```

所以“什么时候输出某个 bucket”由 `ClockDriver + TradesWorker` 决定，不由 accumulator 自己决定。

---

## 7. allowed_lateness 如何影响输出

设：

```text
freq_ms = 1000
allowed_lateness_ms = 500
target_bucket = 10:00:00
target_bucket_end = 10:00:01
```

只有当：

```text
target_bucket_end <= now_ms - allowed_lateness_ms
```

这个 target bucket 才 mature。

例子：

```text
Clock tick 10:00:01.000:
  watermark = 10:00:00.500
  10:00:00 bucket end = 10:00:01.000
  not mature

Clock tick 10:00:02.000:
  watermark = 10:00:01.500
  10:00:00 bucket end = 10:00:01.000
  mature
```

所以 `10:00:00` bucket 会在 `10:00:02` 这一轮被输出。

代价：

```text
实时信号会晚 allowed_lateness + 最多一个 ClockDriver tick
```

收益：

```text
边界迟到 trade 能进入正确的 exchange-time bucket
```

---

## 8. ClockDriver 如何处理 finalized bucket

exchange-time trades worker 返回：

```python
(bucket_ts_ms, bars, trades)
```

或者：

```python
None
```

`ClockDriver` 每次 tick：

```text
1. now_ms = int(time.time() * 1000)
2. 调 worker.flush(now_ms)
3. 如果返回 None，不写 queue
4. 如果返回 (bucket_ts_ms, bars, trades):
     - 写 audit
     - build payload
     - bar_queue.put(payload)
     - after_flush_commit(bucket_ts_ms)
```

重要点：

```text
bar_queue 只能由 ClockDriver 写
TradesWorker 不碰 bar_queue
Accumulator 不知道 audit / queue / runtime
```

这样 audit、queue full 统计、dynamic symbol commit 都集中在 ClockDriver。

---

## 9. Engine._runtime_loop 如何消费

`ClockDriver` 最终塞进 `bar_queue` 的 payload 仍兼容现有 runtime：

```text
(round_bars, round_trades)
```

其中：

```text
round_bars:
  symbol -> ndarray([ts, open, high, low, close, volume, nan, nan])

round_trades:
  symbol -> dict(vwap, buy_vol, sell_vol, trade_count, ...)
```

`Engine._runtime_loop` 会：

```text
1. 从 bar_queue 取 payload
2. 解析 round_bars / round_trades
3. 计算 ret
4. 组装 rt.BarData
5. push_bars 到 C++ runtime
6. get_all_outputs
7. 写入 signal_deque
```

因此 exchange-time 模式不会改变 factor runtime 的消费接口。

---

## 10. 一个完整例子

参数：

```text
freq = 1s
allowed_lateness = 500ms
symbols = A, B, C
```

trade 到达：

```text
A trade_ts=10:00:00.100 receive=10:00:00.120 -> bucket[10:00:00]
B trade_ts=10:00:00.900 receive=10:00:01.100 -> bucket[10:00:00]
C trade_ts=10:00:01.100 receive=10:00:01.120 -> bucket[10:00:01]
```

Clock tick `10:00:01.000`：

```text
watermark = 10:00:00.500
target bucket 10:00:00 end = 10:00:01.000
not mature
TradesWorker returns None
ClockDriver does not put bar_queue
```

Clock tick `10:00:02.000`：

```text
watermark = 10:00:01.500
target bucket 10:00:00 mature

TradesWorker asks:
  A acc pop 10:00:00 -> real snapshot
  B acc pop 10:00:00 -> real snapshot
  C acc pop 10:00:00 -> maybe forward-fill or None

TradesWorker returns:
  (10:00:00, bars, trades)

ClockDriver:
  audit.write_aggregate_batch(...)
  bar_queue.put(...)
```

Clock tick `10:00:03.000`：

```text
target bucket 10:00:01 mature
C 的 10:00:01 trade 被输出
```

---

## 11. late trade 怎么处理

如果 `10:00:00` bucket 已经被 TradesWorker emit，后面又收到：

```text
trade_ts = 10:00:00.800
receive = 10:00:02.300
```

这笔 trade 不会补发第二个 `10:00:00` payload。

处理方式：

```text
drop + metrics
```

原因：

```text
runtime 不做历史回滚
同一个 bucket_ts_ms 只能 emit 一次
已输出 bucket 不能被静默修改
```

---

## 12. 空 bucket 和 forward-fill

如果某个 active symbol 在 target bucket 没有 trade：

```text
acc.pop_ready_bucket(target_bucket_start_ms, now_ms, active=True)
```

会尝试 forward-fill：

```text
open/high/low/close/vwap = last_close
volume = 0
trade_count = 0
```

前提：

```text
last_close 已知
symbol active
```

如果从未有过 trade，`last_close` 是 NaN，则不生成无限 NaN 空桶。

---

## 13. 常见误区

### 误区 1：用 receive_ts_ms 分桶

错误：

```text
bucket_start_ms = receive_ts_ms - receive_ts_ms % freq_ms
```

正确：

```text
bucket_start_ms = trade_ts_ms - trade_ts_ms % freq_ms
```

### 误区 2：Accumulator 会主动 flush

不会。

Accumulator 只是状态容器。

flush 由：

```text
ClockDriver -> TradesWorker -> Accumulator
```

触发。

### 误区 3：同一个 bucket 可以分多次送 runtime

不可以。

第一版设计要求：

```text
同一个 bucket_ts_ms 最多 emit 一次
```

后续迟到 trade 只能 drop 或进入 correction audit，不进入 runtime。

### 误区 4：exchange_time 可以直接和 kline_trades 混用

第一版不支持。

原因是 kline 和 exchange-time trades 的 bucket 时点可能不一致。

---

## 14. 什么时候用 exchange_time

适合：

```text
希望 trades-derived bars 更接近交易所官方 kline
需要减少秒边界错桶
可以接受 allowed_lateness 带来的延迟
trades-only data_source
```

不适合：

```text
极低延迟策略，不能接受额外 0.5s ~ 2s 延迟
必须立即处理本地收到的每笔 trade
当前阶段需要 kline_trades 混合模式
```

---

## 15. 一句话总结

exchange-time Engine 的核心是：

```text
用 trade_ts_ms 决定 trade 属于哪个 bucket；
用 allowed_lateness 等一小段时间吸收迟到 trade；
用 TradesWorker 的 target_bucket_start_ms 同步所有 symbols；
由 ClockDriver 统一把 finalized bucket 写 audit 并推入 runtime。
```
