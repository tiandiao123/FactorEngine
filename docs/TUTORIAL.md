# FactorEngine Tutorial

This document is a fast path for understanding the repository structure and the execution model of FactorEngine.

If you only read three files first, read these in order:

1. `factorengine/engine.py` — the actual runtime entry point.
2. `docs/20260419/engine_architecture_refactor.md` — the current architecture baseline.
3. `README.md` — external-facing capabilities and install instructions.

## 1. What this project is

FactorEngine is a real-time factor computation stack for quantitative trading.

At a high level it combines:

- Python market-data collection and cache management
- a Python factor registry and visualization layer
- C++ operator kernels for fast rolling/time-series math
- a C++ DAG runtime for streaming factor inference

The core idea is simple: market data arrives continuously, the engine stores it in rolling caches, and a factor runtime turns each new bar into updated factor values.

## 2. Mental model in 30 seconds

You can think about the system as a pipeline:

```text
market data / simulation
        |
        v
dataflow workers
        |
        v
rolling numpy caches
        |
        +--> snapshots for Python-side consumers
        |
        v
bar queue
        |
        v
C++ inference runtime
        |
        v
latest factor outputs
```

There are two ways to use the project:

1. Use `factorengine.Engine` as the top-level application object.
2. Use `fe_runtime` directly if you only care about factor DAG execution.

## 3. Current architecture

The current implementation is the v2 architecture described in `docs/20260419/engine_architecture_refactor.md`.

The important design choice is that dataflow and factor inference are decoupled:

1. The dataflow thread collects or generates bars and writes them into cache.
2. The same dataflow thread pushes `round_bars` into a queue.
3. A dedicated runtime thread consumes that queue and calls the C++ inference engine.
4. The main thread reads the latest cached market snapshot and latest factor outputs.

That separation matters because it keeps data ingestion independent from factor computation latency.

## 4. Top-level repository map

### `factorengine/`

This is the main Python package.

- `engine.py`: the top-level `Engine` class. If you want to understand how the system boots, start here.
- `factors/`: factor registration, factor discovery, and graph visualization.
- `scheduler/`: an older Python-first scheduler prototype. Useful for simple experiments and for understanding pre-runtime ideas, but not the main high-performance path.

### `dataflow/`

This is the market-data side of the system.

- `livetrading/`: live data workers, collectors, caches, and manager objects.
- `simulation/`: synthetic bar generation for local testing and runtime demos.

This layer is responsible for producing structured numeric arrays, not for factor math.

### `native/`

This is the C++ implementation layer.

- `include/fe/ops/`: batch-style operator kernels such as `Ma`, `TsStd`, `Corr`, and `TsRank`.
- `include/fe/runtime/`: the streaming DAG runtime, including `FactorGraph`, `SymbolRunner`, and `InferenceEngine`.
- `pybind/`: Python bindings for the C++ modules.

If you care about performance or numerical behavior, this directory matters most.

### `examples/`

Runnable demos. `examples/engine_rt_demo.py` is the best quick example for the current engine plus runtime path.

### `tests/`

This repo is heavily test-driven across several slices:

- `tests/dataflow/`: cache and dataflow behavior
- `tests/factors/`: registry and end-to-end factor inference
- `tests/kernel/`: C++ operator alignment and benchmarks
- `tests/runtime_engine/`: runtime-level integration tests
- `tests/visualization/`: graph visualization demos

### `docs/`

This folder contains both stable onboarding docs and dated design notes.

- `TUTORIAL.md`: this file, intended as the quick orientation entry point
- `20260419/engine_architecture_refactor.md`: current engine architecture baseline
- `20260418/`: performance and C++ kernel progress reports
- earlier dated folders: design and implementation notes by milestone

## 5. The main Python modules

### `factorengine.engine`

This is the public entry point.

What it owns:

- mode selection: live or simulation
- construction of the proper dataflow manager
- optional initialization of the C++ `InferenceEngine`
- the queue between dataflow and runtime
- the deque holding latest factor outputs
- snapshot APIs like `get_data()`, `get_trade_data()`, `get_book_data()`, and `get_factor_outputs()`

If you want to know how the system actually runs, this file is the most important place to read.

### `factorengine.factors`

This package defines how factors are organized on the Python side.

Key pieces:

- `registry.py`: the `FactorRegistry` and `@register_factor` decorator
- `visualize.py`: ASCII, DOT, and PNG graph rendering helpers
- `okx_perp/`: the current factor group implemented in the repo

The registry does not execute factors itself. Its job is to discover builder functions and return compiled `FactorGraph` objects that are later handed to `fe_runtime`.

### `factorengine.scheduler`

This is a minimal Python-side factor evaluation prototype.

It includes:

- `Scheduler`: a timer-driven callback loop
- `FactorRuntime`: pulls snapshots from `Engine`, slices windows, and computes values in Python
- `FactorSpec` and `FactorSnapshot`: lightweight factor definition and result containers

This part is useful for understanding the older pure-Python evaluation path and for rapid prototyping, but it is not the main high-throughput runtime.

## 6. The dataflow modules

### `dataflow.livetrading`

This is the live market-data stack.

Important files:

- `manager.py`: creates and starts bars, trades, and books workers
- `cache.py`: thread-safe rolling caches backed by numpy arrays
- `bars/`, `trades/`, `books/`: stream-specific workers and processing logic
- `okx/`: OKX-specific wiring and channel handling

The caches are deliberately simple: append or extend numeric arrays, keep only the configured rolling window, and return copies for readers.

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

1. `Engine` creates `DataflowManager`.
2. Live workers subscribe to OKX channels and normalize incoming events.
3. Bars, trades, and books are appended into rolling caches.
4. If a factor group is enabled, bar rounds are forwarded to the runtime queue.
5. The runtime thread updates the C++ inference engine.
6. Your strategy code pulls cache snapshots and latest factor outputs.

### Simulation mode

1. `Engine` creates `SimDataflowManager`.
2. `SimBarWorker` generates deterministic synthetic bars.
3. Bars are written into `BarCache` and optionally enqueued for inference.
4. The runtime thread computes factors the same way as in live mode.

That symmetry is why simulation mode is the easiest place to understand the full stack.

## 9. What to read depending on your goal

### I want to understand the public API

Read these files:

1. `README.md`
2. `factorengine/engine.py`
3. `examples/engine_rt_demo.py`

### I want to understand factor execution

Read these files:

1. `factorengine/factors/registry.py`
2. `native/include/fe/runtime/factor_graph.hpp`
3. `native/include/fe/runtime/inference_engine.hpp`

### I want to understand market data and caches

Read these files:

1. `dataflow/livetrading/manager.py`
2. `dataflow/livetrading/cache.py`
3. `dataflow/simulation/manager.py`

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
4. `dataflow/simulation/manager.py`
5. `examples/engine_rt_demo.py`
6. `factorengine/factors/registry.py`
7. `native/include/fe/runtime/`

That gives you the control plane first, then the simulation path, then the factor runtime.

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

## 12. Recommended next steps

After reading this tutorial, the most useful follow-up is usually one of these:

1. Run `examples/engine_rt_demo.py` in simulation mode and inspect `Engine.get_factor_outputs()`.
2. Open `factorengine/engine.py` and trace `Engine.start()` to the runtime thread.
3. Pick one factor group under `factorengine/factors/okx_perp/` and follow how it becomes a compiled `FactorGraph`.
4. Read `docs/20260419/engine_architecture_refactor.md` to understand why the queue-based refactor happened.