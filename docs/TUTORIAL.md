# FactorEngine Tutorial

This document is a fast path for understanding the repository structure and the execution model of FactorEngine.

If you only read three files first, read these in order:

1. `factorengine/engine.py` — the actual runtime entry point.
2. `dataflow/README.md` — the dataflow architecture (exchange adapters, ClockDriver, shared components).
3. `docs/20260528/exchange_time_engine_tutorial.md` — the current exchange-time trades bucket workflow.

## 1. What this project is

FactorEngine is a real-time factor computation stack for quantitative trading.

At a high level it combines:

- Python market-data collection via WebSocket (**OKX** and **Binance Futures**) — kline candles and real-time trades
- a Python factor registry and visualization layer (**123 factors** across 2 groups)
- C++ operator kernels for fast rolling/time-series math (**33 operators**)
- a C++ DAG runtime for streaming factor inference
- a unified ClockDriver that synchronizes and aggregates heterogeneous data streams
- optional aggregate audit logging for post-run verification
- exchange-time trades bucketing for latency-tolerant, exchange-timestamp-aligned bars
- an exchange-agnostic architecture with shared components (`BaseCollector`, `KlinePublisher`, `TradesAccumulator`)

The core idea is simple: market data arrives continuously via WebSocket push, independent workers collect and buffer each data stream, a ClockDriver periodically flushes all workers on wall-clock boundaries, aggregates the results, and a factor runtime turns each merged bar into updated factor values.

## 2. Mental model in 30 seconds

You can think about the system as a pipeline:

```text
Exchange WebSocket (kline / aggTrade / trades)
        │                                │
        v                                v
   KlineWorker                      TradesWorker
   (Collector → Adapter → Publisher) (Collector → Accumulators)
        │                                │
        v                                v
        └──────────── ClockDriver ───────┘
                      (wall-clock aligned flush)
                      kline_data = kline_worker.flush()
                      trades_data = trades_worker.flush()
                      → _aggregate()
                      → optional audit JSONL write
                      → bar_queue.put()
                              │
                              v
               C++ inference runtime
               (push_bars → factor DAG → get_all_outputs)
                              │
                              v
                      signal_deque (latest factor outputs)
```

This works identically regardless of whether the exchange is OKX, Binance, or a future addition. The exchange-specific logic lives entirely in `dataflow/okx/` and `dataflow/binance/`.

There are two ways to use the project:

1. Use `factorengine.Engine` as the top-level application object.
2. Use `fe_runtime` directly if you only care about factor DAG execution.

## 3. Current architecture

The current implementation is the v4 architecture (multi-thread streaming model with ClockDriver, multi-exchange support):

1. **Thread 1 (exchange-kline)**: `KlineWorker` (OKX) or `BinanceKlineWorker` daemon thread — collects exchange WebSocket kline data, filters/aggregates via adapter, batches all symbols via `KlinePublisher` into an internal buffer. Does not push to any queue. Exposes `flush()` for ClockDriver.
2. **Thread 2 (exchange-trades)**: `TradesWorker` (OKX) or `BinanceTradesWorker` daemon thread — collects trades via WebSocket, routes each trade to per-symbol accumulators. In default receive-time mode, trades update the current local bucket; in exchange-time mode, trades are assigned by exchange `trade_ts_ms` and finalized after `allowed_lateness_ms`. Exposes `flush()` which returns `(round_bars, round_trades)`.
3. **Thread 3 (clock-driver)**: `ClockDriver` daemon thread — fires on wall-clock aligned boundaries (e.g. every 5 seconds at `:00`, `:05`, `:10`, …). Calls `flush()` on all registered workers, runs `_aggregate()` to merge kline + trades data (kline bars take priority per symbol), optionally writes aggregate audit records, and pushes the unified payload to `bar_queue`.
4. **Thread 4 (factor-infer)**: `Engine._runtime_loop` daemon thread — consumes `bar_queue`, constructs `rt.BarData`, calls C++ `InferenceEngine.push_bars()`, stores results in `signal_deque`.
5. **Thread 5 (main)**: User strategy code — reads `engine.get_factor_outputs()` and `engine.get_data()`.

That separation matters because:
- Each data source (kline, trades, future: orderbook, funding rate) runs independently with its own WS connection and event loop
- ClockDriver ensures all data sources are flushed at the exact same wall-clock boundary, guaranteeing time alignment
- Audit logging records exactly the aggregate bars that ClockDriver flushed, so post-run checks can compare the online aggregate output with external references
- Adding a new data source (e.g. orderbook) only requires implementing a worker with a `flush()` method and calling `clock_driver.register("ob", ob_worker)`
- Adding a new exchange only requires implementing collector/adapter/worker in a new `dataflow/<exchange>/` package — the shared components (`KlinePublisher`, `TradesAccumulator`, `BaseCollector`, `ClockDriver`) require zero changes
- Factor computation is completely decoupled from data ingestion

## 4. Top-level repository map

### `factorengine/`

This is the main Python package.

- `engine.py`: the top-level `Engine` class. Supports `exchange="okx"` (default) or `exchange="binance"`. If you want to understand how the system boots, start here.
- `factors/`: factor registration, factor discovery, and graph visualization.
  - `okx_perp/`: 70 kline-based factors (inputs: close, volume, open, high, low, ret)
  - `okx_perp_trades/`: 53 trades-based factors (inputs: vwap, buy_vol, sell_vol, plus OHLCV)

### `dataflow/`

This is the market-data side of the system. See [`dataflow/README.md`](../dataflow/README.md) for the full architecture document.

- `base_collector.py`: **BaseCollector ABC** — shared reconnect/lifecycle logic inherited by all four collectors.
- `clock_driver.py`: **ClockDriver** — the unified wall-clock aligned flush driver and aggregation center. Registered workers are flushed simultaneously on each tick, their outputs merged, and the result pushed to `bar_queue`. This is the single component that bridges workers and the inference thread.
- `audit/`: **aggregate audit logging** — optional async JSONL writer for ClockDriver aggregate bars. It records aggregate bars only, not raw websocket messages or per-trade events.
- `kline/publisher.py`: **KlinePublisher** — exchange-agnostic round batching with timeout, forward-fill, and deduplication. Used by both OKX and Binance kline workers.
- `trades/accumulator.py`: **TradesAccumulator** — exchange-agnostic per-symbol O(1) streaming aggregation. Tracks vwap, buy_vol, sell_vol, trade_count, and derives OHLCV from trades. Also contains the exchange-time bucketed accumulator used by `trade_time_mode="exchange_time"`.
- `okx/`: OKX exchange data streams (WebSocket-based).
  - `okx/common.py`: shared constants (endpoints, channel mappings, subscription limits).
  - `okx/kline/`: collector → adapter → publisher → worker.
  - `okx/trades/`: collector → accumulator → worker.
- `binance/`: Binance Futures (USDT-M) data streams.
  - `binance/common.py`: endpoints (production requires `/market` route), symbol mapping (`BTC-USDT` ↔ `btcusdt`).
  - `binance/kline/`: collector → adapter → worker. Minimum kline interval: 1 minute.
  - `binance/trades/`: collector (aggTrade) → worker.
- `simulation/`: synthetic bar generation for local testing and runtime demos.
- `cache.py`: thread-safe rolling cache backed by numpy arrays.

This layer is responsible for producing structured numeric data, not for factor math.

### `native/`

This is the C++ implementation layer.

- `include/fe/ops/`: batch-style operator kernels such as `Ma`, `TsStd`, `Corr`, and `TsRank`.
- `include/fe/runtime/`: the streaming DAG runtime, including `FactorGraph`, `SymbolRunner`, and `InferenceEngine`. `BarData` includes `vwap`, `buy_vol`, `sell_vol` fields.
- `pybind/`: Python bindings for the C++ modules.

If you care about performance or numerical behavior, this directory matters most.

### `examples/`

Runnable demos:

- `staticSymbols/engine_rt_live.py`: live mode connecting to OKX, streaming kline data, computing 70 factors for 3 symbols in real-time.
- `staticSymbols/engine_rt_trades.py`: live OKX trades-only mode, computing 53 trades factors for 20 symbols.
- `staticSymbols/engine_rt_binance_trades.py`: live **Binance Futures** trades-only mode, picks the top 200 USDT-M perpetuals by 24h quote volume at startup, computes 53 trades factors, and enables aggregate audit logging.
- `staticSymbols/engine_rt_simulation.py`: simulation mode with synthetic data, useful for testing and debugging.
- `dynanmicSymbols/engine_rt_binance_dynamic_symbols.py`: Binance dynamic symbol universe demo.
- `dynanmicSymbols/engine_rt_okx_dynamic_symbols.py`: OKX dynamic symbol universe demo.
- `timeBuckets/engine_rt_binance_exchange_time.py`: Binance static-symbol exchange-time trades bucket demo.
- `timeBuckets/engine_rt_binance_dynamic_exchange_time.py`: Binance dynamic-symbol exchange-time trades bucket demo.
- `timeBuckets/engine_rt_okx_dynamic_exchange_time.py`: OKX dynamic-symbol exchange-time trades bucket demo.
- `verifier/`: audit verification scripts. Binance can compare against official klines or `aggTrades`; OKX compares against official 1m candles.

### `tests/`

This repo is heavily test-driven across several slices:

- `tests/dataflow/`: kline adapter, publisher, worker, end-to-end pipeline, and ClockDriver tests
- `tests/dataflow/binance/`: Binance-specific tests — collector parsing, worker flush, Engine integration
- `tests/factors/`: registry and end-to-end factor inference
- `tests/kernels/`: C++ operator alignment and benchmarks
- `tests/audit/`: aggregate audit writer and ClockDriver integration tests
- `tests/runtime_engine/`: runtime-level integration tests (including trades integration and ClockDriver aggregation)
- `tests/trades/`: accumulator, collector, worker, and trades factor tests
- `tests/visualization/`: graph visualization demos

### `docs/`

This folder contains both stable onboarding docs and dated design notes.

- `TUTORIAL.md`: this file, intended as the quick orientation entry point
- `OPERATORS.md`: full C++ operator list with math formulas and test locations
- `20260504/binance_dataflow_design.md`: Binance Futures integration design and implementation notes
- `20260505/trades_aggfeatures_plan.md`: trades aggregate factor plan
- `20260523/dynamic_symbol_list_design.md`: design for runtime symbol universe updates
- `20260523/stream_audit_replay_design.md`: stream audit/replay design notes
- `20260523/stream_audit_replay_refactor_plan.md`: stream audit/replay refactor plan
- `20260524/aggregate_audit_implementation.md`: implemented aggregate audit logging call flow and schema
- `20260524/bar_schema_alignment_design.md`: bar schema alignment risks and cleanup plan
- `20260525/dynamic_symbol_end_to_end_flow.md`: dynamic symbol update path across Engine, ClockDriver, workers, and C++ runtime
- `20260526/exchange_time_trades_aggregation_allowed_lateness.md`: exchange-time trades bucketing design with allowed lateness
- `20260528/exchange_time_engine_tutorial.md`: operational tutorial for `trade_time_mode="exchange_time"`

## 5. The main Python modules

### `factorengine.engine`

This is the public entry point.

What it owns:

- mode selection: live or simulation
- **exchange selection**: `"okx"` (default) or `"binance"`
- construction of the proper dataflow workers based on `exchange` + `data_source`:
  - OKX: `KlineWorker`, `TradesWorker`
  - Binance: `BinanceKlineWorker`, `BinanceTradesWorker`
  - Simulation: `SimDataflowManager`
- `ClockDriver` creation and worker registration based on `data_source`
- optional initialization of the C++ `InferenceEngine`
- the queue between ClockDriver and runtime thread
- the deque holding latest factor outputs
- snapshot APIs like `get_data()` and `get_factor_outputs()`
- optional `audit_config` wiring and `audit_stats`
- optional `trade_time_mode="exchange_time"` and `allowed_lateness_ms` for trades-only live engines
- runtime symbol universe updates through `update_symbols(...)`
- `testnet` parameter for Binance Futures testnet connectivity

If you want to know how the system actually runs, this file is the most important place to read.

### `factorengine.factors`

This package defines how factors are organized on the Python side.

Key pieces:

- `registry.py`: the `FactorRegistry` and `@register_factor` decorator
- `visualize.py`: ASCII, DOT, and PNG graph rendering helpers
- `okx_perp/`: 70 kline-based factors (inputs: close, volume, open, high, low, ret)
- `okx_perp_trades/`: 53 trades-based factors (inputs: vwap, buy_vol, sell_vol)

The registry does not execute factors itself. Its job is to discover builder functions and return compiled `FactorGraph` objects that are later handed to `fe_runtime`.

## 6. The dataflow modules

For the complete architecture document, see [`dataflow/README.md`](../dataflow/README.md).

### `dataflow.base_collector`

Abstract base class for all WebSocket collectors. All four collectors (OKX kline/trades, Binance kline/trades) inherit from this.

Provides:
- Symbol chunking (≤ N symbols per WS connection)
- Auto-reconnect loop with error-specific backoff
- Subclass contract: implement `_connect_and_listen(session, symbols)`

### `dataflow.clock_driver`

This is the central coordination piece (v4 architecture).

ClockDriver is a daemon thread that:
1. Registers named workers via `register(name, worker)` — any object with a `flush()` method
2. On each wall-clock aligned tick, calls `flush()` on every registered worker
3. Runs `_aggregate()` to merge outputs — kline bars take priority per symbol, trades-derived OHLCV is used as fallback
4. Optionally writes aggregate audit records to `AuditWriter`
5. Pushes the unified `(merged_bars, merged_trades)` payload to `bar_queue`

This design means adding a new data source (orderbook, funding rate) is as simple as:
```python
clock_driver.register("ob", orderbook_worker)
```

### `dataflow.audit`

Aggregate audit logging records the bars ClockDriver already flushed. It is intentionally aggregate-only: it does not save raw websocket messages or every trade.

Important files:

- `config.py`: `AuditConfig`, including queue size, flush interval, flush thresholds, and file rotation rules
- `schema.py`: `AggregateBarEvent`, the JSONL record schema
- `writer.py`: `AuditWriter`, the async writer that batches records and rotates files

When enabled through `Engine(audit_config=AuditConfig(...))`, audit files are written as JSONL:

```text
data/audit/<name>/aggregate_bars_YYYYMMDD_HHMMSS_part000001.jsonl
```

Use `engine.audit_stats` to inspect queue, flush, drop, and current-file state.

### `dataflow.kline.publisher`

Exchange-agnostic `KlinePublisher` — round batches BarPoint arrays from all symbols:

- Timeout + forward-fill for missing symbols
- Per-symbol deduplication via `last_pushed_ts`
- Timestamp gap detection and warning
- Exposes `flush_for_clock()` for ClockDriver

Used by both `KlineWorker` (OKX) and `BinanceKlineWorker` with zero code duplication.

### `dataflow.trades.accumulator`

Exchange-agnostic `TradesAccumulator` — per-symbol O(1) streaming aggregation:

- Running `vwap`, `buy_vol`, `sell_vol`, `trade_count`
- OHLCV derived from trades (for `trades`-only mode)
- Thread-safe via `threading.Lock`
- Forward-fills OHLC with last close on empty buckets

Used by both `TradesWorker` (OKX) and `BinanceTradesWorker`.

For exchange-time mode, the same module also provides bucketed event-time accumulation:

```text
trade_ts_ms -> bucket_start_ms
symbol      -> EventTimeTradesAccumulator
            -> buckets[bucket_start_ms]
```

The important distinction is:

- **Receive-time mode**: a trade updates whichever local accumulator is active when the message arrives.
- **Exchange-time mode**: a trade is assigned by exchange `trade_ts_ms`; `ClockDriver` only emits buckets whose end time is older than `now_ms - allowed_lateness_ms`.

Late trades outside the configured lateness window are not merged into finalized buckets. They are counted in `engine.trades_stats["dropped_late_trade_count"]`.

### `dataflow.okx`

OKX exchange data stack.

**Kline pipeline** (within the kline worker thread):

```text
OKX WebSocket (candle1s / candle1m / ...)
    → OKXKlineCollector (connect, subscribe, parse JSON)
    → KlineAdapter (filter unconfirmed, time-aligned aggregation)
    → KlinePublisher (round batching, timeout, forward-fill → internal buffer)
```

**Trades pipeline** (within the trades worker thread):

```text
OKX WebSocket (trades)
    → OKXTradesCollector (connect, subscribe, parse JSON)
    → per-symbol TradesAccumulator.update(px, sz, side)
```

Key design decisions:
- OKX supports 1-second klines natively (`candle1s`)
- `KlineAdapter` handles time-aligned aggregation (e.g. 5×1s → 5s bars)
- Symbol format: `BTC-USDT-SWAP` (perpetual)

### `dataflow.binance`

Binance Futures (USDT-M) data stack.

**Trades pipeline** (primary path for sub-minute frequencies):

```text
Binance Futures WS (aggTrade via /market/ws)
    → BinanceTradesCollector (connect, dynamic SUBSCRIBE, parse JSON)
    → per-symbol TradesAccumulator.update(px, sz, side)
```

**Kline pipeline** (≥ 1 minute only):

```text
Binance Futures WS (kline_1m+ via /market/ws)
    → BinanceKlineCollector (connect, dynamic SUBSCRIBE, parse JSON)
    → BinanceKlineAdapter (filter confirmed candles: k.x == true)
    → KlinePublisher (round batching)
```

Key design decisions:
- Binance Futures has **no sub-minute klines** — `data_source="trades"` is the primary path for 1s frequency
- Production WS requires `/market` URL prefix (2026 routing upgrade); testnet does not
- Dynamic subscription via JSON `SUBSCRIBE` message (not URL path)
- Symbol conversion: `BTC-USDT` ↔ `btcusdt` (WS lowercase) / `BTCUSDT` (REST uppercase)
- `is_buyer_maker` flag maps to buy/sell side (inverted from OKX convention)

### `dataflow.simulation`

This is the local test and demo backend.

Important files:

- `generator.py`: synthetic bar generator
- `worker.py`: periodic simulation worker
- `manager.py`: simulation-mode drop-in replacement for the live manager
- `symbols.py`: default symbols and base-price helpers

If you want to debug the engine without connecting to an exchange, start here.

## 7. The native modules

### `fe_ops`

This is the batch array-level operator library.

It contains 33 kernels grouped roughly as:

- P0: elementwise ops
- P1: single-series rolling ops
- P2: bivariate and special rolling ops
- P3: lower-priority or heavier rolling ops

Full documentation with math formulas: [`docs/OPERATORS.md`](OPERATORS.md).

This layer is mostly about numerical correctness and speed relative to Python or pandas baselines.

### `fe_runtime`

This is the streaming factor runtime.

The important concepts are:

- `FactorGraph`: a compiled DAG of factor operations
- `SymbolRunner`: manages multiple factor graphs for one symbol
- `InferenceEngine`: manages multiple symbols and dispatches bar updates

Supported input features: `close`, `volume`, `open`, `high`, `low`, `ret`, `vwap`, `buy_vol`, `sell_vol`. Unknown features throw `std::invalid_argument`.

`BarData` struct carries all nine fields; `push_bars` distributes them to each symbol's factor graphs in parallel.

## 8. How data moves through the system

### Live mode (v4 — ClockDriver, multi-exchange)

```text
Thread 1 (exchange-kline daemon):
  Exchange WS → Collector (parse JSON, on_candle callback)
              → Adapter (filter, aggregate to N-second bars)
              → Publisher (batch all symbols → internal _ready_round buffer)

Thread 2 (exchange-trades daemon):
  Exchange WS → Collector (parse JSON, on_trade callback)
              → per-symbol TradesAccumulator.update(px, sz, side)

Thread 3 (clock-driver daemon):
  every Ns (wall-clock aligned):
    kline_data  = kline_worker.flush()   → dict[sym, ndarray] | None
    trades_data = trades_worker.flush()  → (dict[sym, ndarray], dict[sym, dict])
    merged      = _aggregate(kline_data, trades_data)
    optional aggregate audit JSONL write
    bar_queue.put((merged_bars, merged_trades))

Thread 4 (factor-infer daemon):
  bar_queue.get() → construct rt.BarData per symbol
                    (close, volume, open, high, low, ret,
                     vwap, buy_vol, sell_vol from trades)
                  → inference.push_bars(batch)  [C++ parallel]
                  → inference.get_all_outputs()
                  → signal_deque.append(result)

Thread 5 (main / strategy):
  engine.get_factor_outputs()  → read signal_deque[-1]
  engine.get_data()            → read debug_cache snapshot
  engine.clock_stats           → ClockDriver tick/drop counts
  engine.audit_stats           → audit queue/flush/drop/current-file state
```

### Simulation mode

1. `Engine` creates `SimDataflowManager`.
2. `SimBarWorker` generates deterministic synthetic bars.
3. Bars are written into `BarCache` and enqueued as complete rounds for inference.
4. The runtime thread computes factors the same way as in live mode.

That symmetry is why simulation mode is the easiest place to understand the full stack.

### Exchange-time trades mode

For trades-only live engines, `Engine` can bucket trades by exchange timestamp instead of local receive time:

```python
from factorengine.engine import Engine

engine = Engine(
    symbols=["BTC-USDT", "ETH-USDT"],
    mode="live",
    exchange="binance",
    data_source="trades",
    data_freq="5s",
    factor_group=["okx_perp_trades"],
    trade_time_mode="exchange_time",
    allowed_lateness_ms=200,
)
```

The same mode is supported for OKX swaps:

```python
engine = Engine(
    symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    mode="live",
    exchange="okx",
    data_source="trades",
    data_freq="5s",
    factor_group=["okx_perp_trades"],
    trade_time_mode="exchange_time",
    allowed_lateness_ms=200,
)
```

This solves receive-time boundary skew. A trade with exchange time `10:00:00.999` that arrives locally at `10:00:01.020` still belongs to the bucket starting at `10:00:00.000`, as long as it arrives inside the allowed lateness window.

Current constraints:

```text
data_source="trades" + trade_time_mode="exchange_time" is supported.
data_source="kline_trades" + trade_time_mode="exchange_time" is intentionally blocked for now.
```

The block exists because kline workers emit the current wall-clock bucket, while exchange-time trades workers emit mature event-time buckets. Mixing those in one merged bar would require explicit timestamp reconciliation.

Runnable examples:

```bash
python examples/engine/timeBuckets/engine_rt_binance_exchange_time.py
python examples/engine/timeBuckets/engine_rt_binance_dynamic_exchange_time.py
python examples/engine/timeBuckets/engine_rt_okx_dynamic_exchange_time.py
```

These examples write logs under `examples/engine/timeBuckets/data/logs/` and aggregate audit files under `examples/engine/timeBuckets/data/audit/`.

The dynamic examples also exercise runtime symbol universe updates: `Engine.update_symbols(...)` stages a target set, `ClockDriver` commits it after a flush boundary, the exchange worker rebuilds subscriptions, and the C++ runtime adds/removes per-symbol runners in queue order.

## 9. Exchange comparison

| Feature | OKX | Binance Futures |
|---------|-----|-----------------|
| `exchange=` | `"okx"` (default) | `"binance"` |
| Kline WS | `candle1s`, `candle1m`, … | `kline_1m`, `kline_5m`, … (no sub-minute) |
| Trades WS | `trades` channel | `aggTrade` stream |
| Min kline freq | 1 second | 1 minute |
| WS endpoint | `ws.okx.com:8443/ws/v5/public` | `fstream.binance.com/market/ws` |
| Symbol format | `BTC-USDT-SWAP` | `BTC-USDT` ↔ `btcusdt` |
| Subscription | JSON channel subscribe | JSON `SUBSCRIBE` method |
| Buy/sell field | `side` = `"buy"` / `"sell"` | `is_buyer_maker` (inverted) |
| Testnet support | N/A | `testnet=True` → `stream.binancefuture.com` |

## 10. Audit and verification workflow

The current audit workflow is designed for post-run sanity checks of aggregate bars. Audit files record exactly what `ClockDriver` flushed and sent toward the runtime:

```text
ClockDriver aggregate output
        │
        v
AuditWriter JSONL files
        │
        v
examples/verifier/
        │
        v
Compare audit sub-minute bars aggregated to 1m vs exchange references
```

The verifier compares `open`, `high`, `low`, `close`, and `volume`. `trade_count` is printed for context but does not participate in the match decision because the official kline count and the current audit aggregation count do not necessarily share the same counting semantics.

For Binance, use `verify_binance_audit_vs_kline.py`. It supports two reference sources:

- `--reference kline`: Binance official 1m klines.
- `--reference aggtrades`: Binance REST `aggTrades`, rebuilt to 1m. This is usually the best reference for trades-only exchange-time audits because both sides are derived from aggregate trades.

Example:

```bash
python examples/verifier/verify_binance_audit_vs_kline.py \
  --audit-file examples/engine/timeBuckets/data/audit/binance_exchange_time_demo/aggregate_bars_20260528_081536_part000001.jsonl \
  --symbols BTC-USDT,ETH-USDT,SOL-USDT \
  --start "2026-05-28T08:16:00Z" \
  --end "2026-05-28T08:33:00Z" \
  --reference aggtrades \
  --price-rtol 0.0001 \
  --volume-rtol 0.001 \
  --max-mismatches 3
```

`examples/verifier/run.sh` is a ready-to-edit Binance command.

For OKX, use `verify_okx_audit_vs_candles.py` or `run_okx.sh`:

```bash
bash examples/verifier/run_okx.sh
```

When verifying live OKX output, choose a complete UTC-minute window that avoids the process startup minute and the newest still-settling minute. OKX official candle history has exclusive paging boundaries, and the verifier compensates for that when fetching the requested start minute.

Read [`examples/verifier/README.md`](../examples/verifier/README.md) for timestamp semantics and tolerance rules.

## 11. Symbol list status

Static `Engine(symbols=...)` usage is still supported, but live engines can also update their symbol universe at runtime.

The static Binance trades demo still does a startup-only universe selection:

```text
startup:
  fetch Binance exchangeInfo + 24h ticker
  choose top-N USDT-M perpetuals by quoteVolume
  pass that list into Engine(symbols=...)

runtime:
  subscriptions and accumulators stay fixed
```

Dynamic examples use `Engine.update_symbols(...)` instead:

```python
engine.update_symbols(
    ["BTC-USDT", "ETH-USDT", "DOGE-USDT", "ADA-USDT", "LINK-USDT"],
    version="manual-rotate:20260528T120000Z:1",
    metadata={"reason": "scheduled_rotation"},
)
```

The update flow is:

```text
Engine.update_symbols(...)
  -> SymbolManager stages pending diff
  -> ClockDriver commits after a flush boundary
  -> exchange worker rebuilds WS subscriptions
  -> C++ InferenceEngine adds/removes SymbolRunner instances
  -> factor outputs are filtered by active universe
```

Start with:

```bash
python examples/engine/dynanmicSymbols/engine_rt_binance_dynamic_symbols.py
python examples/engine/dynanmicSymbols/engine_rt_okx_dynamic_symbols.py
python examples/engine/timeBuckets/engine_rt_binance_dynamic_exchange_time.py
python examples/engine/timeBuckets/engine_rt_okx_dynamic_exchange_time.py
```

For details, read [`docs/20260523/dynamic_symbol_list_design.md`](20260523/dynamic_symbol_list_design.md), [`docs/20260525/dynamic_symbol_end_to_end_flow.md`](20260525/dynamic_symbol_end_to_end_flow.md), and [`docs/20260525/cpp_runtime_dynamic_symbol_support_development.md`](20260525/cpp_runtime_dynamic_symbol_support_development.md).

## 12. What to read depending on your goal

### I want to understand the public API

Read these files:

1. `README.md`
2. `factorengine/engine.py`
3. `examples/engine/staticSymbols/engine_rt_live.py`
4. `examples/engine/staticSymbols/engine_rt_binance_trades.py`
5. `examples/engine/timeBuckets/engine_rt_binance_exchange_time.py`

### I want to understand factor execution

Read these files:

1. `factorengine/factors/registry.py`
2. `native/include/fe/runtime/factor_graph.hpp`
3. `native/include/fe/runtime/inference_engine.hpp`

### I want to understand live market data collection

Read these files:

1. `dataflow/README.md` — full dataflow architecture document
2. `dataflow/clock_driver.py` — the central coordination piece
3. `dataflow/okx/kline/worker.py` and `dataflow/binance/kline/worker.py` — kline workers
4. `dataflow/okx/trades/worker.py` and `dataflow/binance/trades/worker.py` — trades workers
5. `dataflow/kline/publisher.py` — shared round batching
6. `dataflow/trades/accumulator.py` — shared O(1) streaming trades aggregation

### I want to understand how ClockDriver aggregates data

Read these files:

1. `dataflow/clock_driver.py` — `register()`, `_flush_and_aggregate()`, `_aggregate()`
2. `docs/20260524/aggregate_audit_implementation.md` — current ClockDriver → audit → runtime call flow
3. `factorengine/engine.py` — `_init_live()` to see how workers are registered per exchange

### I want to understand how a new exchange was added

Read these files:

1. `docs/20260504/binance_dataflow_design.md` — design document
2. `dataflow/binance/` — the implementation
3. `dataflow/base_collector.py` — shared ABC
4. `factorengine/engine.py` — `_init_live()` exchange branching

### I want to understand the complete live pipeline end-to-end

Read these documents:

1. `dataflow/README.md` — full dataflow architecture with diagrams
2. `docs/20260505/trades_aggfeatures_plan.md` — trades aggregate factor plan
3. `docs/20260524/aggregate_audit_implementation.md` — implemented ClockDriver aggregate audit path
4. `docs/20260524/bar_schema_alignment_design.md` — bar schema alignment constraints
5. `docs/20260528/exchange_time_engine_tutorial.md` — exchange-time bucket behavior and operational examples

### I want to understand audit verification

Read these files:

1. `dataflow/audit/config.py` — audit runtime knobs
2. `dataflow/audit/writer.py` — async JSONL writer, flush, and rotation
3. `examples/verifier/verify_binance_audit_vs_kline.py` — Binance official kline or `aggTrades` comparison
4. `examples/verifier/verify_okx_audit_vs_candles.py` — OKX official candle comparison
5. `examples/verifier/README.md` — usage and tolerance semantics

### I want to understand dynamic symbol lists

Read these files:

1. `docs/20260523/dynamic_symbol_list_design.md` — design proposal
2. `docs/20260525/dynamic_symbol_end_to_end_flow.md` — committed end-to-end flow
3. `examples/engine/dynanmicSymbols/engine_rt_binance_dynamic_symbols.py` — Binance runtime universe rotation
4. `examples/engine/timeBuckets/engine_rt_binance_dynamic_exchange_time.py` — dynamic universe plus exchange-time buckets
5. `dataflow/base_collector.py` — subscription lifecycle and rebuild mechanics

## 13. A practical reading order for new contributors

If you are new to the repo, this order is efficient:

1. `README.md`
2. `dataflow/README.md` — the dataflow architecture overview
3. `factorengine/engine.py` — focus on `_init_live()` and `_runtime_loop()`
4. `dataflow/clock_driver.py` — the aggregation center
5. `dataflow/base_collector.py` — shared WS reconnect pattern
6. `dataflow/okx/kline/worker.py` → `collector.py` → `adapter.py`
7. `dataflow/kline/publisher.py` — shared round batching
8. `dataflow/okx/trades/worker.py` → `dataflow/trades/accumulator.py`
9. `dataflow/binance/trades/worker.py` → `collector.py` — see how Binance mirrors OKX
10. `docs/20260524/aggregate_audit_implementation.md`
11. `docs/20260528/exchange_time_engine_tutorial.md`
12. `examples/engine/timeBuckets/engine_rt_binance_exchange_time.py`
13. `examples/verifier/README.md`
14. `examples/engine/staticSymbols/engine_rt_binance_trades.py`
15. `factorengine/factors/registry.py`
16. `native/include/fe/runtime/`

That gives you the control plane first, then the ClockDriver coordination, the shared components, each exchange's data pipeline, and finally the factor runtime.

## 14. Common source of confusion

### Which factor execution path is current

`fe_runtime` plus `factorengine.Engine` is the current high-performance path. Older Python-first scheduler notes may appear in historical design docs, but the active package no longer exposes a `factorengine.scheduler` module.

### Why both Python and C++ exist

Because the responsibilities are different:

- Python is good for I/O orchestration, registration, snapshots, tests, and developer ergonomics.
- C++ is used for the hot loop where incremental rolling computations and graph execution must stay fast.

### Live vs simulation: what's the same, what's different

The factor inference thread (`_runtime_loop`) is identical in both modes. The only difference is how `bar_queue` gets filled:

- **Live**: ClockDriver flushes registered workers → `_aggregate()` → `bar_queue`
- **Simulation**: `SimBarWorker` (synthetic generator → cache → queue)

This means any factor bug you find in simulation will also exist in live, and vice versa.

### Why audit logs only aggregate bars

The current audit path is designed for low-overhead operational checks. It records the same aggregate bars that ClockDriver sends to the runtime, so it can verify time alignment, OHLCV shape, missing minutes, and large drift against external references.

It does not prove every raw trade was processed correctly. For that level of proof, the system would need selected raw trade logging or replay from exchange historical trade APIs.

### Why ClockDriver instead of letting each worker push to bar_queue

Three reasons:

1. **Time alignment**: all data sources are flushed at the same wall-clock boundary, ensuring the factor DAG sees a consistent snapshot across kline, trades, orderbook, etc.
2. **Extensibility**: adding a new data source is `register("name", worker)` — no changes to existing workers or the inference thread.
3. **Decoupling**: workers don't know about each other or about `bar_queue`. This prevents the coupling that made the earlier Phase 1 architecture fragile.

### Why shared components instead of per-exchange copies

`KlinePublisher` and `TradesAccumulator` are exchange-agnostic. They operate on standardized data (BarPoint arrays, price/size/side triples) and know nothing about OKX or Binance specifics. This means:

- Bug fixes in round batching or accumulation logic apply to all exchanges automatically
- The old OKX import paths (`dataflow.okx.kline.publisher`, `dataflow.okx.trades.accumulator`) still work via re-export shims — no breaking changes
- Adding a new exchange requires only exchange-specific collectors and adapters

### OKX vs Binance: what's the same, what's different

**Same**: `KlinePublisher`, `TradesAccumulator`, `ClockDriver`, `Engine._runtime_loop`, `BarData` format, factor inference, `signal_deque`.

**Different**: WS endpoints, message formats, subscription protocols, symbol naming, kline frequency limits (Binance has no sub-minute klines), buy/sell side field semantics.

## 15. Recommended next steps

After reading this tutorial, the most useful follow-up is usually one of these:

1. Run `examples/engine/staticSymbols/engine_rt_simulation.py` and inspect `Engine.get_factor_outputs()`.
2. Run `examples/engine/staticSymbols/engine_rt_live.py` to see real-time factor computation with live OKX data.
3. Run `examples/engine/staticSymbols/engine_rt_binance_trades.py` to see real-time factor computation with live Binance Futures data.
4. Run `examples/engine/staticSymbols/engine_rt_trades.py` to see OKX trades factors with the full ClockDriver pipeline.
5. Run `examples/engine/timeBuckets/engine_rt_binance_exchange_time.py` to see exchange-time trades bucketing.
6. Run `examples/engine/timeBuckets/engine_rt_okx_dynamic_exchange_time.py` to see dynamic symbols plus exchange-time bucketing on OKX.
7. Open `factorengine/engine.py` and trace `Engine.start()` → `_init_live()` → ClockDriver wiring.
8. Pick one factor group under `factorengine/factors/okx_perp/` and follow how it becomes a compiled `FactorGraph`.
9. Read `docs/20260524/aggregate_audit_implementation.md` for the audit call flow.
10. Run `examples/verifier/run.sh` or `examples/verifier/run_okx.sh` after you have a local audit file.
11. Read `docs/20260504/binance_dataflow_design.md` for how multi-exchange support was added.
