# FactorEngine Tutorial

This document is a fast path for understanding the repository structure and the execution model of FactorEngine.

If you only read three files first, read these in order:

1. `factorengine/engine.py` — the actual runtime entry point.
2. `docs/20260419/engine_architecture_refactor.md` — the architecture baseline.
3. `docs/20260503/kline_v2/kline_to_factor_pipeline.md` — the live data pipeline (OKX kline → factor inference).

## 1. What this project is

FactorEngine is a real-time factor computation stack for quantitative trading.

At a high level it combines:

- Python market-data collection via WebSocket (OKX) — kline candles and real-time trades
- a Python factor registry and visualization layer
- C++ operator kernels for fast rolling/time-series math
- a C++ DAG runtime for streaming factor inference
- a unified ClockDriver that synchronizes and aggregates heterogeneous data streams

The core idea is simple: market data arrives continuously via WebSocket push, independent workers collect and buffer each data stream, a ClockDriver periodically flushes all workers on wall-clock boundaries, aggregates the results, and a factor runtime turns each merged bar into updated factor values.

## 2. Mental model in 30 seconds

You can think about the system as a pipeline:

```text
OKX WebSocket (candle1s / candle1m / ...)   OKX WebSocket (trades)
        |                                          |
        v                                          v
   KlineWorker                               TradesWorker
   (Collector → Adapter → Publisher)         (Collector → Accumulators)
        |                                          |
        v                                          v
        └──────────── ClockDriver ─────────────────┘
                      (wall-clock aligned flush)
                      kline_data = kline_worker.flush()
                      trades_data = trades_worker.flush()
                      → _aggregate() → bar_queue.put()
                              |
                              v
               C++ inference runtime
               (push_bars → factor DAG → get_all_outputs)
                              |
                              v
                      signal_deque (latest factor outputs)
```

There are two ways to use the project:

1. Use `factorengine.Engine` as the top-level application object.
2. Use `fe_runtime` directly if you only care about factor DAG execution.

## 3. Current architecture

The current implementation is the v4 architecture (4+ thread streaming model with ClockDriver):

1. **Thread 1 (okx-kline)**: `KlineWorker` daemon thread — collects OKX WebSocket candle data, filters/aggregates via `KlineAdapter`, batches all symbols via `KlinePublisher` into an internal buffer. Does not push to any queue. Exposes `flush()` for ClockDriver.
2. **Thread 2 (okx-trades)**: `TradesWorker` daemon thread — collects OKX trades via WebSocket, routes each trade to per-symbol `TradesAccumulator` instances. Exposes `flush()` which snapshots all accumulators and returns `(round_bars, round_trades)`.
3. **Thread 3 (clock-driver)**: `ClockDriver` daemon thread — fires on wall-clock aligned boundaries (e.g. every 5 seconds at `:00`, `:05`, `:10`, …). Calls `flush()` on all registered workers, runs `_aggregate()` to merge kline + trades data (kline bars take priority per symbol), and pushes the unified payload to `bar_queue`.
4. **Thread 4 (factor-infer)**: `Engine._runtime_loop` daemon thread — consumes `bar_queue`, constructs `rt.BarData`, calls C++ `InferenceEngine.push_bars()`, stores results in `signal_deque`.
5. **Thread 5 (main)**: User strategy code — reads `engine.get_factor_outputs()` and `engine.get_data()`.

That separation matters because:
- Each data source (kline, trades, future: orderbook, funding rate) runs independently with its own WS connection and event loop
- ClockDriver ensures all data sources are flushed at the exact same wall-clock boundary, guaranteeing time alignment
- Adding a new data source (e.g. orderbook) only requires implementing a worker with a `flush()` method and calling `clock_driver.register("ob", ob_worker)`
- Factor computation is completely decoupled from data ingestion

## 4. Top-level repository map

### `factorengine/`

This is the main Python package.

- `engine.py`: the top-level `Engine` class. If you want to understand how the system boots, start here.
- `factors/`: factor registration, factor discovery, and graph visualization.
  - `okx_perp/`: 60 kline-based factors
  - `okx_perp_trades/`: 6 trades-based factors (using `vwap`, `buy_vol`, `sell_vol` inputs)
- `scheduler/`: an older Python-first scheduler prototype. Useful for simple experiments but not the main high-performance path.

### `dataflow/`

This is the market-data side of the system.

- `clock_driver.py`: **ClockDriver** — the unified wall-clock aligned flush driver and aggregation center. Registered workers are flushed simultaneously on each tick, their outputs merged, and the result pushed to `bar_queue`. This is the single component that bridges workers and the inference thread.
- `okx/`: OKX exchange data streams (WebSocket-based).
  - `okx/common.py`: shared constants (endpoints, channel mappings, subscription limits).
  - `okx/kline/collector.py`: WebSocket connection, subscription, and JSON parsing.
  - `okx/kline/adapter.py`: candle filtering (confirmed only), time-aligned aggregation for sub-minute frequencies.
  - `okx/kline/publisher.py`: round batching — collects all symbols per time window, timeout + forward-fill for missing symbols. Exposes `flush_for_clock()` for ClockDriver.
  - `okx/kline/worker.py`: orchestrates collector → adapter → publisher in a daemon thread. Exposes `flush()` which delegates to `publisher.flush_for_clock()`.
  - `okx/trades/collector.py`: trades WebSocket client, parses and dispatches individual trades.
  - `okx/trades/accumulator.py`: per-symbol O(1) streaming aggregation — tracks `vwap`, `buy_vol`, `sell_vol`, `trade_count`, and OHLCV derived from trades. Thread-safe via `threading.Lock`.
  - `okx/trades/worker.py`: orchestrates collector → accumulators in a daemon thread. `flush()` snapshots all accumulators and returns `(round_bars, round_trades)`.
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

- `engine_rt_live.py`: live mode connecting to OKX, streaming kline data, computing 60 factors for 3 symbols in real-time.
- `engine_rt_trades.py`: live mode with kline + trades data, computing 66 factors (kline + trades) using `data_source="kline_trades"`.
- `engine_rt_simulation.py`: simulation mode with synthetic data, useful for testing and debugging.

### `tests/`

This repo is heavily test-driven across several slices:

- `tests/dataflow/`: kline adapter, publisher, worker, end-to-end pipeline, and ClockDriver tests
- `tests/factors/`: registry and end-to-end factor inference
- `tests/kernel/`: C++ operator alignment and benchmarks
- `tests/runtime_engine/`: runtime-level integration tests (including trades integration and ClockDriver aggregation)
- `tests/trades/`: accumulator, collector, worker, and trades factor tests
- `tests/visualization/`: graph visualization demos

### `docs/`

This folder contains both stable onboarding docs and dated design notes.

- `TUTORIAL.md`: this file, intended as the quick orientation entry point
- `20260503/kline_v2/kline_to_factor_pipeline.md`: **comprehensive pipeline doc** — OKX kline collection → aggregation → batching → factor inference, with architecture diagrams
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
- construction of the proper dataflow workers (live: `KlineWorker`, `TradesWorker`; simulation: `SimDataflowManager`)
- `ClockDriver` creation and worker registration based on `data_source`
- optional initialization of the C++ `InferenceEngine`
- the queue between ClockDriver and runtime thread
- the deque holding latest factor outputs
- snapshot APIs like `get_data()` and `get_factor_outputs()`

If you want to know how the system actually runs, this file is the most important place to read.

### `factorengine.factors`

This package defines how factors are organized on the Python side.

Key pieces:

- `registry.py`: the `FactorRegistry` and `@register_factor` decorator
- `visualize.py`: ASCII, DOT, and PNG graph rendering helpers
- `okx_perp/`: 60 kline-based factors (inputs: close, volume, open, high, low, ret)
- `okx_perp_trades/`: 6 trades-based factors (inputs: vwap, buy_vol, sell_vol)

The registry does not execute factors itself. Its job is to discover builder functions and return compiled `FactorGraph` objects that are later handed to `fe_runtime`.

### `factorengine.scheduler`

This is a minimal Python-side factor evaluation prototype.

It includes:

- `Scheduler`: a timer-driven callback loop
- `FactorRuntime`: pulls snapshots from `Engine`, slices windows, and computes values in Python
- `FactorSpec` and `FactorSnapshot`: lightweight factor definition and result containers

This part is useful for understanding the older pure-Python evaluation path and for rapid prototyping, but it is not the main high-throughput runtime.

## 6. The dataflow modules

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

### `dataflow.okx.kline`

This is the OKX kline data stack.

The pipeline within the worker thread:

```text
OKX WebSocket
    → OKXKlineCollector (connect, subscribe, parse JSON)
    → KlineAdapter (filter unconfirmed, time-aligned aggregation)
    → KlinePublisher (round batching, timeout, forward-fill → internal buffer)
```

The publisher no longer pushes to `bar_queue` directly. Instead, it buffers complete rounds internally, and ClockDriver calls `worker.flush()` → `publisher.flush_for_clock()` to retrieve the latest data.

Key design decisions:

- **Time-aligned aggregation**: all symbols share the same window boundaries, ensuring aggregated bars always have matching timestamps across symbols
- **Round batching**: the publisher collects all symbols per time window before marking a round complete
- **Forward-fill on timeout**: if a symbol is missing, its last known bar is used
- **Three flush paths**: (1) ready buffer (normal), (2) pending + fill (partial), (3) full forward-fill from last round (rare, when kline WS is delayed)

### `dataflow.okx.trades`

This is the OKX trades data stack.

The pipeline within the worker thread:

```text
OKX WebSocket
    → OKXTradesCollector (connect, subscribe, parse JSON)
    → per-symbol TradesAccumulator (O(1) streaming aggregation)
```

Each trade updates the accumulator's running `vwap`, `buy_vol`, `sell_vol`, `trade_count`, and OHLCV fields. When ClockDriver calls `worker.flush()`, all accumulators are atomically snapshotted and reset, returning `(round_bars, round_trades)`.

The worker does not push to `bar_queue` — that is ClockDriver's job.

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

### Live mode (v4 — ClockDriver)

```text
Thread 1 (okx-kline daemon):
  OKX WS → Collector (parse JSON, on_candle callback)
         → Adapter (filter unconfirmed, aggregate to N-second bars)
         → Publisher (batch all symbols → internal _ready_round buffer)

Thread 2 (okx-trades daemon):
  OKX WS → Collector (parse JSON, on_trade callback)
         → per-symbol TradesAccumulator.update(px, sz, side)

Thread 3 (clock-driver daemon):
  every 5s (wall-clock aligned):
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

## 9. What to read depending on your goal

### I want to understand the public API

Read these files:

1. `README.md`
2. `factorengine/engine.py`
3. `examples/engine/engine_rt_live.py`
4. `examples/engine/engine_rt_trades.py`

### I want to understand factor execution

Read these files:

1. `factorengine/factors/registry.py`
2. `native/include/fe/runtime/factor_graph.hpp`
3. `native/include/fe/runtime/inference_engine.hpp`

### I want to understand live market data collection

Read these files:

1. `dataflow/clock_driver.py` — the central coordination piece
2. `dataflow/okx/kline/worker.py` — kline data worker
3. `dataflow/okx/trades/worker.py` — trades data worker
4. `dataflow/okx/kline/publisher.py` — round batching and `flush_for_clock()`
5. `dataflow/okx/trades/accumulator.py` — O(1) streaming trades aggregation
6. `docs/20260503/kline_v2/kline_to_factor_pipeline.md` — full pipeline walkthrough

### I want to understand how ClockDriver aggregates data

Read these files:

1. `dataflow/clock_driver.py` — `register()`, `_flush_and_aggregate()`, `_aggregate()`
2. `docs/20260503/trades_v2/clockdriver_refactor_plan.md` — design rationale
3. `factorengine/engine.py` — `_init_live()` to see how workers are registered

### I want to understand the complete live pipeline end-to-end

Read these documents:

1. `docs/20260503/kline_v2/kline_to_factor_pipeline.md` — OKX kline pipeline with diagrams
2. `docs/20260503/trades_v1/trades_kline_full_pipeline.md` — kline+trades pipeline walkthrough
3. `docs/20260503/trades_v2/clockdriver_refactor_plan.md` — ClockDriver unified architecture

### I want to understand the older Python prototype path

Read these files:

1. `factorengine/scheduler/runtime.py`
2. `factorengine/scheduler/scheduler.py`
3. `factorengine/scheduler/factor_spec.py`

## 10. A practical reading order for new contributors

If you are new to the repo, this order is efficient:

1. `README.md`
2. `docs/20260419/engine_architecture_refactor.md`
3. `factorengine/engine.py` — focus on `_init_live()` and `_runtime_loop()`
4. `dataflow/clock_driver.py` — the aggregation center
5. `dataflow/okx/kline/worker.py` → `collector.py` → `adapter.py` → `publisher.py`
6. `dataflow/okx/trades/worker.py` → `accumulator.py` → `collector.py`
7. `docs/20260503/kline_v2/kline_to_factor_pipeline.md`
8. `examples/engine/engine_rt_trades.py`
9. `factorengine/factors/registry.py`
10. `native/include/fe/runtime/`

That gives you the control plane first, then the ClockDriver coordination, then each data pipeline, and finally the factor runtime.

## 11. Common source of confusion

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

## 12. Recommended next steps

After reading this tutorial, the most useful follow-up is usually one of these:

1. Run `examples/engine/engine_rt_simulation.py` and inspect `Engine.get_factor_outputs()`.
2. Run `examples/engine/engine_rt_live.py` to see real-time factor computation with live OKX data.
3. Run `examples/engine/engine_rt_trades.py` to see kline + trades factors with the full ClockDriver pipeline.
4. Open `factorengine/engine.py` and trace `Engine.start()` → `_init_live()` → ClockDriver wiring.
5. Pick one factor group under `factorengine/factors/okx_perp/` and follow how it becomes a compiled `FactorGraph`.
6. Read `docs/20260503/trades_v2/clockdriver_refactor_plan.md` for the full Plan B architecture rationale.
