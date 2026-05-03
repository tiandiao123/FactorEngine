# FactorEngine Tutorial

This document is a fast path for understanding the repository structure and the execution model of FactorEngine.

If you only read three files first, read these in order:

1. `factorengine/engine.py` — the actual runtime entry point.
2. `docs/20260419/engine_architecture_refactor.md` — the architecture baseline.
3. `docs/20260503/kline_v2/kline_to_factor_pipeline.md` — the live data pipeline (OKX kline → factor inference).

## 1. What this project is

FactorEngine is a real-time factor computation stack for quantitative trading.

At a high level it combines:

- Python market-data collection via WebSocket (OKX) and cache management
- a Python factor registry and visualization layer
- C++ operator kernels for fast rolling/time-series math
- a C++ DAG runtime for streaming factor inference

The core idea is simple: market data arrives continuously via WebSocket push, the engine aggregates and batches it, and a factor runtime turns each new bar into updated factor values.

## 2. Mental model in 30 seconds

You can think about the system as a pipeline:

```text
OKX WebSocket (candle1s / candle1m / ...)
        |
        v
Collector → Adapter (filter + time-aligned aggregation)
        |
        v
Publisher (round batching: wait all symbols, timeout + forward-fill)
        |
        v
bar_queue (dict[str, ndarray] per round)
        |
        v
C++ inference runtime (push_bars → factor DAG → get_all_outputs)
        |
        v
signal_deque (latest factor outputs)
```

There are two ways to use the project:

1. Use `factorengine.Engine` as the top-level application object.
2. Use `fe_runtime` directly if you only care about factor DAG execution.

## 3. Current architecture

The current implementation is the v3 architecture (3-thread streaming model):

1. **Thread 1 (okx-kline)**: `KlineWorker` daemon thread — collects OKX WebSocket data, filters/aggregates via `KlineAdapter`, batches all symbols via `KlinePublisher`, pushes complete rounds into `bar_queue`.
2. **Thread 2 (factor-infer)**: `Engine._runtime_loop` daemon thread — consumes `bar_queue`, constructs `rt.BarData`, calls C++ `InferenceEngine.push_bars()`, stores results in `signal_deque`.
3. **Thread 3 (main)**: User strategy code — reads `engine.get_factor_outputs()` and `engine.get_data()`.

That separation matters because it keeps data ingestion independent from factor computation latency, and both independent from strategy execution.

## 4. Top-level repository map

### `factorengine/`

This is the main Python package.

- `engine.py`: the top-level `Engine` class. If you want to understand how the system boots, start here.
- `factors/`: factor registration, factor discovery, and graph visualization.
- `scheduler/`: an older Python-first scheduler prototype. Useful for simple experiments and for understanding pre-runtime ideas, but not the main high-performance path.

### `dataflow/`

This is the market-data side of the system.

- `okx/`: OKX exchange data streams (WebSocket-based).
  - `okx/common.py`: shared constants (endpoints, channel mappings, subscription limits).
  - `okx/kline/collector.py`: WebSocket connection, subscription, and JSON parsing.
  - `okx/kline/adapter.py`: candle filtering (confirmed only), time-aligned aggregation for sub-minute frequencies.
  - `okx/kline/publisher.py`: round batching — collects all symbols per time window, timeout + forward-fill for missing symbols.
  - `okx/kline/worker.py`: orchestrates collector → adapter → publisher in a daemon thread.
- `simulation/`: synthetic bar generation for local testing and runtime demos.
- `cache.py`: thread-safe rolling cache backed by numpy arrays.

This layer is responsible for producing structured numeric arrays (`BarPoint`), not for factor math.

### `native/`

This is the C++ implementation layer.

- `include/fe/ops/`: batch-style operator kernels such as `Ma`, `TsStd`, `Corr`, and `TsRank`.
- `include/fe/runtime/`: the streaming DAG runtime, including `FactorGraph`, `SymbolRunner`, and `InferenceEngine`.
- `pybind/`: Python bindings for the C++ modules.

If you care about performance or numerical behavior, this directory matters most.

### `examples/`

Runnable demos:

- `engine_rt_live.py`: live mode connecting to OKX, streaming kline data, computing 60 factors for 3 symbols in real-time.
- `engine_rt_simulation.py`: simulation mode with synthetic data, useful for testing and debugging.

### `tests/`

This repo is heavily test-driven across several slices:

- `tests/dataflow/`: kline adapter, publisher, worker, and end-to-end pipeline tests
- `tests/factors/`: registry and end-to-end factor inference
- `tests/kernel/`: C++ operator alignment and benchmarks
- `tests/runtime_engine/`: runtime-level integration tests
- `tests/visualization/`: graph visualization demos

### `docs/`

This folder contains both stable onboarding docs and dated design notes.

- `TUTORIAL.md`: this file, intended as the quick orientation entry point
- `20260503/kline_v2/kline_to_factor_pipeline.md`: **comprehensive pipeline doc** — OKX kline collection → aggregation → batching → factor inference, with architecture diagrams
- `20260503/kline_v2/round_batching_design.md`: design doc for Publisher round batching (v2)
- `20260503/kline_v1/`: initial kline streaming refactor design
- `20260419/engine_architecture_refactor.md`: engine architecture baseline
- `20260419/`: performance and C++ kernel progress reports
- earlier dated folders: design and implementation notes by milestone

## 5. The main Python modules

### `factorengine.engine`

This is the public entry point.

What it owns:

- mode selection: live or simulation
- construction of the proper dataflow (live: `KlineWorker`, simulation: `SimDataflowManager`)
- optional initialization of the C++ `InferenceEngine`
- the queue between dataflow and runtime
- the deque holding latest factor outputs
- snapshot APIs like `get_data()` and `get_factor_outputs()`

If you want to know how the system actually runs, this file is the most important place to read.

### `factorengine.factors`

This package defines how factors are organized on the Python side.

Key pieces:

- `registry.py`: the `FactorRegistry` and `@register_factor` decorator
- `visualize.py`: ASCII, DOT, and PNG graph rendering helpers
- `okx_perp/`: the current factor group implemented in the repo (60 factors)

The registry does not execute factors itself. Its job is to discover builder functions and return compiled `FactorGraph` objects that are later handed to `fe_runtime`.

### `factorengine.scheduler`

This is a minimal Python-side factor evaluation prototype.

It includes:

- `Scheduler`: a timer-driven callback loop
- `FactorRuntime`: pulls snapshots from `Engine`, slices windows, and computes values in Python
- `FactorSpec` and `FactorSnapshot`: lightweight factor definition and result containers

This part is useful for understanding the older pure-Python evaluation path and for rapid prototyping, but it is not the main high-throughput runtime.

## 6. The dataflow modules

### `dataflow.okx`

This is the OKX live market-data stack (v2.1, streaming with time-aligned aggregation).

The pipeline consists of four components chained together:

```text
OKX WebSocket
    → OXKlineCollector (connect, subscribe, parse JSON)
    → KlineAdapter (filter unconfirmed, time-aligned aggregation)
    → KlinePublisher (round batching, timeout, forward-fill)
    → bar_queue
```

Important files:

- `common.py`: WS/REST endpoints, channel resolution (`resolve_bar_channel`), subscription limits
- `kline/collector.py`: async WebSocket client with auto-reconnect. Subscribes up to 200 symbols per connection, receives server-pushed candle updates
- `kline/adapter.py`: filters unconfirmed candles (`confirm != "1"`), aggregates 1s candles into N-second bars using time-aligned windows (`ts % (freq * 1000)`) for sub-minute frequencies
- `kline/publisher.py`: collects all symbols' bars into a round, flushes when complete or on timeout (forward-fills missing symbols from last round)
- `kline/worker.py`: orchestrates the above in a daemon thread running an asyncio event loop

Key design decisions:

- **Time-aligned aggregation**: all symbols share the same window boundaries, ensuring aggregated bars always have matching timestamps across symbols
- **Round batching**: the engine receives a complete `dict[str, ndarray]` with all symbols per round, matching simulation behavior
- **Forward-fill on timeout**: if a symbol is missing after `batch_timeout_s`, its last known bar is used, keeping the factor DAG's rolling windows continuous

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

When the Python engine pushes bar batches into `fe_runtime`, this is the code that updates factor values incrementally.

## 8. How data moves through the system

### Live mode

```text
Thread 1 (okx-kline daemon):
  OKX WS → Collector (parse JSON, on_candle callback)
         → Adapter (filter unconfirmed, aggregate to N-second bars)
         → Publisher (batch all symbols into one round)
         → bar_queue.put({sym1: bar, sym2: bar, ...})

Thread 2 (factor-infer daemon):
  bar_queue.get() → construct rt.BarData per symbol
                  → inference.push_bars(batch)  [C++ parallel]
                  → inference.get_all_outputs()
                  → signal_deque.append(result)

Thread 3 (main / strategy):
  engine.get_factor_outputs()  → read signal_deque[-1]
  engine.get_data()            → read debug_cache snapshot
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
3. `examples/engine_rt_live.py`

### I want to understand factor execution

Read these files:

1. `factorengine/factors/registry.py`
2. `native/include/fe/runtime/factor_graph.hpp`
3. `native/include/fe/runtime/inference_engine.hpp`

### I want to understand live market data collection

Read these files:

1. `dataflow/okx/kline/worker.py` — the orchestrator
2. `dataflow/okx/kline/collector.py` — WebSocket connection
3. `dataflow/okx/kline/adapter.py` — filtering and aggregation
4. `dataflow/okx/kline/publisher.py` — round batching
5. `docs/20260503/kline_v2/kline_to_factor_pipeline.md` — full pipeline walkthrough

### I want to understand the complete live pipeline end-to-end

Read this single document:

1. `docs/20260503/kline_v2/kline_to_factor_pipeline.md` — covers OKX WS → Collector → Adapter → Publisher → bar_queue → C++ inference → signal_deque → strategy, with diagrams

### I want to understand the older Python prototype path

Read these files:

1. `factorengine/scheduler/runtime.py`
2. `factorengine/scheduler/scheduler.py`
3. `factorengine/scheduler/factor_spec.py`

## 10. A practical reading order for new contributors

If you are new to the repo, this order is efficient:

1. `README.md`
2. `docs/20260419/engine_architecture_refactor.md`
3. `factorengine/engine.py`
4. `dataflow/okx/kline/worker.py` → `collector.py` → `adapter.py` → `publisher.py`
5. `docs/20260503/kline_v2/kline_to_factor_pipeline.md`
6. `examples/engine_rt_live.py`
7. `factorengine/factors/registry.py`
8. `native/include/fe/runtime/`

That gives you the control plane first, then the live data pipeline, then the factor runtime.

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

- **Live**: `KlineWorker` (OKX WebSocket → adapter → publisher → queue)
- **Simulation**: `SimBarWorker` (synthetic generator → cache → queue)

This means any factor bug you find in simulation will also exist in live, and vice versa.

## 12. Recommended next steps

After reading this tutorial, the most useful follow-up is usually one of these:

1. Run `examples/engine_rt_simulation.py` and inspect `Engine.get_factor_outputs()`.
2. Run `examples/engine_rt_live.py` to see real-time factor computation with live OKX data.
3. Open `factorengine/engine.py` and trace `Engine.start()` to the runtime thread.
4. Pick one factor group under `factorengine/factors/okx_perp/` and follow how it becomes a compiled `FactorGraph`.
5. Read `docs/20260503/kline_v2/kline_to_factor_pipeline.md` for the full live pipeline with diagrams.
