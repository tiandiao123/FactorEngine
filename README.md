# FactorEngine

> **This project is under active development. APIs and layout may change.**

Real-time factor stack for **quantitative trading**: Python **market data collection** (OKX + Binance Futures), **33 C++ operator kernels** (`fe_ops`, pybind11), and a **DAG streaming runtime** (`fe_runtime`) that evaluates compiled factor graphs bar-by-bar with minimal Python overhead in the hot path.

**123 factors** across two groups: **70 kline-based** (`okx_perp`) and **53 trades-based** (`okx_perp_trades`).

For a reading-order tour, see [`docs/TUTORIAL.md`](docs/TUTORIAL.md). For the dataflow architecture, see [`dataflow/README.md`](dataflow/README.md). Recent operational design notes live under [`docs/20260523`](docs/20260523), [`docs/20260524`](docs/20260524), [`docs/20260525`](docs/20260525), [`docs/20260526`](docs/20260526), and [`docs/20260528`](docs/20260528).

## Architecture (v4 — ClockDriver unified flush, multi-exchange)

Top-level [`factorengine.engine.Engine`](factorengine/engine.py) coordinates:

1. **Dataflow threads** — independent workers per exchange, each collecting one data stream:
   - **Kline** — `KlineWorker` (OKX) / `BinanceKlineWorker`: exchange WS → adapter → `KlinePublisher` (shared round buffer).
   - **Trades** — `TradesWorker` (OKX) / `BinanceTradesWorker`: exchange WS → per-symbol `TradesAccumulator` (shared incremental aggregator).
2. **ClockDriver thread** — the **single aggregation center**. Fires on wall-clock aligned boundaries (`:00`, `:05`, `:10`, …), calls `flush()` on every registered worker, merges results via `_aggregate()`, optionally writes aggregate audit records, and pushes one `(merged_bars, merged_trades)` payload into `bar_queue`. Workers never touch `bar_queue`.
3. **Runtime thread** — `bar_queue.get()` → builds `fe_runtime.BarData` (OHLCV + `ret` + optional trades fields) → `InferenceEngine.push_bars` → **signal deque**.
4. **Main thread** — `get_factor_outputs()` (O(1)), optional `get_data()` (bar cache snapshot), `kline_stats` / `trades_stats` / `clock_stats` / `audit_stats`.

Dynamic symbol lists are supported for live engines through a staged universe update flow: `Engine.update_symbols(...)` validates and stages the target universe, `ClockDriver` commits the diff after a flush boundary, exchange workers rebuild subscriptions, and the C++ runtime adds/removes per-symbol runners in the same `bar_queue` order as data updates. Static `Engine(symbols=...)` usage remains supported.

Trades engines also support `trade_time_mode="exchange_time"` for trades-only live flows. In this mode, trades are assigned to buckets by exchange `trade_ts_ms`, finalized after `allowed_lateness_ms`, and emitted once by `ClockDriver`. This reduces receive-time boundary skew while adding a small, configurable delay.

```
┌─ Exchange Workers (OKX or Binance) ─────────────────────────────────┐
│  kline thread:  Exchange WS → Collector → Adapter → Publisher       │
│  trades thread: Exchange WS → Collector → per-symbol Accumulators   │
└─────────────────┬──────────────────────────┬────────────────────────┘
                  │                          │
                  v                          v
         ┌──────────────────────────────────────────┐
         │  ClockDriver (freq=5s, wall-clock aligned)│
         │    kline_data  = kline_worker.flush()     │
         │    trades_data = trades_worker.flush()    │
         │    → _aggregate(kline priority per sym)   │
         │    → optional audit JSONL write           │
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

### Supported exchanges

| Exchange | `exchange=` | Kline | Trades | Min kline freq | Notes |
|----------|-------------|-------|--------|----------------|-------|
| **OKX** | `"okx"` (default) | `candle1s`+ | `trades` | 1 second | Full kline+trades support |
| **Binance Futures** | `"binance"` | `kline_1m`+ | `aggTrade` | 1 minute | No sub-minute klines; use `data_source="trades"` for 1s |

### `data_source` (live)

| Value | Workers started | Who flushes |
|-------|----------------|-------------|
| **`kline`** | KlineWorker | ClockDriver (kline only) |
| **`trades`** | TradesWorker | ClockDriver (trades only); OHLCV derived from trades; supports `trade_time_mode="receive_time"` or `"exchange_time"` |
| **`kline_trades`** | Both | ClockDriver (both); kline bars take priority per symbol, trades snapshots merged |

### Core pieces

| Area | Role |
|------|------|
| **`dataflow/`** | Market data collection & aggregation. See [`dataflow/README.md`](dataflow/README.md) for full architecture. |
| **`dataflow/base_collector.py`** | `BaseCollector` ABC — shared WS reconnect logic for all collectors. |
| **`dataflow/kline/publisher.py`** | Exchange-agnostic `KlinePublisher` — round batching, timeout, forward-fill. |
| **`dataflow/trades/accumulator.py`** | Exchange-agnostic trades aggregation: receive-time `TradesAccumulator` plus exchange-time bucketed accumulation. |
| **`dataflow/clock_driver.py`** | Unified wall-clock flush & aggregation center; `register(name, worker)` + `_aggregate()` → `bar_queue`. |
| **`dataflow/audit/`** | Optional aggregate audit logging: ClockDriver aggregate bars → async JSONL writer with flush and rotation controls. |
| **`dataflow/okx/`** | OKX exchange: kline `collector` → `adapter` → `publisher` → `worker`; trades `collector` → `accumulator` → `worker`. |
| **`dataflow/binance/`** | Binance Futures: same pattern as OKX, with `/market` WS route and `aggTrade` stream. |
| **`dataflow/simulation/`** | Synthetic bars + optional `bar_queue` for tests/demos. |
| **`dataflow/cache.py`** | Rolling `BarCache` for `get_data()` snapshots. |
| **`factorengine.Engine`** | `exchange`, `data_source`, `factor_group` as **str or list**; **`update_symbols`**, **`get_factor_outputs`**, **`get_ready_factor_outputs`**, **`get_symbol_status`**, **`get_data`**, **stats** accessors. |
| **`fe_ops` / `fe_runtime`** | Batch kernels + DAG streaming; **`BarData`** includes **`vwap`, `buy_vol`, `sell_vol`**; `add_input` supports those names. |
| **`factorengine/factors/`** | `FactorRegistry`, `@register_factor` — **`okx_perp`** (70 kline factors), **`okx_perp_trades`** (53 trades factors). |

Design references: [`docs/20260504/binance_dataflow_design.md`](docs/20260504/binance_dataflow_design.md), [`docs/20260505/trades_aggfeatures_plan.md`](docs/20260505/trades_aggfeatures_plan.md), [`docs/20260523/dynamic_symbol_list_design.md`](docs/20260523/dynamic_symbol_list_design.md), [`docs/20260525/dynamic_symbol_end_to_end_flow.md`](docs/20260525/dynamic_symbol_end_to_end_flow.md), [`docs/20260526/exchange_time_trades_aggregation_allowed_lateness.md`](docs/20260526/exchange_time_trades_aggregation_allowed_lateness.md), [`docs/20260528/exchange_time_engine_tutorial.md`](docs/20260528/exchange_time_engine_tutorial.md).

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
reg.load_group("okx_perp")          # 70 kline-based factors
reg.load_group("okx_perp_trades")   # 53 trades-based factors
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
| [`engine_rt_live.py`](examples/engine/staticSymbols/engine_rt_live.py) | OKX kline + factors (`data_source="kline"`). |
| [`engine_rt_trades.py`](examples/engine/staticSymbols/engine_rt_trades.py) | OKX trades-only with 20 symbols and 53 trades factors. |
| [`engine_rt_binance_trades.py`](examples/engine/staticSymbols/engine_rt_binance_trades.py) | **Binance Futures** trades-only, picks top 200 USDT-M perpetuals at startup and enables aggregate audit logging. |
| [`engine_rt_binance_dynamic_symbols.py`](examples/engine/dynanmicSymbols/engine_rt_binance_dynamic_symbols.py) | Binance trades-only dynamic symbol universe demo with readiness logging and aggregate audit output. |
| [`engine_rt_okx_dynamic_symbols.py`](examples/engine/dynanmicSymbols/engine_rt_okx_dynamic_symbols.py) | OKX trades-only dynamic symbol universe demo mirroring the Binance configuration. |
| [`engine_rt_binance_exchange_time.py`](examples/engine/timeBuckets/engine_rt_binance_exchange_time.py) | Binance trades-only exchange-time demo with `allowed_lateness_ms` and tee logging for debugging. |
| [`engine_rt_binance_dynamic_exchange_time.py`](examples/engine/timeBuckets/engine_rt_binance_dynamic_exchange_time.py) | Binance dynamic-symbol exchange-time demo; rotates symbol lists while preserving bucket finalization semantics. |
| [`engine_rt_okx_dynamic_exchange_time.py`](examples/engine/timeBuckets/engine_rt_okx_dynamic_exchange_time.py) | OKX dynamic-symbol exchange-time demo with OKX swap symbols and aggregate audit output. |
| [`engine_rt_simulation.py`](examples/engine/staticSymbols/engine_rt_simulation.py) | Simulation bars + factors. |

### OKX kline example

```python
from factorengine.engine import Engine

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

### Binance Futures trades example

```python
from factorengine.engine import Engine

engine = Engine(
    symbols=["BTC-USDT", "ETH-USDT", "SOL-USDT"],
    mode="live",
    exchange="binance",         # Binance Futures (USDT-M)
    data_freq="1s",
    factor_group=["okx_perp_trades"],
    num_threads=4,
    data_source="trades",       # aggTrade → OHLCV + vwap/buy/sell
)
engine.start()
factors = engine.get_factor_outputs()
engine.stop()
```

### Binance Futures kline example (≥ 1m)

```python
engine = Engine(
    symbols=["BTC-USDT", "ETH-USDT"],
    mode="live",
    exchange="binance",
    data_freq="60s",            # minimum 1m for Binance klines
    factor_group=["okx_perp"],
    data_source="kline",
)
```

`factor_group` must be set to start the inference thread and wire `bar_queue`; workers are started based on `data_source`; ClockDriver is created when `factor_group` is set, registering the appropriate workers.

### Aggregate audit logging

`Engine` accepts an optional `AuditConfig`. When enabled, ClockDriver writes the aggregate bars it already flushed to JSONL files. This is aggregate-only audit: it does not persist raw websocket messages or per-trade events.

```python
from dataflow.audit import AuditConfig
from factorengine.engine import Engine

engine = Engine(
    symbols=["BTC-USDT", "ETH-USDT", "SOL-USDT"],
    mode="live",
    exchange="binance",
    data_freq="5s",
    factor_group=["okx_perp_trades"],
    data_source="trades",
    audit_config=AuditConfig(
        enabled=True,
        root_dir="data/audit/binance_trades_demo",
        flush_interval_seconds=1.0,
        flush_max_records=10_000,
        flush_max_bytes=4 * 1024 * 1024,
        rotate_interval_seconds=3600,
        rotate_max_bytes=512 * 1024 * 1024,
    ),
)
```

Audit files look like:

```text
data/audit/binance_trades_demo/aggregate_bars_YYYYMMDD_HHMMSS_part000001.jsonl
```

Use `engine.audit_stats` to inspect queue, flush, drop, and current-file state. See [`docs/20260524/aggregate_audit_implementation.md`](docs/20260524/aggregate_audit_implementation.md) for the full call flow.

### Audit verifiers

The Binance verifier compares FactorEngine aggregate audit bars with either Binance official 1m klines or Binance `aggTrades` rebuilt to 1m. For exchange-time trades audits, `aggTrades` is the strictest reference because it uses the same trades-only data source:

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

Use [`examples/verifier/run.sh`](examples/verifier/run.sh) as a ready-to-edit Binance command.

The OKX verifier compares aggregate audit bars with OKX official 1m candles. Pick a fully covered UTC minute window that avoids the demo startup minute and the most recent still-settling minute:

```bash
bash examples/verifier/run_okx.sh
```

`trade_count` is displayed for context but is not part of the match decision. See [`examples/verifier/README.md`](examples/verifier/README.md) for timestamp semantics and tolerance rules.

### Dynamic symbol lists

Static symbol lists still work:

```python
engine = Engine(symbols=["BTC-USDT", "ETH-USDT"], ...)
```

For low-frequency universe rebalances, create the engine with `enable_dynamic_symbols=True` and stage updates with `update_symbols(...)`:

```python
engine.update_symbols(
    ["BTC-USDT", "ETH-USDT", "SOL-USDT"],
    version="manual-rotate:20260526T120000Z:0",
    metadata={"reason": "scheduled_rebalance"},
)
```

The update is applied after a `ClockDriver` flush boundary. Added symbols get fresh C++ `SymbolRunner` state; removed symbols are physically removed from the runtime. Use `get_symbol_status()` to inspect per-symbol warmup state and `get_ready_factor_outputs()` when downstream consumers should ignore symbols whose factors are not fully warmed up yet.

See [`examples/engine/dynanmicSymbols/`](examples/engine/dynanmicSymbols/), [`docs/20260523/dynamic_symbol_list_design.md`](docs/20260523/dynamic_symbol_list_design.md), and [`docs/20260525/dynamic_symbol_end_to_end_flow.md`](docs/20260525/dynamic_symbol_end_to_end_flow.md) for the full flow.

### Exchange-time trades buckets

Trades-only live engines can opt into exchange-time bucketing:

```python
engine = Engine(
    symbols=["BTC-USDT", "ETH-USDT", "SOL-USDT"],
    mode="live",
    exchange="binance",
    data_freq="1s",
    factor_group=["okx_perp_trades"],
    data_source="trades",
    trade_time_mode="exchange_time",
    allowed_lateness_ms=200,
)
```

In receive-time mode, a trade updates the current local accumulator when it arrives. In exchange-time mode, each trade is assigned by exchange `trade_ts_ms`, not local receive time:

```text
symbol -> EventTimeTradesAccumulator
          -> buckets[bucket_start_ms] -> TradeBucket
```

`ClockDriver` advances a watermark (`now_ms - allowed_lateness_ms`) and flushes mature buckets whose `bucket_end_ms <= watermark_ms`. A finalized bucket is emitted at most once; trades that arrive after the lateness window are counted in `engine.trades_stats["dropped_late_trade_count"]`.

Current constraints:

```text
data_source="trades" + trade_time_mode="exchange_time" is supported.
data_source="kline_trades" + trade_time_mode="exchange_time" is intentionally blocked in phase 1.
```

Examples:

```bash
python examples/engine/timeBuckets/engine_rt_binance_exchange_time.py
python examples/engine/timeBuckets/engine_rt_binance_dynamic_exchange_time.py
python examples/engine/timeBuckets/engine_rt_okx_dynamic_exchange_time.py
```

See [`docs/20260528/exchange_time_engine_tutorial.md`](docs/20260528/exchange_time_engine_tutorial.md) for the operational walkthrough and [`docs/20260526/exchange_time_trades_aggregation_allowed_lateness.md`](docs/20260526/exchange_time_trades_aggregation_allowed_lateness.md) for the original design note.

## Native C++ kernels (`fe_ops`)

**33 operators** (P0–P3), aligned with Python references in `tests/kernels/` where covered. Full documentation: [`docs/OPERATORS.md`](docs/OPERATORS.md).

| Tier | Examples | Count |
|------|-----------|-------|
| **P0** | `Neg`, `Abs`, `Log`, `Add`, `Div`, … | 12 |
| **P1** | `Ma`, `TsStd`, `Ema`, `TsRank`, `Delay`, … | 12 |
| **P2** | `Corr`, `Autocorr`, `TsSkew`, … | 4 |
| **P3** | `TsMed`, `TsMad`, `TsWMA`, … | 5 |

## Tests

```bash
pytest tests/ -v                          # full suite (1080+ tests)

pytest tests/kernels/ -v                  # C++ operator alignment
pytest tests/factors/ -v                  # registry + inference
pytest tests/dataflow/ -v                 # kline/trades pipeline
pytest tests/runtime_engine/ -v           # engine integration
pytest tests/trades/ -v                   # accumulator + trades factors
pytest tests/audit/ -v                    # aggregate audit writer + ClockDriver integration

python tests/kernels/benchmark/bench_ops.py
python tests/visualization/demo_visualize.py
```

## Repository layout (abbrev.)

```
FactorEngine/
├── setup.py
├── pyproject.toml
├── examples/
│   ├── engine/                   # live/simulation Engine demos
│   │   ├── timeBuckets/          # exchange-time bucket demos
│   │   ├── staticSymbols/        # static universe demos
│   │   └── dynanmicSymbols/      # dynamic universe demos for Binance and OKX
│   └── verifier/                 # audit verifiers for Binance and OKX
├── dataflow/
│   ├── base_collector.py         # BaseCollector ABC (shared WS reconnect)
│   ├── audit/                    # aggregate audit schema/config/writer
│   ├── cache.py
│   ├── clock_driver.py           # unified flush & aggregation center
│   ├── kline/publisher.py        # exchange-agnostic round batching
│   ├── trades/accumulator.py     # exchange-agnostic trades aggregation
│   ├── okx/
│   │   ├── common.py
│   │   ├── kline/                # collector → adapter → publisher → worker
│   │   └── trades/               # collector → accumulator → worker
│   ├── binance/
│   │   ├── common.py             # /market WS route, symbol mapping
│   │   ├── kline/                # collector → adapter → worker (≥1m)
│   │   └── trades/               # collector (aggTrade) → worker
│   └── simulation/
├── factorengine/
│   ├── engine.py                 # Engine (exchange, data_source, factor_group)
│   ├── factors/
│   │   ├── registry.py           # FactorRegistry, @register_factor
│   │   ├── okx_perp/             # 70 kline-based factors
│   │   └── okx_perp_trades/      # 53 trades-based factors
├── native/                       # CMake: fe_ops + fe_runtime
└── tests/
    ├── audit/
    ├── kernels/
    ├── factors/
    ├── dataflow/
    ├── runtime_engine/
    ├── trades/
    └── visualization/
```

## Documentation

| Doc | Purpose |
|-----|---------|
| [`docs/TUTORIAL.md`](docs/TUTORIAL.md) | Repo orientation and reading-order guide. |
| [`dataflow/README.md`](dataflow/README.md) | Dataflow architecture, exchange notes, extension guide. |
| [`docs/OPERATORS.md`](docs/OPERATORS.md) | Full C++ operator list with math formulas and test locations. |
| [`docs/20260504/binance_dataflow_design.md`](docs/20260504/binance_dataflow_design.md) | Binance integration design and implementation notes. |
| [`docs/20260505/trades_aggfeatures_plan.md`](docs/20260505/trades_aggfeatures_plan.md) | Trades aggregate factor plan. |
| [`docs/20260523/dynamic_symbol_list_design.md`](docs/20260523/dynamic_symbol_list_design.md) | Design for runtime symbol universe updates. |
| [`docs/20260523/stream_audit_replay_refactor_plan.md`](docs/20260523/stream_audit_replay_refactor_plan.md) | Stream audit/replay refactor design. |
| [`docs/20260524/aggregate_audit_implementation.md`](docs/20260524/aggregate_audit_implementation.md) | Implemented aggregate audit logging call flow and schema. |
| [`docs/20260524/bar_schema_alignment_design.md`](docs/20260524/bar_schema_alignment_design.md) | Bar schema alignment risks and cleanup plan. |
| [`docs/20260525/cpp_runtime_dynamic_symbol_support_development.md`](docs/20260525/cpp_runtime_dynamic_symbol_support_development.md) | C++ runtime add/remove/reset symbol plan and implementation details. |
| [`docs/20260525/dynamic_symbol_end_to_end_flow.md`](docs/20260525/dynamic_symbol_end_to_end_flow.md) | End-to-end dynamic symbol update flow across dataflow and C++ runtime. |
| [`docs/20260526/exchange_time_trades_aggregation_allowed_lateness.md`](docs/20260526/exchange_time_trades_aggregation_allowed_lateness.md) | Exchange-time trades bucketing design with `allowed_lateness`. |
| [`docs/20260528/exchange_time_engine_tutorial.md`](docs/20260528/exchange_time_engine_tutorial.md) | Exchange-time engine operational tutorial and examples. |
| [`examples/verifier/README.md`](examples/verifier/README.md) | Audit verifier usage and tolerance semantics. |

## Authors

Primary maintainer and notable tooling credits:

- **lcq_dev** ([tiandiao123](https://github.com/tiandiao123))
- [**Cursor**](https://cursor.com/)
- [**GitHub Copilot**](https://github.com/features/copilot)
- **Codex** (OpenAI)

## License

MIT
