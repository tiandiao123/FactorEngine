# FactorEngine Tutorial

This document is a fast path for understanding the repository structure and the execution model of FactorEngine.

If you only read three files first, read these in order:

1. `factorengine/engine.py` — the actual runtime entry point.
2. `dataflow/README.md` — the dataflow architecture (exchange adapters, ClockDriver, shared components).
3. `docs/20260503/kline_v2/kline_to_factor_pipeline.md` — the live data pipeline (kline → factor inference).

## 1. What this project is

FactorEngine is a real-time factor computation stack for quantitative trading.

At a high level it combines:

- Python market-data collection via WebSocket (**OKX** and **Binance Futures**) — kline candles and real-time trades
- a Python factor registry and visualization layer (**123 factors** across 2 groups)
- C++ operator kernels for fast rolling/time-series math (**33 operators**)
- a C++ DAG runtime for streaming factor inference
- a unified ClockDriver that synchronizes and aggregates heterogeneous data streams
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
                      → _aggregate() → bar_queue.put()
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
2. **Thread 2 (exchange-trades)**: `TradesWorker` (OKX) or `BinanceTradesWorker` daemon thread — collects trades via WebSocket, routes each trade to per-symbol `TradesAccumulator` instances. Exposes `flush()` which snapshots all accumulators and returns `(round_bars, round_trades)`.
3. **Thread 3 (clock-driver)**: `ClockDriver` daemon thread — fires on wall-clock aligned boundaries (e.g. every 5 seconds at `:00`, `:05`, `:10`, …). Calls `flush()` on all registered workers, runs `_aggregate()` to merge kline + trades data (kline bars take priority per symbol), and pushes the unified payload to `bar_queue`.
4. **Thread 4 (factor-infer)**: `Engine._runtime_loop` daemon thread — consumes `bar_queue`, constructs `rt.BarData`, calls C++ `InferenceEngine.push_bars()`, stores results in `signal_deque`.
5. **Thread 5 (main)**: User strategy code — reads `engine.get_factor_outputs()` and `engine.get_data()`.

That separation matters because:
- Each data source (kline, trades, future: orderbook, funding rate) runs independently with its own WS connection and event loop
- ClockDriver ensures all data sources are flushed at the exact same wall-clock boundary, guaranteeing time alignment
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
- `scheduler/`: an older Python-first scheduler prototype. Useful for simple experiments but not the main high-performance path.

### `dataflow/`

This is the market-data side of the system. See [`dataflow/README.md`](../dataflow/README.md) for the full architecture document.

- `base_collector.py`: **BaseCollector ABC** — shared reconnect/lifecycle logic inherited by all four collectors.
- `clock_driver.py`: **ClockDriver** — the unified wall-clock aligned flush driver and aggregation center. Registered workers are flushed simultaneously on each tick, their outputs merged, and the result pushed to `bar_queue`. This is the single component that bridges workers and the inference thread.
- `kline/publisher.py`: **KlinePublisher** — exchange-agnostic round batching with timeout, forward-fill, and deduplication. Used by both OKX and Binance kline workers.
- `trades/accumulator.py`: **TradesAccumulator** — exchange-agnostic per-symbol O(1) streaming aggregation. Tracks vwap, buy_vol, sell_vol, trade_count, and derives OHLCV from trades. Used by both OKX and Binance trades workers.
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

- `engine_rt_live.py`: live mode connecting to OKX, streaming kline data, computing 70 factors for 3 symbols in real-time.
- `engine_rt_trades.py`: live OKX trades-only mode, computing 53 trades factors for 20 symbols at 1s frequency.
- `engine_rt_binance_trades.py`: live **Binance Futures** trades-only mode, computing 53 trades factors for 20 symbols using the production `aggTrade` stream.
- `engine_rt_simulation.py`: simulation mode with synthetic data, useful for testing and debugging.

### `tests/`

This repo is heavily test-driven across several slices:

- `tests/dataflow/`: kline adapter, publisher, worker, end-to-end pipeline, and ClockDriver tests
- `tests/dataflow/binance/`: Binance-specific tests (65 tests) — collector parsing, worker flush, Engine integration
- `tests/factors/`: registry and end-to-end factor inference
- `tests/kernel/`: C++ operator alignment and benchmarks
- `tests/runtime_engine/`: runtime-level integration tests (including trades integration and ClockDriver aggregation)
- `tests/trades/`: accumulator, collector, worker, and trades factor tests
- `tests/visualization/`: graph visualization demos

### `docs/`

This folder contains both stable onboarding docs and dated design notes.

- `TUTORIAL.md`: this file, intended as the quick orientation entry point
- `OPERATORS.md`: full C++ operator list with math formulas and test locations
- `20260504/binance_dataflow_design.md`: Binance Futures integration design and implementation notes
- `20260503/kline_v2/kline_to_factor_pipeline.md`: **comprehensive pipeline doc** — kline collection → aggregation → batching → factor inference, with architecture diagrams
- `20260503/kline_v2/round_batching_design.md`: design doc for Publisher round batching (v2)
- `20260503/trades_v1/trades_integration_design.md`: trades integration design (Phase 1)
- `20260503/trades_v1/trades_kline_full_pipeline.md`: full kline+trades pipeline walkthrough
- `20260503/trades_v2/clockdriver_refactor_plan.md`: ClockDriver refactor plan (Plan B — unified scheduling)
- `20260503/trades_v2/trades_v2_changelog.md`: trades v2 changelog / migration notes
- `20260419/engine_architecture_refactor.md`: engine architecture baseline
- earlier dated folders: design and implementation notes by milestone

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

### `factorengine.scheduler`

This is a minimal Python-side factor evaluation prototype.

It includes:

- `Scheduler`: a timer-driven callback loop
- `FactorRuntime`: pulls snapshots from `Engine`, slices windows, and computes values in Python
- `FactorSpec` and `FactorSnapshot`: lightweight factor definition and result containers

This part is useful for understanding the older pure-Python evaluation path and for rapid prototyping, but it is not the main high-throughput runtime.

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
4. Pushes the unified `(merged_bars, merged_trades)` payload to `bar_queue`

This design means adding a new data source (orderbook, funding rate) is as simple as:
```python
clock_driver.register("ob", orderbook_worker)
```

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
    → OXKTradesCollector (connect, subscribe, parse JSON)
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
```

### Simulation mode

1. `Engine` creates `SimDataflowManager`.
2. `SimBarWorker` generates deterministic synthetic bars.
3. Bars are written into `BarCache` and enqueued as complete rounds for inference.
4. The runtime thread computes factors the same way as in live mode.

That symmetry is why simulation mode is the easiest place to understand the full stack.

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

## 10. What to read depending on your goal

### I want to understand the public API

Read these files:

1. `README.md`
2. `factorengine/engine.py`
3. `examples/engine/engine_rt_live.py`
4. `examples/engine/engine_rt_binance_trades.py`

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
2. `docs/20260503/trades_v2/clockdriver_refactor_plan.md` — design rationale
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
2. `docs/20260503/kline_v2/kline_to_factor_pipeline.md` — OKX kline pipeline with diagrams
3. `docs/20260503/trades_v1/trades_kline_full_pipeline.md` — kline+trades pipeline walkthrough
4. `docs/20260503/trades_v2/clockdriver_refactor_plan.md` — ClockDriver unified architecture

### I want to understand the older Python prototype path

Read these files:

1. `factorengine/scheduler/runtime.py`
2. `factorengine/scheduler/scheduler.py`
3. `factorengine/scheduler/factor_spec.py`

## 11. A practical reading order for new contributors

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
10. `docs/20260503/kline_v2/kline_to_factor_pipeline.md`
11. `examples/engine/engine_rt_binance_trades.py`
12. `factorengine/factors/registry.py`
13. `native/include/fe/runtime/`

That gives you the control plane first, then the ClockDriver coordination, the shared components, each exchange's data pipeline, and finally the factor runtime.

## 12. Common source of confusion

### There are two factor execution ideas in the repo

Yes.

- `factorengine.scheduler` is the Python-first prototype path.
- `fe_runtime` plus `factorengine.Engine` is the current high-performance path.

When in doubt, treat `Engine` plus `fe_runtime` as the mainline architecture.

### Why both Python and C++ exist

Because the responsibilities are different:

- Python is good for I/O orchestration, registration, snapshots, tests, and developer ergonomics.
- C++ is used for the hot loop where incremental rolling computations and graph execution must stay fast.

### Live vs simulation: what's the same, what's different

The factor inference thread (`_runtime_loop`) is identical in both modes. The only difference is how `bar_queue` gets filled:

- **Live**: ClockDriver flushes registered workers → `_aggregate()` → `bar_queue`
- **Simulation**: `SimBarWorker` (synthetic generator → cache → queue)

This means any factor bug you find in simulation will also exist in live, and vice versa.

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

## 13. Recommended next steps

After reading this tutorial, the most useful follow-up is usually one of these:

1. Run `examples/engine/engine_rt_simulation.py` and inspect `Engine.get_factor_outputs()`.
2. Run `examples/engine/engine_rt_live.py` to see real-time factor computation with live OKX data.
3. Run `examples/engine/engine_rt_binance_trades.py` to see real-time factor computation with live Binance Futures data.
4. Run `examples/engine/engine_rt_trades.py` to see OKX trades factors with the full ClockDriver pipeline.
5. Open `factorengine/engine.py` and trace `Engine.start()` → `_init_live()` → ClockDriver wiring.
6. Pick one factor group under `factorengine/factors/okx_perp/` and follow how it becomes a compiled `FactorGraph`.
7. Read `docs/20260503/trades_v2/clockdriver_refactor_plan.md` for the full Plan B architecture rationale.
8. Read `docs/20260504/binance_dataflow_design.md` for how multi-exchange support was added.
