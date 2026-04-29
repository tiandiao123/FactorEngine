# Live Dataflow Streaming Adapter Design

> Date: 2026-04-29
> Scope: next-step design for adapting `dataflow/livetrading` to the streaming factor engine
> Status: design draft, no implementation yet

## 1. Why this document exists

The current factor engine is a streaming system:

```text
regular bar-like input point
    -> queue
    -> fe_runtime.InferenceEngine.push_bars()
    -> rolling factor graph state update
    -> latest factor outputs
```

`dataflow/simulation` already matches this model because each simulation tick creates one finished bar per symbol and can push a `round_bars` message into `bar_queue`.

`dataflow/livetrading` does not yet match this model. It was originally built around a snapshot/batch contract:

```text
OKX WebSocket event
    -> worker parse / aggregate
    -> rolling numpy cache
    -> Engine.get_data() / get_trade_data() / get_book_data()
```

That snapshot path is still useful and should stay, but it is not enough for streaming factor inference.

The next design goal is:

> Live kline data and live trades data should both be adaptable into regular bar-like points that can feed the same streaming factor queue.

Order book level data is explicitly out of scope for this design round.

---

## 2. Design goals

### 2.1 Support two live input sources

The live dataflow layer should support two first-class bar-producing sources:

```text
Source A: OKX kline stream
Source B: OKX trades stream aggregated into trade-derived bars
```

Both sources should be able to feed the streaming factor engine.

### 2.2 Keep the factor engine simple

The first version should not require the factor engine to understand raw trades.

The factor engine should continue to consume regular bar-like points:

```text
[ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote]
```

Inside `Engine._runtime_loop`, these still map to the current `fe_runtime.BarData` fields:

```text
close, volume, open, high, low, ret
```

### 2.3 Keep kline and trades separate at first

The first version should not merge exchange klines with trade-derived features.

Instead, live mode should select one bar source:

```text
live_bar_source = "kline" | "trades"
```

This avoids early complexity around aligning two exchange streams with different latency, ordering, and failure modes.

### 2.4 Preserve snapshot APIs

Existing APIs should continue to work:

```python
engine.get_data()
engine.get_trade_data()
engine.get_book_data()
```

The streaming queue is an additional output path from live dataflow, not a replacement for caches.

---

## 3. Non-goals for the first version

The first version should not attempt these:

- order book feature bars
- joining kline bars with trade feature bars
- cross-sectional synchronization guarantees
- irregular event-time factor execution
- 10ms or tick-by-tick factor updates
- C++ rewrite of the trade aggregator
- broad runtime input expansion for `vwap`, `buy_vol`, `trade_cnt`, and similar fields

Those can come later after the regular live bar path is stable.

---

## 4. Current gap in the repository

Today, `Engine` creates a `bar_queue` when `factor_group` is configured.

In simulation mode:

```text
SimBarWorker
    -> generates round_bars
    -> BarCache.append()
    -> bar_queue.put(round_bars)
```

In live mode:

```text
BarDataflowWorker
    -> parses or aggregates candles
    -> BarCache.append()
```

Live mode currently does not push completed bars into `bar_queue`.

So this is not enough:

```python
Engine(mode="live", factor_group="okx_perp")
```

The runtime thread can start, but it has no live bar messages to consume.

The live dataflow refactor must add a streaming event path:

```text
completed live bar
    -> cache append
    -> queue publish
```

---

## 5. Unified output contract

Both kline and trades should output the same bar row format in the first version:

```text
RegularBarPoint:
    symbol: str
    row: np.ndarray shape-(8,) float64

row columns:
    [ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote]
```

The queue message should remain compatible with the existing runtime loop:

```python
round_bars: dict[str, np.ndarray]
```

Examples:

```python
{"BTC-USDT-SWAP": btc_bar}
```

or:

```python
{
    "BTC-USDT-SWAP": btc_bar,
    "ETH-USDT-SWAP": eth_bar,
}
```

This keeps `Engine._runtime_loop` mostly unchanged.

---

## 6. Kline source design

### 6.1 Source behavior

Kline source is the simpler path.

The current live bar worker already does most of the required work:

```text
OKX candle stream
    -> confirmed candle filter
    -> optional sub-minute aggregation
    -> BarCache.append()
```

The missing part is publishing completed bars:

```text
OKX candle stream
    -> confirmed candle filter
    -> optional sub-minute aggregation
    -> completed bar
    -> BarCache.append()
    -> bar_queue.put(...)
```

### 6.2 Direct and aggregated kline modes

The existing behavior should remain:

```text
data_freq < 60s
    -> subscribe candle1s
    -> aggregate locally into N-second bars

data_freq >= 60s
    -> subscribe direct OKX candle channel
    -> parse confirmed candle directly
```

### 6.3 Required streaming protections

The live kline path needs stronger protections than snapshot-only mode:

- only push confirmed candles
- track `last_pushed_ts_by_symbol`
- skip duplicate bars with the same timestamp
- skip older bars that arrive after a newer bar was already pushed
- decide how to log gaps

For a stateful factor graph, duplicate pushes are worse than duplicate cache rows because they permanently mutate rolling operator state.

### 6.4 Kline queue policy

For the first version, kline source can publish partial rounds:

```python
bar_queue.put({symbol: bar}, block=False)
```

This means each symbol updates independently as its confirmed candle arrives.

Later, if multi-symbol batching matters, a round assembler can group bars by timestamp with a short timeout.

---

## 7. Trades source design

### 7.1 Why trades need an adapter

Trades are irregular event streams:

```text
12:00:00.001 trade
12:00:00.003 trade
12:00:00.080 trade
12:00:00.500 trade
```

The factor engine should not consume every raw trade in the first version.

Instead, trades should be converted into regular bars:

```text
raw trades
    -> per-symbol TradeAggregator
    -> fixed interval trade-derived bar
    -> bar_queue
```

### 7.2 First version interval

Start with 1 second trade bars:

```text
trade_bar_interval = 1000 ms
```

This matches the existing second-level mental model and avoids premature complexity from 100ms or 10ms buckets.

### 7.3 Event time

Aggregation should use exchange trade timestamp:

```text
bucket_start_ms = floor(exch_ts_ms / interval_ms) * interval_ms
```

Receive timestamp should be kept for monitoring latency, not for bucket assignment.

Raw parsed trade event shape:

```text
TradeEvent:
    symbol
    exch_ts_ms
    recv_ts_ms
    price
    size
    side
    trade_id
```

The current trade cache stores only `[px, sz, side]`. That is enough for snapshots, but the aggregator path should preserve exchange time internally.

### 7.4 Per-symbol aggregator state

Each symbol should own independent aggregation state:

```text
TradeAggregator per symbol:
    current_bucket_start_ms
    current_bucket_end_ms
    open
    high
    low
    close
    volume
    vol_ccy
    buy_vol
    sell_vol
    trade_count
    buy_count
    sell_count
    last_close
```

For the first version, only these fields need to be emitted:

```text
ts
open
high
low
close
volume
vol_ccy
vol_ccy_quote
```

The extra fields can remain internal or be stored in a future feature cache.

### 7.5 Empty bucket policy

Use fixed-frequency output.

If a symbol has no trades in a bucket but has a known `last_close`, emit an empty bar:

```text
open = last_close
high = last_close
low = last_close
close = last_close
volume = 0
vol_ccy = 0
vol_ccy_quote = 0
```

This gives the factor engine a regular time series.

If a symbol has never traded since startup and has no `last_close`, skip output for that symbol until it is initialized.

### 7.6 Flush trigger

The first version should use a local timer-driven flush, not kline arrival as the trigger.

Reason:

- kline websocket delivery can be delayed
- symbols do not arrive at the same time
- reconnects can cause gaps
- trade aggregation should be independent from kline collection

Recommended behavior:

```text
TradeBarWorker timer ticks every interval_ms
    -> for each symbol
        -> flush the bucket that just ended
        -> emit real or empty bar
    -> publish one round_bars message
```

This makes trades source similar to simulation:

```text
one local loop tick
    -> one bar per initialized symbol
    -> one round_bars message
```

### 7.7 Late and out-of-order trades

First-version policy should be conservative:

- accept trades for the current open bucket
- accept trades for a not-yet-flushed bucket
- drop trades older than the last flushed bucket
- log late drops with counters, not per-event noisy logs

Late trade correction can be designed later. The current streaming factor runtime cannot easily undo a bar that was already pushed.

---

## 8. Live source selection

The live dataflow config should eventually make source selection explicit:

```python
Engine(
    mode="live",
    live_bar_source="kline",   # or "trades"
)
```

Possible first-version values:

| Value | Meaning |
|---|---|
| `"kline"` | use OKX candle stream as bar source |
| `"trades"` | use OKX trades stream aggregated into regular bars |

Do not support `"kline+trades"` in the first version.

That mode requires a joiner and stronger alignment rules.

---

## 9. Queue semantics

The queue should carry regular bar batches:

```python
dict[str, np.ndarray]
```

### 9.1 Kline source

First version:

```text
partial round allowed
one completed candle can publish one-symbol dict
```

Example:

```python
{"BTC-USDT-SWAP": bar}
```

### 9.2 Trades source

First version:

```text
timer flush emits one batch containing all initialized symbols
```

Example:

```python
{
    "BTC-USDT-SWAP": btc_trade_bar,
    "ETH-USDT-SWAP": eth_trade_bar,
}
```

### 9.3 Backpressure

Use the same non-blocking queue policy as simulation:

```python
bar_queue.put(round_bars, block=False)
```

If the queue is full:

- drop the entire round
- increment a dropped-round counter
- log periodic warnings

Blocking the websocket or aggregation worker would create worse live-data failure modes.

---

## 10. Proposed module boundaries

### 10.1 Kline path

Existing files can be extended:

```text
dataflow/livetrading/bars/worker.py
    -> accept optional bar_queue
    -> publish completed bars
    -> deduplicate by symbol/timestamp
```

### 10.2 Trades path

Add a trade-bar aggregation path without replacing raw trade cache support:

```text
dataflow/livetrading/trades/aggregator.py
    -> TradeEvent
    -> TradeAggregator
    -> TradeBarAggregatorManager

dataflow/livetrading/trades/worker.py
    -> keep raw trade cache append
    -> optionally feed TradeAggregator
    -> timer flush emits bar rows
```

One important design choice:

> Raw trade cache and trade-derived bars are different products.

Raw trade cache supports inspection and future batch features.

Trade-derived bars feed the streaming factor engine.

### 10.3 Manager path

`DataflowManager` should accept optional streaming parameters:

```text
bar_queue
live_bar_source
trade_bar_interval_seconds
```

It should wire only the selected live bar source into the queue.

---

## 11. Phased implementation plan

### Phase 0: document and align

Goal:

- agree on source selection
- agree on first-version queue semantics
- agree that order book and kline+trades join are out of scope

Output:

- this document
- a short checklist of implementation tasks

### Phase 1: live kline to runtime queue

Goal:

```text
Engine(mode="live", live_bar_source="kline", factor_group=...)
```

feeds the existing runtime queue.

Tasks:

- add `bar_queue` to live `DataflowManager`
- pass it from `Engine` in live mode
- add optional queue publishing to `BarDataflowWorker`
- add duplicate/old timestamp protection
- add tests with synthetic callback input, not real OKX network

Expected result:

- live kline path becomes streaming-runtime compatible
- no trade aggregation yet

### Phase 2: trade-derived 1s bars

Goal:

```text
Engine(mode="live", live_bar_source="trades", factor_group=...)
```

feeds runtime using bars built from trades.

Tasks:

- parse trade exchange timestamp and trade id in collector path
- add per-symbol trade aggregator
- add timer-driven 1s flush
- implement empty bucket carry-forward
- emit current bar schema
- append trade-derived bars to `BarCache`
- publish `round_bars` to `bar_queue`
- add unit tests for normal, empty, late, and out-of-order cases

Expected result:

- raw trades can drive the existing bar-level factor engine
- factor runtime still does not need new input fields

### Phase 3: source selection cleanup

Goal:

- make `live_bar_source` explicit and documented
- avoid ambiguous combinations of `enable_bars`, `enable_trades`, and factor runtime

Tasks:

- define valid config combinations
- update README or tutorial
- add config validation errors

Example:

```text
live_bar_source="trades" requires trade collection
live_bar_source="kline" requires bar collection
```

### Phase 4: extended trade features

Goal:

Expose microstructure fields to factor graphs.

Possible fields:

```text
vwap
buy_vol
sell_vol
signed_vol
trade_cnt
buy_cnt
sell_cnt
avg_sz
max_sz
vol_imb
cnt_imb
rv
```

This phase requires runtime changes:

- extend `BarData` or introduce a new input struct
- extend `Op` input enum
- update `FactorGraph::add_input`
- update pybind bindings
- update `Engine._runtime_loop`
- update factor builders

Do not do this before Phase 2 is stable.

### Phase 5: future book features

Out of scope for now.

Later, order book features should follow the same pattern:

```text
raw book updates
    -> fixed-interval book feature bars
    -> regular point or extended market point
    -> factor engine
```

---

## 12. First-version recommendation

The recommended first implementation sequence is:

```text
1. Add live kline -> bar_queue
2. Add trades -> 1s trade-derived OHLCV bars
3. Add explicit live_bar_source config
4. Only then consider extra trade feature fields
```

Do not start by expanding the factor runtime input schema.

The fastest safe path is to make live data produce the bar schema the runtime already understands.

---

## 13. Open design questions

These should be answered before implementation:

1. Should the default live source stay `"kline"` for backward compatibility?
2. Should trade-derived bars use `trades` or `trades-all` by default?
3. Should trade bars be appended to the same `BarCache` as kline bars?
4. Should the cache expose whether a bar came from kline or trades?
5. How often should late-trade/drop counters be reported?
6. Should `bar_count` count rows or rounds?
7. Should kline source eventually use a timestamp round assembler, or are partial rounds enough?

---

## 14. Short conclusion

The live dataflow refactor should introduce a clear adapter layer:

```text
raw live data
    -> source-specific adapter
    -> regular bar-like point
    -> cache and queue
    -> streaming factor engine
```

Kline source is already close to this shape.

Trades source needs a per-symbol fixed-interval aggregator before it can feed the existing factor engine.

The first version should keep the runtime input schema unchanged and focus on making live data produce reliable regular bars.
