# FactorEngine

> **⚠️ This project is still in active development. APIs and architecture may change.**

Real-time factor computation stack for **quantitative trading**: a Python **dataflow + cache** layer for OKX perpetual swaps, 33 high-performance **C++ operator kernels** (`fe_ops`) exposed via pybind11, and a **DAG-based factor inference runtime** (`fe_runtime`) that pushes market data through compiled factor graphs in streaming mode.

For a repository walkthrough focused on module boundaries and reading order, see [`docs/TUTORIAL.md`](docs/TUTORIAL.md).

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Python layer                             │
│                                                             │
│  Dataflow (live / sim)   Factor Registry    Visualization   │
│  bars/trades/books       @register_factor   ASCII / DOT /   │
│  → ndarray caches        per platform       PNG graphs      │
└──────────────┬───────────────────┬──────────────────────────┘
               │                   │
               v                   v
┌──────────────────────┐  ┌───────────────────────────────────┐
│ Engine               │  │ C++ Runtime (fe_runtime)          │
│ get_data / snapshot  │  │                                   │
│                      │  │  InferenceEngine                  │
│                      │  │   └─ SymbolRunner (per symbol)    │
│                      │  │       └─ FactorGraph × N          │
│                      │  │           └─ push_bar() streaming │
└──────────────────────┘  └───────────────────────────────────┘
                                       │
                                       v
                          ┌───────────────────────────────────┐
                          │ C++ Kernels (fe_ops)              │
                          │ 33 operators: P0 + P1 + P2 + P3  │
                          │ header-only, O(1) amortized push  │
                          └───────────────────────────────────┘
```

### Core components

| Component | Description |
|-----------|-------------|
| **`dataflow/`** | Market data ingestion: live OKX WebSocket collectors (bars, trades, books) and simulation mode with synthetic data |
| **`factorengine.Engine`** | Single entry point: owns the dataflow manager, exposes `get_data` / `get_trade_data` / `get_book_data` snapshots |
| **`factorengine.scheduler`** | Python-first prototype: fixed-interval ticks, cache slicing, and factor hooks |
| **`fe_ops`** | 33 C++ operator kernels (P0–P3), batch array-level, pybind11 bindings |
| **`fe_runtime`** | DAG push-level runtime: `FactorGraph`, `SymbolRunner`, `InferenceEngine` |
| **`factorengine.factors`** | Python factor registry with platform subfolders, graph visualization |

## Quick start

One command builds C++ extensions and installs the package:

```bash
pip install -e ".[dev]"
```

This will:
1. Invoke CMake to compile `fe_ops` and `fe_runtime` (C++17 pybind11 modules)
2. Install the `factorengine` Python package in editable mode
3. Install dev dependencies (pytest, pandas, graphviz)

Prerequisites: **Python >= 3.11**, **CMake >= 3.16**, **C++17 compiler**, **pybind11** (`pip install pybind11`).

### Verify installation

```python
import fe_ops       # batch array-level kernels
import fe_runtime   # DAG runtime (FactorGraph, SymbolRunner, InferenceEngine)
from factorengine.factors import FactorRegistry
```

## Factor inference runtime

The runtime enables **streaming factor computation**: build a factor expression as a DAG, push market data bar-by-bar, and get factor values with zero Python overhead in the hot loop.

### Build and run a factor

```python
import fe_runtime as rt

# Factor: Div(Sub(close, Ma(close, 120)), TsStd(close, 60))
g = rt.FactorGraph()
c = g.add_input("close")
ma120 = g.add_rolling(rt.Op.MA, c, 120)
dev = g.add_binary(rt.Op.SUB, c, ma120)
vol = g.add_rolling(rt.Op.TS_STD, c, 60)
g.add_binary(rt.Op.DIV, dev, vol)
g.compile()

# Push bars one-by-one (streaming)
for i in range(n):
    g.push_bar(close[i], volume[i], open_[i], high[i], low[i], ret[i])
print(g.output())  # latest factor value
```

### Factor registry (multi-platform)

Factors are organized by platform in subfolders under `factorengine/factors/`:

```
factorengine/factors/
├── registry.py              # FactorRegistry + @register_factor
├── visualize.py             # graph visualization (ASCII / DOT / PNG)
├── okx_perp/                # OKX perpetual swap factors
│   └── factor_bank.py       # @register_factor("okx_perp", "0001") ...
├── binance_perp/            # (future)
└── stock_cn/                # (future)
```

```python
from factorengine.factors import FactorRegistry

reg = FactorRegistry()
reg.load_all()                           # load all platforms
reg.load_group("okx_perp")              # or just one platform
graphs = reg.build_group("okx_perp")    # {factor_id: FactorGraph}
g = reg.build("0001", group="okx_perp") # single factor
```

### InferenceEngine (multi-symbol × multi-factor)

```python
import fe_runtime as rt
from factorengine.factors import FactorRegistry

reg = FactorRegistry()
reg.load_group("okx_perp")

engine = rt.InferenceEngine()
for sym in ["BTC-USDT", "ETH-USDT"]:
    engine.add_symbol(sym)
    for fid, graph in reg.build_group("okx_perp").items():
        engine.add_factor(sym, fid, graph)

# Push bars per symbol
engine.push_bar("BTC-USDT", close, volume, open_, high, low, ret)
outputs = engine.get_outputs("BTC-USDT")  # list of factor values
```

### Factor graph visualization

```python
from factorengine.factors.visualize import print_graph, to_dot, render_graph

g = reg.build("0001", group="okx_perp")
print_graph(g)                           # ASCII to terminal
print(to_dot(g, title="Factor 0001"))    # Graphviz DOT source
render_graph(g, "factor_0001.png")       # PNG image (requires graphviz)
```

## Live engine (market data)

```python
from factorengine.engine import Engine

engine = Engine(
    symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    data_freq="5s",
    pull_interval="10s",
    bar_window_length=1000,
    trade_window_length=10000,
    book_history_length=1000,
    enable_trades=True,
    enable_books=True,
)
engine.start()

import time
while True:
    time.sleep(engine.pull_interval_seconds)
    bar_snapshot = engine.get_data()
    for sym, bars in bar_snapshot.items():
        print(sym, "bars", bars.shape)
```

### Simulation mode

```python
engine = Engine(
    symbols=["BTC-USDT-SWAP"],
    mode="simulation",
    sim_bar_interval=1.0,
    sim_seed=42,
)
engine.start()
```

## Native C++ kernels

**33 operators across 4 priority levels**, all numerically aligned with Python/pandas reference implementations.

| Priority | Operators | Count | Status |
|----------|-----------|-------|--------|
| **P0** — elementwise | `Neg`, `Abs`, `Log`, `Sqr`, `Inv`, `Sign`, `Tanh`, `SLog1p`, `Add`, `Sub`, `Mul`, `Div` | 12 | ✅ |
| **P1** — single-series rolling | `Ma`, `TsSum`, `TsStd`, `TsVari`, `Ema`, `TsMin`, `TsMax`, `TsRank`, `TsZscore`, `Delay`, `TsDiff`, `TsPct` | 12 | ✅ |
| **P2** — bivariate / special | `Corr`, `Autocorr`, `TsMinMaxDiff`, `TsSkew` | 4 | ✅ |
| **P3** — low-frequency | `TsMed`, `TsMad`, `TsWMA`, `TsMaxDiff`, `TsMinDiff` | 5 | ✅ |

Key algorithms: Fenwick Tree + coordinate compression (TsRank), monotonic deques (TsMin/TsMax), online Pearson (Corr), double-rolling (Autocorr), Fisher-Pearson moments (TsSkew).

**Representative speedups vs Python at n=1,440:**

| Operator | Speedup | | Operator | Speedup |
|----------|---------|-|----------|---------|
| Neg | 49× | | TsStd | 25× |
| Log | 70× | | Ema | 30× |
| Add | 57× | | TsZscore | 65× |
| Delay | 62× | | TsPct | 87× |
| Corr | 47× | | Autocorr | 29× |

Full benchmark data: `docs/20260418/cpp_kernel_progress_report.md`.

## Tests

```bash
# Run all tests
pytest tests/ -v

# Kernel alignment only
pytest tests/kernel/ -v

# Factor runtime integration
pytest tests/factors/ -v

# Benchmarks (standalone scripts)
python tests/kernel/benchmark/bench_ops.py
python tests/kernel/benchmark/bench_p2_ops.py
python tests/kernel/benchmark/bench_p3_ops.py

# Visualization demo
python tests/visualization/demo_visualize.py
```

## Project structure

```
FactorEngine/
├── setup.py                    # pip install -e . (CMake + pybind11 auto-build)
├── pyproject.toml              # build dependencies
│
├── dataflow/
│   ├── livetrading/            # OKX WS collectors, bars/trades/books, caches
│   └── simulation/             # synthetic dataflow for Engine(mode="simulation")
│
├── factorengine/
│   ├── engine.py               # Engine entry point
│   ├── scheduler/              # scheduler prototype
│   └── factors/
│       ├── registry.py         # FactorRegistry + @register_factor
│       ├── visualize.py        # graph visualization (ASCII / DOT / PNG)
│       └── okx_perp/           # OKX perpetual swap factor builders
│           └── factor_bank.py
│
├── native/
│   ├── CMakeLists.txt
│   ├── include/fe/
│   │   ├── ops/                # header-only C++ kernels (33 operators)
│   │   │   ├── unary.hpp       #   P0: Neg, Abs, Log, Sqr, Inv, Sign, Tanh, SLog1p
│   │   │   ├── binary.hpp      #   P0: Add, Sub, Mul, Div
│   │   │   ├── rolling_mean.hpp    # P1: Ma
│   │   │   ├── rolling_sum.hpp     # P1: TsSum
│   │   │   ├── rolling_std.hpp     # P1: TsStd, TsVari
│   │   │   ├── rolling_ema.hpp     # P1: Ema
│   │   │   ├── rolling_minmax.hpp  # P1: TsMin, TsMax
│   │   │   ├── rolling_rank.hpp    # P1: TsRank (Fenwick Tree)
│   │   │   ├── rolling_zscore.hpp  # P1: TsZscore
│   │   │   ├── shift.hpp           # P1: Delay, TsDiff, TsPct
│   │   │   ├── bivariate.hpp       # P2: Corr, Autocorr
│   │   │   ├── rolling_extremal.hpp # P2: TsMinMaxDiff; P3: TsMaxDiff, TsMinDiff
│   │   │   ├── rolling_skew.hpp    # P2: TsSkew
│   │   │   ├── rolling_median.hpp  # P3: TsMed, TsMad
│   │   │   └── rolling_wma.hpp     # P3: TsWMA
│   │   └── runtime/            # DAG push-level runtime
│   │       ├── factor_graph.hpp      # FactorGraph: DAG builder + push executor
│   │       ├── kernels.hpp           # push-level kernel adapters
│   │       ├── symbol_runner.hpp     # SymbolRunner: multi-factor per symbol
│   │       └── inference_engine.hpp  # InferenceEngine: multi-symbol orchestrator
│   └── pybind/
│       ├── fe_ops_bind.cpp     # pybind11 bindings for batch kernels
│       └── fe_runtime_bind.cpp # pybind11 bindings for DAG runtime
│
├── tests/
│   ├── kernel/
│   │   ├── test_ops_alignment.py     # P0+P1 alignment
│   │   ├── test_p2_alignment.py      # P2 alignment
│   │   ├── test_p3_alignment.py      # P3 alignment
│   │   ├── test_factor_graph.py      # FactorGraph DAG tests
│   │   ├── reference/ts_ops.py       # Python ground-truth
│   │   └── benchmark/                # performance benchmarks
│   ├── factors/
│   │   ├── test_real_factors.py      # end-to-end factor alignment (5 factors × 3 seeds)
│   │   └── test_inference_engine.py  # registry + SymbolRunner + InferenceEngine
│   ├── visualization/
│   │   └── demo_visualize.py         # visualization demo script
│   └── dataflow/                     # live / sim smoke tests
│
└── docs/                       # design notes, alignment reports, tutorials
```

## Requirements

- Python >= 3.11
- CMake >= 3.16, C++17 compiler
- `aiohttp`, `numpy`, `pybind11` (auto-installed by `pip install -e .`)
- Dev: `pytest`, `pandas`, `graphviz` (install with `pip install -e ".[dev]"`)

## Documentation

- [`docs/TUTORIAL.md`](docs/TUTORIAL.md) — quick repository walkthrough and module guide
- [`docs/20260419/engine_architecture_refactor.md`](docs/20260419/engine_architecture_refactor.md) — current engine architecture baseline

## License

MIT
