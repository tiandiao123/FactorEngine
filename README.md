# FactorEngine

> **This project is under active development. APIs and layout may change.**

Real-time factor stack for **quantitative trading**: Python **market data (OKX kline + trades)**, **33 C++ operator kernels** (`fe_ops`, pybind11), and a **DAG streaming runtime** (`fe_runtime`) that evaluates compiled factor graphs bar-by-bar with minimal Python overhead in the hot path.

For a reading-order tour, see [`docs/TUTORIAL.md`](docs/TUTORIAL.md) (some paths are historical; live code lives under `dataflow/okx/kline/`, `dataflow/okx/trades/`, and `dataflow/clock_driver.py`).

## Architecture (v4 — ClockDriver unified flush)

Top-level [`factorengine.engine.Engine`](factorengine/engine.py) coordinates:

1. **Dataflow threads** — independent workers, each collecting one data stream:
   - **Kline** — `KlineWorker`: OKX candle WS → `KlineAdapter` → `KlinePublisher` (internal round buffer).
   - **Trades** — `TradesWorker`: OKX public trades WS → per-symbol `TradesAccumulator` (incremental `vwap`, `buy_vol`, `sell_vol`, OHLCV per bar bucket).
2. **ClockDriver thread** — the **single aggregation center**. Fires on wall-clock aligned boundaries (`:00`, `:05`, `:10`, …), calls `flush()` on every registered worker, merges results via `_aggregate()`, and pushes one `(merged_bars, merged_trades)` payload into `bar_queue`. Workers never touch `bar_queue`.
3. **Runtime thread** — `bar_queue.get()` → builds `fe_runtime.BarData` (OHLCV + `ret` + optional trades fields) → `InferenceEngine.push_bars` → **signal deque**.
4. **Main thread** — `get_factor_outputs()` (O(1)), optional `get_data()` (bar cache snapshot), `kline_stats` / `trades_stats` / `clock_stats`.

```
┌─────────────────────────────────────────────────────────────────────┐
│  okx-kline thread:  OKX candle WS → Adapter → Publisher (buffer)   │
│  okx-trades thread: OKX trades WS → per-symbol Accumulators        │
└─────────────────┬──────────────────────────┬────────────────────────┘
                  │                          │
                  v                          v
         ┌──────────────────────────────────────────┐
         │  ClockDriver (freq=5s, wall-clock aligned)│
         │    kline_data  = kline_worker.flush()     │
         │    trades_data = trades_worker.flush()    │
         │    → _aggregate(kline priority per sym)   │
         │    → bar_queue.put((merged_bars, trades)) │
         └────────────────────┬──────────────────────┘
                              v
┌─────────────────────────────────────────────────────────────────────┐
│  factor-infer thread:                                               │
│    bar_queue → BarData(close…ret, vwap, buy_vol, sell_vol)          │
│    → InferenceEngine.push_bars → signal_deque                       │
└─────────────────────────────────────────────────────────────────────┘
                              v
┌─────────────────────────────────────────────────────────────────────┐
│  C++ fe_runtime (FactorGraph, InferenceEngine) + fe_ops kernels     │
└─────────────────────────────────────────────────────────────────────┘
```

### `data_source` (live)

| Value | Workers started | Who flushes |
|-------|----------------|-------------|
| **`kline`** | KlineWorker | ClockDriver (kline only) |
| **`trades`** | TradesWorker | ClockDriver (trades only); OHLCV derived from trades |
| **`kline_trades`** | Both | ClockDriver (both); kline bars take priority per symbol, trades snapshots merged |

### Core pieces

| Area | Role |
|------|------|
| **`dataflow/okx/kline/`** | Candle WS: `collector` → `adapter` → `publisher`; `worker` exposes `flush()` for ClockDriver. |
| **`dataflow/okx/trades/`** | Trades WS: `collector` → `accumulator` → `worker`; `flush()` returns `(round_bars, round_trades)`. |
| **`dataflow/clock_driver.py`** | Unified wall-clock flush & aggregation center; `register(name, worker)` + `_aggregate()` → `bar_queue`. |
| **`dataflow/simulation/`** | Synthetic bars + optional `bar_queue` for tests/demos. |
| **`dataflow/cache.py`** | Rolling `BarCache` for `get_data()` snapshots. |
| **`factorengine.Engine`** | `factor_group` as **str or list** of registry groups; **`data_source`**; **`get_factor_outputs`**, **`get_data`**, **`kline_stats`**, **`trades_stats`**, **`clock_stats`**. |
| **`fe_ops` / `fe_runtime`** | Batch kernels + DAG streaming; **`BarData`** includes **`vwap`, `buy_vol`, `sell_vol`**; `add_input` supports those names. |
| **`factorengine/factors/`** | `FactorRegistry`, `@register_factor`, visualization — e.g. **`okx_perp`** (60 kbar factors), **`okx_perp_trades`** (6 trades factors). |
| **`factorengine/scheduler/`** | Older Python-side scheduler experiments (not the main C++ hot path). |

Design references: [`docs/20260503/kline_v2/`](docs/20260503/kline_v2/), [`docs/20260503/trades_v1/trades_integration_design.md`](docs/20260503/trades_v1/trades_integration_design.md), [`docs/20260503/trades_v2/`](docs/20260503/trades_v2/).

## Quick start

```bash
pip install -e ".[dev]"
```

Builds `native/` via CMake + pybind11, installs `factorengine`, dev deps (`pytest`, `pandas`, `graphviz`).

**Prerequisites:** Python **≥ 3.11**, **CMake ≥ 3.16**, **C++17**, **pybind11**.

### Smoke imports

```python
import fe_ops
import fe_runtime
from factorengine.factors import FactorRegistry
```

## Factor runtime (DAG streaming)

`FactorGraph.push_bar` / `InferenceEngine.push_bars` accept **OHLCV + `ret` + optional `vwap`, `buy_vol`, `sell_vol`**. Use `g.add_input("close")` … `g.add_input("sell_vol")` as needed.

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

### Registry (multi-group)

```python
from factorengine.factors import FactorRegistry

reg = FactorRegistry()
reg.load_group("okx_perp")
reg.load_group("okx_perp_trades")
graphs_k = reg.build_group("okx_perp")
graphs_t = reg.build_group("okx_perp_trades")
```

### `InferenceEngine`

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

for i in range(n):
    engine.push_bar("BTC-USDT-SWAP", close[i], volume[i], open_[i], high[i], low[i], ret[i])

list(engine.get_outputs("BTC-USDT-SWAP"))
engine.get_all_outputs()
```

Batch path: `engine.push_bars({sym: rt.BarData(close, vol, o, h, l, ret, vwap, buy_v, sell_v), ...})`.

### Graph visualization

```python
from factorengine.factors.visualize import print_graph, to_dot, render_graph

g = reg.build("0001", group="okx_perp")
print_graph(g)
render_graph(g, "factor_0001.png")   # needs graphviz
```

## `Engine`: live examples

| Script | Description |
|--------|-------------|
| [`examples/engine/engine_rt_live.py`](examples/engine/engine_rt_live.py) | Kline + factors (`factor_group="okx_perp"`, default `data_source="kline"`). |
| [`examples/engine/engine_rt_trades.py`](examples/engine/engine_rt_trades.py) | Kline + trades + **both** factor groups: `factor_group=["okx_perp", "okx_perp_trades"]`, `data_source="kline_trades"`. |
| [`examples/engine/engine_rt_simulation.py`](examples/engine/engine_rt_simulation.py) | Simulation bars + factors. |

```python
from factorengine.engine import Engine

# Kline + kbar + trades factors (combined registry load)
engine = Engine(
    symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    mode="live",
    data_freq="5s",
    factor_group=["okx_perp", "okx_perp_trades"],
    num_threads=4,
    data_source="kline_trades",
)
engine.start()
factors = engine.get_factor_outputs()
engine.stop()
```

`factor_group` must be set to start the inference thread and wire `bar_queue`; workers are started based on `data_source`; ClockDriver is created when `factor_group` is set, registering the appropriate workers.

## Native C++ kernels (`fe_ops`)

**33 operators** (P0–P3), aligned with Python references in `tests/kernel/` where covered.

| Tier | Examples | Count |
|------|-----------|-------|
| **P0** | `Neg`, `Abs`, `Log`, `Add`, `Div`, … | 12 |
| **P1** | `Ma`, `TsStd`, `Ema`, `TsRank`, `Delay`, … | 12 |
| **P2** | `Corr`, `Autocorr`, `TsSkew`, … | 4 |
| **P3** | `TsMed`, `TsMad`, `TsWMA`, … | 5 |

## Tests

```bash
pytest tests/ -v

pytest tests/kernel/ -v
pytest tests/factors/ -v
pytest tests/dataflow/ -v
pytest tests/runtime_engine/ -v
pytest tests/trades/ -v

python tests/kernel/benchmark/bench_ops.py
python tests/visualization/demo_visualize.py
```

## Repository layout (abbrev.)

```
FactorEngine/
├── setup.py
├── pyproject.toml
├── examples/engine/           # engine_rt_live, engine_rt_simulation, engine_rt_trades
├── dataflow/
│   ├── cache.py
│   ├── clock_driver.py        # unified flush & aggregation center
│   ├── okx/
│   │   ├── common.py
│   │   ├── kline/             # collector → adapter → publisher → worker
│   │   └── trades/            # collector → accumulator → worker
│   └── simulation/
├── factorengine/
│   ├── engine.py
│   ├── factors/
│   │   ├── okx_perp/          # 60 kline-based factors
│   │   └── okx_perp_trades/   # 6 trades-based factors
│   └── scheduler/
├── native/                    # CMake: fe_ops + fe_runtime
└── tests/
    ├── kernel/
    ├── factors/
    ├── dataflow/
    ├── runtime_engine/
    ├── trades/
    └── visualization/
```

## Documentation

| Doc | Purpose |
|-----|---------|
| [`docs/TUTORIAL.md`](docs/TUTORIAL.md) | Repo orientation. |
| [`docs/20260503/kline_v2/kline_to_factor_pipeline.md`](docs/20260503/kline_v2/kline_to_factor_pipeline.md) | Kline → factor pipeline. |
| [`docs/20260503/kline_v2/round_batching_design.md`](docs/20260503/kline_v2/round_batching_design.md) | Round batching. |
| [`docs/20260503/trades_v1/trades_integration_design.md`](docs/20260503/trades_v1/trades_integration_design.md) | Trades integration design (Phase 1). |
| [`docs/20260503/trades_v1/trades_kline_full_pipeline.md`](docs/20260503/trades_v1/trades_kline_full_pipeline.md) | Full kline+trades pipeline walkthrough. |
| [`docs/20260503/trades_v2/clockdriver_refactor_plan.md`](docs/20260503/trades_v2/clockdriver_refactor_plan.md) | ClockDriver refactor plan (Plan B). |
| [`docs/20260503/trades_v2/trades_v2_changelog.md`](docs/20260503/trades_v2/trades_v2_changelog.md) | Trades v2 changelog / migration notes. |

## Authors

Primary maintainer and notable tooling credits:

- **lcq_dev** ([tiandiao123](https://github.com/tiandiao123))
- [**Cursor**](https://cursor.com/)
- [**GitHub Copilot**](https://github.com/features/copilot)
- **Codex** (OpenAI)

## License

MIT
