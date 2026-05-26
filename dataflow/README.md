# Dataflow — Market Data Collection & Aggregation

This package is the **market-data side** of FactorEngine. It handles WebSocket connections to exchanges, transforms raw messages into structured numeric arrays, and feeds them to the C++ factor inference engine through a unified clock-driven pipeline.

It also owns the dataflow-side pieces of **dynamic symbol universe rebalance**:

- Fetching candidate universes from exchange REST APIs or local text files
- Filtering and staging a new full target symbol set
- Committing symbol diffs at a ClockDriver tick boundary
- Asking workers to rebuild subscriptions without blocking the clock thread
- Emitting audit metadata so replay/verifier tools can explain missing bars

## Architecture overview

```text
┌─ Exchange A (OKX) ──────────────────┐   ┌─ Exchange B (Binance) ────────────────┐
│                                      │   │                                       │
│  okx-kline thread                    │   │  binance-kline thread (≥1m only)      │
│    OKX WS (candle1s / candle1m)      │   │    Binance Futures WS (kline_1m+)     │
│    → OKXKlineCollector               │   │    → BinanceKlineCollector             │
│    → KlineAdapter (filter+aggregate) │   │    → BinanceKlineAdapter (filter x=1) │
│    → KlinePublisher (round batch)    │   │    → KlinePublisher (round batch)     │
│                                      │   │                                       │
│  okx-trades thread                   │   │  binance-trades thread                │
│    OKX WS (trades)                   │   │    Binance Futures WS (aggTrade)      │
│    → OKXTradesCollector              │   │    → BinanceTradesCollector            │
│    → per-symbol TradesAccumulator    │   │    → per-symbol TradesAccumulator     │
└──────────────┬───────────────────────┘   └──────────────┬────────────────────────┘
               │                                          │
               └──────────── ClockDriver ─────────────────┘
                    (wall-clock aligned flush, e.g. every 1s / 5s / 60s)
                    kline_data  = kline_worker.flush()
                    trades_data = trades_worker.flush()
                    → _aggregate()   (kline bars take priority per symbol)
                    → bar_queue.put( (merged_bars, merged_trades) )
                    → optional post-flush symbol diff commit
                                     │
                                     v
                 ┌───────────────────────────────────────────────────┐
                 │  factor-infer thread (Engine._runtime_loop)       │
                 │    bar_queue → BarData(OHLCV, ret, vwap,          │
                 │                        buy_vol, sell_vol)         │
                 │    → C++ InferenceEngine.push_bars()              │
                 │    → signal_deque (latest factor outputs)         │
                 └───────────────────────────────────────────────────┘

Dynamic universe side channel:

Exchange REST / local txt
  → SymbolSource → SymbolFilter[] → SymbolProvider
  → Engine symbol-refresh thread or manual update_symbols()
  → SymbolManager stages SymbolDiff
  → ClockDriver commits after current bucket is flushed
  → worker/runtime/audit receive ordered notifications
```

### Design principles

1. **Workers never touch `bar_queue`** — only ClockDriver pushes to it.
2. **Time alignment** — all workers are flushed at the same wall-clock boundary. The factor DAG always sees a consistent cross-symbol, cross-source snapshot.
3. **Exchange-agnostic core** — `KlinePublisher`, `TradesAccumulator`, `ClockDriver`, `SymbolManager`, and `BarCache` know little or nothing about OKX/Binance protocol details. Exchange specifics stay in `okx/`, `binance/`, and source implementations.
4. **Post-flush universe commits** — dynamic symbol changes are staged first, then committed only after the current bucket has been flushed. This prevents the just-finished bar from being lost during a rebalance.
5. **Owner-thread heavy work** — ClockDriver only performs lightweight commit/notification. WebSocket rebuilds happen in worker threads; factor runtime changes are consumed in the runtime queue order.
6. **Extensibility** — adding a new data source (orderbook, funding rate) or exchange is `clock_driver.register("name", worker)` with local changes to the new worker/source.

## Package layout

```
dataflow/
├── __init__.py
├── base_collector.py        # BaseCollector ABC — shared WS reconnect logic
├── clock_driver.py          # ClockDriver — wall-clock aligned flush & aggregation
├── cache.py                 # BarCache — thread-safe rolling numpy cache
│
├── audit/                   # ── async audit logging ──
│   ├── config.py            #   AuditConfig
│   ├── schema.py            #   AggregateBarEvent, SymbolDiffEvent
│   └── writer.py            #   non-blocking JSONL writer
│
├── kline/                   # ── exchange-agnostic kline components ──
│   └── publisher.py         #   KlinePublisher — round batching, timeout, forward-fill
│
├── trades/                  # ── exchange-agnostic trades components ──
│   └── accumulator.py       #   TradesAccumulator — per-symbol O(1) streaming aggregation
│
├── symbols/                 # ── dynamic symbol universe support ──
│   ├── models.py            #   SymbolRecord, SymbolUniverse, SymbolDiff
│   ├── manager.py           #   SymbolManager — validate, diff, stage, commit
│   ├── sources.py           #   REST/local/static SymbolSource and SymbolProvider
│   └── filters.py           #   allow/deny/quote/status/top-volume filters
│
├── okx/                     # ── OKX exchange adapters ──
│   ├── common.py            #   endpoints, channel mappings, symbol conversion
│   ├── symbols.py           #   instrument list helpers
│   ├── kline/
│   │   ├── collector.py     #   OKXKlineCollector (WS → on_candle callback)
│   │   ├── adapter.py       #   KlineAdapter (filter, time-align, aggregate)
│   │   ├── publisher.py     #   re-export shim → dataflow.kline.publisher
│   │   └── worker.py        #   KlineWorker (daemon thread, exposes flush())
│   └── trades/
│       ├── collector.py     #   OKXTradesCollector (WS → on_trade callback)
│       ├── accumulator.py   #   re-export shim → dataflow.trades.accumulator
│       └── worker.py        #   TradesWorker (daemon thread, exposes flush())
│
├── binance/                 # ── Binance Futures (USDT-M) adapters ──
│   ├── common.py            #   endpoints (/market route), symbol conversion
│   ├── symbols.py           #   instrument list helpers
│   ├── kline/
│   │   ├── collector.py     #   BinanceKlineCollector (WS → on_candle callback)
│   │   ├── adapter.py       #   BinanceKlineAdapter (filter confirmed k.x=true)
│   │   └── worker.py        #   BinanceKlineWorker (daemon thread, exposes flush())
│   └── trades/
│       ├── collector.py     #   BinanceTradesCollector (WS aggTrade → on_trade)
│       └── worker.py        #   BinanceTradesWorker (daemon thread, exposes flush())
│
└── simulation/              # ── synthetic data for offline dev ──
    ├── generator.py         #   deterministic bar generator
    ├── worker.py            #   SimBarWorker
    ├── manager.py           #   SimDataflowManager (drop-in for live)
    └── symbols.py           #   default symbols and base prices
```

## Core components

### BaseCollector (`base_collector.py`)

Abstract base class for all WebSocket collectors. Provides:

- Symbol chunking (≤ N symbols per WS connection)
- Auto-reconnect loop with exponential backoff
- Subclass contract: implement `_connect_and_listen(session, symbols)`

All four collectors (`OKXKlineCollector`, `OKXTradesCollector`, `BinanceKlineCollector`, `BinanceTradesCollector`) inherit from `BaseCollector`.

### ClockDriver (`clock_driver.py`)

The central coordination component. It is the **only** component that pushes to `bar_queue`.

```python
clock_driver = ClockDriver(freq_seconds=5.0, bar_queue=q)
clock_driver.register("kline", kline_worker)
clock_driver.register("trades", trades_worker)
clock_driver.start()
```

On each wall-clock aligned tick (`:00`, `:05`, `:10`, …):
1. Calls `flush()` on every registered worker
2. Runs `_aggregate()` — kline bars take priority per symbol; trades-derived OHLCV is fallback
3. Writes aggregate audit records when audit is enabled
4. Pushes a legacy `(merged_bars, merged_trades)` tuple or a structured bars payload to `bar_queue`
5. Calls the optional `after_flush_commit(bucket_ts_ms)` hook

The post-flush hook is used by dynamic symbols. The order matters:

```text
flush old active universe
  → write audit for old bucket
  → enqueue bars payload for old bucket
  → commit pending SymbolDiff
  → enqueue symbol_diff control message
```

This means the runtime thread sees:

```text
bars(symbol_version=v1)
symbol_diff(v2)
bars(symbol_version=v2)
```

It never receives a diff before the current bucket has been flushed.

When dynamic symbols are enabled, `ClockDriver` can emit structured payloads:

```python
{
    "type": "bars",
    "bars": {"BTC-USDT": np.ndarray(...), ...},
    "trades": {"BTC-USDT": {"vwap": 102.5, ...}},
    "bucket_ts_ms": 1710000000000,
    "symbol_version": "binance:1710000000000",
    "active_symbols": {"BTC-USDT", "ETH-USDT"},
}
```

Legacy tuple payloads remain supported for non-dynamic paths and older tests.

### KlinePublisher (`kline/publisher.py`)

Exchange-agnostic component that batches `BarPoint` arrays by round:

- Collects all symbols' bars for a given time window
- Timeout + forward-fill for missing symbols
- Per-symbol deduplication via `last_pushed_ts`
- Timestamp gap detection and warning
- Exposes `flush_for_clock()` for ClockDriver

Used by both `KlineWorker` (OKX) and `BinanceKlineWorker`.

### TradesAccumulator (`trades/accumulator.py`)

Exchange-agnostic, per-symbol O(1) streaming aggregator:

- Tracks `vwap`, `buy_vol`, `sell_vol`, `trade_count`
- Derives OHLCV from trades (for `trades`-only mode)
- Thread-safe via `threading.Lock`
- Forward-fills OHLC with last close on empty buckets (prevents NaN propagation)
- `snapshot_and_reset()` — atomic read + reset for the next bucket

Used by both `TradesWorker` (OKX) and `BinanceTradesWorker`.

### Dynamic symbols (`symbols/`)

Dynamic symbols are modeled as **full universe snapshots**, not incremental subscribe commands.

The main data types are:

| Type | Responsibility |
|------|----------------|
| `SymbolRecord` | Optional per-instrument metadata: exchange, quote asset, contract type, status, quote volume, raw exchange payload |
| `SymbolUniverse` | Full target symbol set with `version`, `exchange`, `symbols`, `metadata`, and optional TTL |
| `SymbolDiff` | Difference between committed active universe and a target universe: `added`, `removed`, `unchanged`, `target` |
| `SymbolManager` | Thread-safe validator and staging area for pending diffs |

The source/filter/provider split is:

```text
SymbolSource
  fetches raw candidate universe
  examples: BinanceExchangeSymbolSource, OkxExchangeSymbolSource,
            LocalTextSymbolSource, StaticSymbolSource

SymbolFilter
  transforms or narrows a universe
  examples: QuoteAssetFilter, ContractTypeFilter, TradableStatusFilter,
            TopQuoteVolumeFilter, AllowListFilter, DenyListFilter

SymbolProvider
  runs source.fetch(), then filter.apply() in order,
  and returns one final SymbolUniverse
```

Example: Binance official tradable universe intersected with a local allowlist and capped by volume:

```python
from dataflow.symbols import BinanceExchangeSymbolSource, SymbolProvider
from dataflow.symbols.filters import (
    AllowListFilter,
    ContractTypeFilter,
    QuoteAssetFilter,
    TopQuoteVolumeFilter,
    TradableStatusFilter,
)

provider = SymbolProvider(
    BinanceExchangeSymbolSource(),
    filters=[
        QuoteAssetFilter("USDT"),
        ContractTypeFilter("PERPETUAL"),
        TradableStatusFilter("TRADING"),
        AllowListFilter.from_txt("config/symbol_pools/binance_usdt_perp.txt"),
        TopQuoteVolumeFilter(top_n=200),
    ],
    refresh_interval_seconds=1800,
)
```

`SymbolProvider` does not mutate Engine, workers, or runtime. It only returns a new `SymbolUniverse`. Mutation happens later through `SymbolManager` and the ClockDriver commit hook.

### BarCache (`cache.py`)

Thread-safe rolling cache backed by numpy arrays. Used for `engine.get_data()` debug snapshots. Not on the main compute path.

## Exchange-specific notes

### OKX (`okx/`)

| Stream | WS endpoint | Channel | Min frequency |
|--------|-------------|---------|---------------|
| Kline | `wss://ws.okx.com:8443/ws/v5/business` | `candle1s`, `candle1m`, … | 1 second |
| Trades | `wss://ws.okx.com:8443/ws/v5/public` | `trades` | real-time |

- OKX supports 1-second klines (`candle1s`) natively
- `KlineAdapter` handles time-aligned aggregation (e.g. 5×1s → 5s bars)
- Symbol format: `BTC-USDT-SWAP` (OKX perpetual), `BTC-USDT` (spot)

### Binance Futures (`binance/`)

| Stream | WS endpoint | Channel | Min frequency |
|--------|-------------|---------|---------------|
| Kline | `wss://fstream.binance.com/market/ws` | `kline_1m`, `kline_5m`, … | 1 minute |
| Trades | `wss://fstream.binance.com/market/ws` | `aggTrade` | real-time |

- **No 1-second klines** — trades-only mode (`data_source="trades"`) is the primary path for sub-minute frequencies
- Production WS requires the `/market` route prefix (2026 routing upgrade)
- Testnet (`stream.binancefuture.com`) does **not** need `/market`
- Symbol format: `BTC-USDT` (unified) ↔ `btcusdt` (Binance WS lowercase)
- Dynamic subscription via JSON `SUBSCRIBE` message (not URL path)
- `BinanceKlineAdapter` filters for confirmed candles only (`k.x == true`)

## Data flow by `data_source` mode

| `data_source` | Workers started | What ClockDriver aggregates |
|---------------|-----------------|------------------------------|
| `kline` | KlineWorker only | Kline bars only |
| `trades` | TradesWorker only | Trades-derived OHLCV + trades snapshots (vwap, buy_vol, sell_vol) |
| `kline_trades` | Both | Kline bars (priority) + trades snapshots; trades-derived OHLCV as fallback |

### Trades-only mode detail

When `data_source="trades"`, there is no kline WS connection. The flow is:

```text
aggTrade messages → TradesAccumulator.update(px, sz, side)
                    ├── O(1) running: vwap, buy_vol, sell_vol, trade_count
                    └── OHLCV derived from trades
                                  │
           ClockDriver.flush()  ──┘
                    │
                    v
           round_bars[sym] = [ts, open, high, low, close, vol, NaN, NaN]
           round_trades[sym] = {vwap, buy_vol, sell_vol, trade_count, ...}
                    │
                    v
           bar_queue → Engine._runtime_loop → BarData → C++ push_bars
```

### kline_trades mode detail

Both streams run in parallel. ClockDriver merges them:

```text
Kline round:   {BTC: [ts,O,H,L,C,V,...], ETH: [ts,O,H,L,C,V,...]}
Trades round:  {BTC: {vwap,buy_vol,sell_vol,...}, ETH: {vwap,...}}

_aggregate():
  merged_bars  = kline_round  (kline bars take priority)
  merged_trades = trades snapshots
  for sym in trades_bars:
      if sym not in merged_bars:  # fallback: use trades-derived OHLCV
          merged_bars[sym] = trades_bar

Result: every symbol has OHLCV (from kline or trades), plus trades metadata
```

## Worker `flush()` return types

| Worker | `flush()` return type |
|--------|----------------------|
| `KlineWorker` / `BinanceKlineWorker` | `dict[str, np.ndarray] \| None` |
| `TradesWorker` / `BinanceTradesWorker` | `tuple[dict[str, np.ndarray], dict[str, dict]]` |

The first element (`round_bars`) maps symbol → `np.ndarray` of shape `(8,)`:
`[timestamp_ms, open, high, low, close, volume, vol_ccy, vol_ccy_quote]`.

The second element (`round_trades`) maps symbol → dict with keys:
`vwap`, `buy_vol`, `sell_vol`, `trade_count`, `open`, `high`, `low`, `close`, `volume`.

## Dynamic symbol universe rebalance

Dynamic symbols are intended for low-frequency rebalance, such as refreshing a Binance USDT perpetual top-N universe or an OKX swap universe every 30 minutes. The goal is **process continuity**, not zero-gap high-frequency subscription churn.

Supported first-version path:

| Area | Status |
|------|--------|
| `exchange="binance"` + `data_source="trades"` | Supported |
| `exchange="okx"` + `data_source="trades"` | Supported |
| Manual `engine.update_symbols([...])` | Supported |
| Background `symbol_provider.fetch()` refresh thread | Supported |
| Binance / OKX trades worker subscription rebuild | Supported |
| Structured runtime payloads with `symbol_version` and `active_symbols` | Supported |
| Audit `symbol_diff` events and verifier timeline | Supported |
| Physical deletion of C++ symbol runners | Supported for factor runtime paths |
| Kline / `kline_trades` dynamic universe | Not first-version scope |

### How a rebalance starts

There are two entry points:

```python
# Manual or strategy-driven update.
engine.update_symbols(["BTC-USDT", "ETH-USDT"], version="manual:20260524")
```

or:

```python
# Low-frequency provider-driven update.
engine = Engine(
    symbols=["BTC-USDT"],
    exchange="binance",  # or "okx" with symbols like "BTC-USDT-SWAP"
    data_source="trades",
    enable_dynamic_symbols=True,
    symbol_provider=provider,
    symbol_refresh_interval_seconds=1800,
)
engine.start()
```

The refresh thread starts with Engine and stops with Engine. It does one background `refresh_symbols_once()` after startup, then waits for the configured interval. Each refresh:

1. Calls `symbol_provider.fetch()`
2. Gets a full `SymbolUniverse`
3. Calls `SymbolManager.stage_universe(universe)`
4. Leaves the resulting `SymbolDiff` pending

It does **not** touch workers, `bar_queue`, or C++ runtime directly.

### Staging vs committing

`SymbolManager` owns the committed active universe and at most one pending diff.

```text
current active = {BTC, ETH}
new universe   = {BTC, SOL}

stage_universe():
  added     = {SOL}
  removed   = {ETH}
  unchanged = {BTC}
  target    = {BTC, SOL}
  pending   = SymbolDiff(...)
```

Validation happens during staging:

- Exchange must match the Engine exchange.
- Version must be newer than the current committed version.
- Empty universe is rejected unless explicitly allowed.
- Optional change-size limits can reject unexpectedly large diffs.

Commit happens only from `Engine.commit_pending_symbol_diff()`, which is wired into `ClockDriver(after_flush_commit=...)`.

### Commit order and thread ownership

The important invariant is:

```text
current bucket flush must finish before the symbol universe changes
```

Runtime sequence:

```text
symbol-refresh thread / user thread
  → stage SymbolDiff in SymbolManager

ClockDriver thread, next tick:
  → worker.flush() under old active universe
  → aggregate bars/trades
  → audit aggregate bars with old symbol_version
  → bar_queue.put({"type": "bars", "symbol_version": old_version, ...})
  → Engine.commit_pending_symbol_diff(bucket_ts_ms)
      → SymbolManager.commit_pending()
      → Engine updates active_symbols / symbol_version
      → TradesWorker/BinanceTradesWorker.apply_symbol_diff(diff)
      → AuditWriter.write_symbol_diff(...)
      → bar_queue.put({"type": "symbol_diff", "diff": diff, ...})

Trades worker thread:
  → collector stops
  → worker loop rebuilds the exchange collector with target symbols

factor-infer runtime thread:
  → consumes old bars payload
  → consumes symbol_diff control message
  → consumes following bars payload under new symbol_version
```

The ClockDriver thread never waits for a WebSocket reconnect. `TradesWorker.apply_symbol_diff()` / `BinanceTradesWorker.apply_symbol_diff()` update in-memory symbol state under a lock, request a collector rebuild, and return quickly.

### Trades worker dynamic behavior

The OKX `TradesWorker` and `BinanceTradesWorker` keep three symbol-related states:

| Field | Purpose |
|-------|---------|
| `symbols` | Public snapshot, kept sorted after commits |
| `_active_symbols` | Symbols included in `flush()` output |
| `_target_symbols` | Symbols used when the collector is rebuilt |

`flush()` takes a locked snapshot of active accumulators and then iterates outside the lock. Late trades for removed symbols are ignored because `_on_trade()` checks the accumulator map under the same lock.

When a diff is applied:

1. New symbols get fresh `TradesAccumulator` instances.
2. Removed symbols' accumulators are dropped.
3. `_active_symbols`, `_target_symbols`, and `symbols` are updated.
4. `_rebuild_requested` is set.
5. The current collector is stopped so the worker thread can create a new collector with the target universe.

This accepts a short reconnect window during low-frequency rebalance, but the Engine process and ClockDriver stay alive.

### Runtime payload filtering

Structured bars payloads include `active_symbols`. `Engine._runtime_loop()` uses that set to:

- Ignore bars for inactive symbols if any stale data appears
- Filter `get_all_outputs()` so `signal_deque[-1]["factors"]` only exposes active symbols
- Preserve `symbol_version` and `active_symbols` in each signal snapshot

The runtime queue also carries `{"type": "symbol_diff"}` control messages. They are ordered with bars because both go through the same `bar_queue`.

### Audit and verifier impact

Aggregate audit rows include:

- `event_type="aggregate_bar"`
- `symbol_version`
- `active_symbols_count`

Committed universe changes write separate events:

```python
{
    "event_type": "symbol_diff",
    "commit_ts_ms": 1710000000000,
    "effective_from_ts_ms": 1710000000000,
    "exchange": "binance",
    "data_source": "trades",
    "symbol_version": "binance:1710000000000",
    "added": ["SOL-USDT"],
    "removed": ["ETH-USDT"],
    "unchanged_count": 199,
    "target_count": 200,
}
```

The audit verifier reads `symbol_diff` events and builds a best-effort active-symbol timeline. If a symbol has been removed, later missing aggregate bars are counted as `inactive_missing`, not as data loss.

### Failure behavior

| Failure | Behavior |
|---------|----------|
| Provider REST error | Refresh thread logs the exception and keeps the current universe |
| Invalid universe | `SymbolManager` rejects it and increments rejected update stats |
| No symbol changes | Version may advance, but no pending diff is committed |
| `bar_queue` full for bars | ClockDriver drops the bar payload and increments drop count |
| `bar_queue` full for symbol diff | Engine logs warning; active dataflow state has still been committed |
| Worker reconnect gap | Accepted for first-version low-frequency rebalance |

## Adding a new exchange

1. Create `dataflow/<exchange>/common.py` — endpoints, symbol conversion
2. Create `dataflow/<exchange>/trades/collector.py` — inherit `BaseCollector`, implement `_connect_and_listen`
3. Create `dataflow/<exchange>/trades/worker.py` — mirror `BinanceTradesWorker`, reuse `TradesAccumulator`
4. (Optional) Create kline collector/adapter/worker if the exchange supports kline WS
5. Add exchange-specific `SymbolSource` support if dynamic universe refresh is needed
6. Add the exchange branch to `Engine._init_live()` in `factorengine/engine.py`
7. Add the exchange name to `Engine._VALID_EXCHANGES`

The shared components (`KlinePublisher`, `TradesAccumulator`, `BaseCollector`, `ClockDriver`) require zero changes.

## Adding a new data source (e.g. orderbook)

1. Create a worker class with a `flush()` method
2. Register it: `clock_driver.register("ob", ob_worker)`
3. Extend `ClockDriver._aggregate()` to handle the new data type
4. Extend `BarData` fields in C++ if the factor DAG needs new inputs

## Testing

```bash
# All dataflow tests
pytest tests/dataflow/ -v

# Exchange-specific
pytest tests/dataflow/okx/ -v
pytest tests/dataflow/binance/ -v

# Trades-specific
pytest tests/trades/ -v

# Dynamic symbol universe
pytest tests/symbols/ -v
pytest tests/runtime_engine/test_engine_dynamic_symbols.py -v

# Audit / verifier metadata for dynamic universes
pytest tests/audit/ -v
```

Test coverage includes: collector message parsing, adapter filtering, publisher round batching, worker flush logic, ClockDriver aggregation, Engine integration with queue injection, dynamic symbol manager/provider/filter behavior, Binance/OKX trades worker rebuild requests, and audit/verifier symbol timeline handling. Live WebSocket access is not required for these unit/integration tests.
