# Dataflow — Market Data Collection & Aggregation

This package is the **market-data side** of FactorEngine. It handles WebSocket connections to exchanges, transforms raw messages into structured numeric arrays, and feeds them to the C++ factor inference engine through a unified clock-driven pipeline.

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
                                     │
                                     v
                 ┌───────────────────────────────────────────────────┐
                 │  factor-infer thread (Engine._runtime_loop)       │
                 │    bar_queue → BarData(OHLCV, ret, vwap,          │
                 │                        buy_vol, sell_vol)         │
                 │    → C++ InferenceEngine.push_bars()              │
                 │    → signal_deque (latest factor outputs)         │
                 └───────────────────────────────────────────────────┘
```

### Design principles

1. **Workers never touch `bar_queue`** — only ClockDriver pushes to it.
2. **Time alignment** — all workers are flushed at the same wall-clock boundary. The factor DAG always sees a consistent cross-symbol, cross-source snapshot.
3. **Exchange-agnostic core** — `KlinePublisher`, `TradesAccumulator`, `ClockDriver`, and `BarCache` know nothing about OKX or Binance. Exchange specifics are isolated to `okx/` and `binance/` sub-packages.
4. **Extensibility** — adding a new data source (orderbook, funding rate) or exchange is `clock_driver.register("name", worker)` with no changes to existing code.

## Package layout

```
dataflow/
├── __init__.py
├── base_collector.py        # BaseCollector ABC — shared WS reconnect logic
├── clock_driver.py          # ClockDriver — wall-clock aligned flush & aggregation
├── cache.py                 # BarCache — thread-safe rolling numpy cache
│
├── kline/                   # ── exchange-agnostic kline components ──
│   └── publisher.py         #   KlinePublisher — round batching, timeout, forward-fill
│
├── trades/                  # ── exchange-agnostic trades components ──
│   └── accumulator.py       #   TradesAccumulator — per-symbol O(1) streaming aggregation
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
3. Pushes `(merged_bars, merged_trades)` to `bar_queue`

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

## Adding a new exchange

1. Create `dataflow/<exchange>/common.py` — endpoints, symbol conversion
2. Create `dataflow/<exchange>/trades/collector.py` — inherit `BaseCollector`, implement `_connect_and_listen`
3. Create `dataflow/<exchange>/trades/worker.py` — mirror `BinanceTradesWorker`, reuse `TradesAccumulator`
4. (Optional) Create kline collector/adapter/worker if the exchange supports kline WS
5. Add the exchange branch to `Engine._init_live()` in `factorengine/engine.py`
6. Add the exchange name to `Engine._VALID_EXCHANGES`

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
```

Test coverage includes: collector message parsing, adapter filtering, publisher round batching, worker flush logic, ClockDriver aggregation, Engine integration with queue injection (no live WS needed).
