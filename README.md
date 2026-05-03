# FactorEngine

> **This project is under active development. APIs and layout may change.**

Real-time factor stack for **quantitative trading**: Python **market data + queues**, **33 C++ operator kernels** (`fe_ops`, pybind11), and a **DAG streaming runtime** (`fe_runtime`) that evaluates compiled factor graphs bar-by-bar with minimal Python overhead in the hot path.

For a reading-order tour of the repo, see [`docs/TUTORIAL.md`](docs/TUTORIAL.md) (note: some tutorial sections still mention legacy `livetrading` paths; the live stack today lives under `dataflow/okx/kline/`).

## Architecture (current)

Top-level [`factorengine.engine.Engine`](factorengine/engine.py) wires three logical paths:

1. **Dataflow thread** — OKX WebSocket klines (`dataflow/okx/kline/`) in **live** mode, or synthetic bars (`dataflow/simulation/`) in **simulation** mode. Completed bar **rounds** are pushed to a bounded queue when factor inference is enabled.
2. **Runtime thread** — pulls `round_bars` from the queue, builds `fe_runtime.BarData` batches, calls `InferenceEngine.push_bars`, and appends snapshots to an in-memory **signal deque** (latest factor values per symbol).
3. **Main thread** — reads **`get_factor_outputs()`** (O(1), no extra compute) and optionally **`get_data()`** for a numpy bar-window snapshot (debug / monitoring; backed by `BarCache` in live mode when enabled).

```
┌─────────────────────────────────────────────────────────────────┐
│  Python: OKX kline WS  OR  simulation bars                     │
│       → KlineWorker / SimDataflowManager                        │
│       → bar_queue (round_bars per wake)  [when factor_group set] │
└───────────────────────────────┬─────────────────────────────────┘
                                v
┌─────────────────────────────────────────────────────────────────┐
│  Thread: factor-infer                                           │
│       bar_queue → InferenceEngine.push_bars → signal_deque      │
└───────────────────────────────┬─────────────────────────────────┘
                                v
┌─────────────────────────────────────────────────────────────────┐
│  C++: fe_runtime (FactorGraph, SymbolRunner, InferenceEngine)   │
│       └─ fe_ops kernels (rolling / bivariate / …)              │
└─────────────────────────────────────────────────────────────────┘
```

### Core pieces

| Area | Role |
|------|------|
| **`dataflow/okx/kline/`** | OKX candle WebSocket: `collector` → `publisher` / `adapter` → `worker`; pushes completed rounds to `Engine`’s queue. |
| **`dataflow/simulation/`** | Fake bar stream for tests and demos without the exchange. |
| **`dataflow/cache.py`** | Rolling `BarCache` for optional numpy snapshots (`get_data`). |
| **`factorengine.Engine`** | Starts workers + optional inference thread; **`get_factor_outputs`**, **`get_data`**, **`kline_stats`** (live). |
| **`fe_ops` / `fe_runtime`** | Batch kernels and DAG **streaming** inference (`FactorGraph`, `InferenceEngine`). |
| **`factorengine/factors/`** | `@register_factor`, `FactorRegistry`, graph visualization. |
| **`factorengine/scheduler/`** | Older Python-side scheduler experiments (not the main C++ hot path). |

Design notes for the kline → factor pipeline and round batching: [`docs/20260503/kline_v2/kline_to_factor_pipeline.md`](docs/20260503/kline_v2/kline_to_factor_pipeline.md), [`docs/20260503/kline_v2/round_batching_design.md`](docs/20260503/kline_v2/round_batching_design.md).

## Quick start

Install in editable mode (builds C++ extensions via CMake):

```bash
pip install -e ".[dev]"
```

This compiles `native/` (CMake + pybind11), installs `factorengine`, and pulls dev deps (`pytest`, `pandas`, `graphviz`).

**Prerequisites:** Python **≥ 3.11**, **CMake ≥ 3.16**, **C++17** toolchain, **pybind11** (pulled in via setuptools / pip).

### Smoke imports

```python
import fe_ops
import fe_runtime
from factorengine.factors import FactorRegistry
```

## Factor runtime (DAG streaming)

Build a graph, compile, push bars one at a time, read the output:

```python
import fe_runtime as rt

g = rt.FactorGraph()
c = g.add_input("close")
ma120 = g.add_rolling(rt.Op.MA, c, 120)
dev = g.add_binary(rt.Op.SUB, c, ma120)
vol = g.add_rolling(rt.Op.TS_STD, c, 60)
g.add_binary(rt.Op.DIV, dev, vol)
g.compile()

for i in range(n):
    g.push_bar(close[i], volume[i], open_[i], high[i], low[i], ret[i])
print(g.output())
```

### Registry (multi-platform groups)

```python
from factorengine.factors import FactorRegistry

reg = FactorRegistry()
reg.load_all()                          # or reg.load_group("okx_perp")
graphs = reg.build_group("okx_perp")    # {factor_id: FactorGraph}
```

Factors live under `factorengine/factors/<platform>/` (e.g. `okx_perp/factor_bank.py`).

### `InferenceEngine` (multi-symbol × multi-factor)

```python
import fe_runtime as rt
from factorengine.factors import FactorRegistry

reg = FactorRegistry()
reg.load_group("okx_perp")

engine = rt.InferenceEngine()
for sym in ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]:
    engine.add_symbol(sym)
    for fid, graph in reg.build_group("okx_perp").items():
        engine.add_factor(sym, fid, graph)

# One bar at a time (scalars); batch alternative: engine.push_bars({sym: BarData(...), ...})
for i in range(n):
    engine.push_bar("BTC-USDT-SWAP", close[i], volume[i], open_[i], high[i], low[i], ret[i])

list(engine.get_outputs("BTC-USDT-SWAP"))   # latest factor values for one symbol
engine.get_all_outputs()                   # {symbol: {factor_id: value}}
```

### Graph visualization

```python
from factorengine.factors.visualize import print_graph, to_dot, render_graph

g = reg.build("0001", group="okx_perp")
print_graph(g)
print(to_dot(g, title="Factor 0001"))
render_graph(g, "factor_0001.png")   # needs graphviz installed
```

## `Engine`: live OKX kline + factors

The simplest end-to-end demo is [`examples/engine_rt_live.py`](examples/engine_rt_live.py). In **live** mode, the OKX kline worker and the inference queue are started when you pass a **`factor_group`** (otherwise the engine does not wire the kline streaming path to the factor runtime).

```python
from factorengine.engine import Engine

engine = Engine(
    symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    mode="live",
    data_freq="1s",
    factor_group="okx_perp",
    num_threads=4,
)
engine.start()

# Poll latest factor values (filled by the background inference thread)
factors = engine.get_factor_outputs()   # {symbol: {factor_id: float}}

# Optional: rolling bar window snapshot for debugging
bars = engine.get_data()

engine.stop()
```

## `Engine`: simulation

See [`examples/engine_rt_simulation.py`](examples/engine_rt_simulation.py):

```python
engine = Engine(
    symbols=["BTC-USDT-SWAP"],
    mode="simulation",
    sim_bar_interval=1.0,
    sim_seed=42,
    factor_group="okx_perp",
    num_threads=4,
)
engine.start()
# … get_factor_outputs(), get_data(), engine.stop()
```

## Native C++ kernels (`fe_ops`)

**33 operators** (P0–P3), numerically aligned with Python/pandas references where tested.

| Tier | Examples | Count |
|------|-----------|-------|
| **P0** elementwise | `Neg`, `Abs`, `Log`, `Add`, `Div`, … | 12 |
| **P1** single-series rolling | `Ma`, `TsStd`, `Ema`, `TsRank`, `Delay`, … | 12 |
| **P2** bivariate / special | `Corr`, `Autocorr`, `TsSkew`, … | 4 |
| **P3** heavier rolling | `TsMed`, `TsMad`, `TsWMA`, … | 5 |

More detail on kernels and alignment lives under `tests/kernel/` and dated notes such as [`docs/20260419/test_report.md`](docs/20260419/test_report.md).

## Tests

```bash
pytest tests/ -v

# Focused slices
pytest tests/kernel/ -v
pytest tests/factors/ -v
pytest tests/dataflow/ -v
pytest tests/runtime_engine/ -v

# Micro-benchmarks (optional)
python tests/kernel/benchmark/bench_ops.py
python tests/kernel/benchmark/bench_p2_ops.py
python tests/kernel/benchmark/bench_p3_ops.py

# Visualization
python tests/visualization/demo_visualize.py
```

## Repository layout (abbrev.)

```
FactorEngine/
├── setup.py                 # pip install -e . (CMake build_ext)
├── pyproject.toml
├── examples/
│   ├── engine_rt_live.py       # OKX kline + InferenceEngine
│   └── engine_rt_simulation.py
├── dataflow/
│   ├── cache.py
│   ├── okx/
│   │   ├── common.py          # bar channel / freq helpers
│   │   └── kline/             # WS collector, publisher, adapter, worker
│   └── simulation/
├── factorengine/
│   ├── engine.py
│   ├── factors/
│   └── scheduler/
├── native/                    # CMake, fe_ops + fe_runtime
└── tests/
    ├── kernel/
    ├── factors/
    ├── dataflow/
    ├── runtime_engine/
    └── visualization/
```

## Documentation

| Doc | Purpose |
|-----|---------|
| [`docs/TUTORIAL.md`](docs/TUTORIAL.md) | Repo orientation (partially historical paths). |
| [`docs/20260419/engine_architecture_refactor.md`](docs/20260419/engine_architecture_refactor.md) | Engine / threading baseline. |
| [`docs/20260503/kline_v2/`](docs/20260503/kline_v2/) | Current kline → factor pipeline write-ups. |

## Documentation

- [`docs/TUTORIAL.md`](docs/TUTORIAL.md) — quick repository walkthrough and module guide
- [`docs/20260419/engine_architecture_refactor.md`](docs/20260419/engine_architecture_refactor.md) — current engine architecture baseline

## License

MIT
