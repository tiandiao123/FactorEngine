# FactorEngine

> **⚠️ This project is still in active development. APIs and architecture may change.**

Real-time factor computation engine for OKX perpetual contracts.

## Architecture

Current prototype has two layers:

1. `dataflow`
   Continuous collection of `bars`, `trades`, and `books`
2. `scheduler`
   Timer-driven factor evaluation on top of current cache snapshots

```
┌────────────────────────────┐
│ Dataflow                   │
│ bars / trades / books      │
│ OKX WS -> array caches     │
└──────────────┬─────────────┘
               │
               v
┌────────────────────────────┐
│ Engine                     │
│ get_data / get_trade_data  │
│ get_book_data              │
└──────────────┬─────────────┘
               │
               v
┌────────────────────────────┐
│ Scheduler Prototype        │
│ fixed interval tick        │
│ -> slice cache             │
│ -> compute factors         │
└────────────────────────────┘
```

- `bars` are exposed as `dict[symbol, ndarray(N, 6)]`
- `trades` are exposed as `dict[symbol, ndarray(N, 3)]`
- `books` are exposed as `dict[symbol, ndarray(N, 20)]`
- Scheduler prototype is intentionally minimal and exists to validate tick scheduling and cache slicing before moving to C++

## Quick Start

```bash
pip install -e .
```

```python
from factorengine.engine import Engine

engine = Engine(
    symbols=["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
    data_freq="5s",        # aggregate 1s candles into 5s bars
    pull_interval="10s",   # how often you pull data
    bar_window_length=1000,     # max bars to keep per symbol
    trade_window_length=10000,  # max trade rows to keep per symbol
    book_history_length=1000,   # max book rows to keep per symbol
    enable_trades=True,
    trade_channels=("trades-all",),
    enable_books=True,
    book_channels=("books5",),
)
engine.start()

import time
while True:
    time.sleep(engine.pull_interval_seconds)
    bar_snapshot = engine.get_data()
    trade_snapshot = engine.get_trade_data()
    book_snapshot = engine.get_book_data()

    for sym, bars in bar_snapshot.items():
        # bars columns: [ts, open, high, low, close, vol]
        print(sym, "bars", bars.shape)
    for sym, trades in trade_snapshot.items():
        # trades columns: [px, sz, side]
        print(sym, "trades", trades.shape)
    for sym, books in book_snapshot.items():
        # books columns:
        # [bid_px1..5, bid_sz1..5, ask_px1..5, ask_sz1..5]
        print(sym, "books", books.shape)
```

### Full market (~304 contracts)

```python
import asyncio, aiohttp
from dataflow.collector import fetch_all_swap_symbols

async def fetch():
    async with aiohttp.ClientSession() as s:
        return await fetch_all_swap_symbols(s)

symbols = asyncio.run(fetch())
engine = Engine(
    symbols=symbols,
    data_freq="5s",
    pull_interval="10s",
    enable_trades=True,
    enable_books=True,
)
engine.start()
```

### Run test script

```bash
cd FactorEngine

# Dataflow live smoke test
python -m tests.test_dataflow_live BTC-USDT-SWAP ETH-USDT-SWAP

# Dataflow live test, default symbol: BTC-USDT-SWAP
python -m tests.test_dataflow_live

# Dataflow live test, full market
python -m tests.test_dataflow_live --all --sample-limit 5

# Scheduler prototype live test
python -m tests.test_scheduler_live

# Scheduler prototype, full market
python -m tests.test_scheduler_live --all --sample-limit 5

# Raw OKX stream debug test
python -m tests.test_micro_ws --inst-id BTC-USDT-SWAP --duration 30
```

## API

| Method | Description |
|--------|-------------|
| `Engine(symbols, data_freq, pull_interval, bar_window_length, trade_window_length, book_history_length, ...)` | Create engine |
| `engine.start()` | Start dataflow |
| `engine.stop()` | Graceful shutdown |
| `engine.get_data()` | Bar snapshot of all symbols |
| `engine.get_trade_data()` | Trade snapshot of all symbols |
| `engine.get_book_data()` | Book snapshot of all symbols |
| `engine.get_data(["BTC-USDT-SWAP"])` | Bar snapshot of specific symbols |
| `engine.bar_count` | Total bars aggregated |
| `engine.trade_count` | Total trades captured |
| `engine.book_count` | Total books captured |

### Frequency format

`1s`, `5s`, `10s`, `30s`, `1m`, `1min`, `5min`, `1h`, `1hr`

## Data Schemas

### Bars

```text
dict[str, ndarray(N, 6)]
columns = [ts, open, high, low, close, vol]
```

### Trades

```text
dict[str, ndarray(N, 3)]
columns = [px, sz, side]
side: buy=1, sell=-1
```

### Books

```text
dict[str, ndarray(N, 20)]
columns = [
    bid_px1..5,
    bid_sz1..5,
    ask_px1..5,
    ask_sz1..5,
]
```

## Scheduler Prototype

Current prototype lives under:

- `factorengine/scheduler/`

It includes:

- `FactorSpec`
- `FactorRuntime`
- `FactorSnapshot`
- `Scheduler`

This prototype currently validates:

- fixed interval evaluation ticks
- cache slicing
- factor computation over `bars/trades/books`
- factor snapshot output

It is intentionally still Python-first and not the final C++ runtime.

## Project Structure

```
FactorEngine/
  dataflow/
    okx/              # OKX collectors
    bars/             # bar aggregation worker
    trades/           # trade worker
    books/            # book worker
    cache.py          # array caches
    manager.py        # dataflow manager
  factorengine/
    engine.py         # Engine entry point + dataflow access
    scheduler/        # scheduler prototype
  tests/
    test_dataflow_live.py  # Live dataflow smoke test
    test_scheduler_live.py # Live scheduler smoke test
    test_micro_ws.py       # Raw OKX stream debug test
  docs/               # Design docs and tutorials
```

## Requirements

- Python >= 3.11
- aiohttp
- numpy

## License

MIT
