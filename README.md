# FactorEngine

> **⚠️ This project is still in active development. APIs and architecture may change.**

Real-time factor computation engine for OKX perpetual contracts.

## Architecture

Two threads, one shared cache — fully decoupled.

```
┌──────────────────────┐      data_cache       ┌──────────────────────┐
│  Thread 1: Dataflow  │  dict[symbol, ndarray] │  Main Thread (you)   │
│  OKX WS → aggregate  │ ────────────────────▶  │  engine.get_data()   │
│  → write cache       │    threading.Lock      │  → factor compute    │
└──────────────────────┘                        └──────────────────────┘
```

- **Dataflow thread**: subscribes to OKX WebSocket `candle1s`, aggregates into N-second bars, writes to shared `data_cache`
- **Main thread**: calls `engine.get_data()` to pull a snapshot (deep copy), runs factor computation without blocking data collection

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
    bar_window_length=1000,    # max bars to keep per symbol
    trade_window_length=10000, # max trade events to keep per symbol
    book_history_length=1000,  # max book updates to keep per symbol
)
engine.start()

import time
while True:
    time.sleep(engine.pull_interval_seconds)
    snapshot = engine.get_data()  # {symbol: ndarray(N, 6)}
    for sym, data in snapshot.items():
        # columns: [ts, open, high, low, close, vol]
        print(f"{sym}: {data.shape}, close={data[-1, 4]:.2f}")
```

### Full market (~304 contracts)

```python
import asyncio, aiohttp
from dataflow.collector import fetch_all_swap_symbols

async def fetch():
    async with aiohttp.ClientSession() as s:
        return await fetch_all_swap_symbols(s)

symbols = asyncio.run(fetch())
engine = Engine(symbols=symbols, data_freq="5s", pull_interval="10s")
engine.start()
```

### Run test script

```bash
cd FactorEngine

# Specific symbols
python -m tests.test_live BTC-USDT-SWAP ETH-USDT-SWAP

# All SWAP contracts
python -m tests.test_live
```

## API

| Method | Description |
|--------|-------------|
| `Engine(symbols, data_freq, pull_interval, bar_window_length, trade_window_length, book_history_length, ...)` | Create engine |
| `engine.start()` | Start dataflow thread |
| `engine.stop()` | Graceful shutdown |
| `engine.get_data()` | Snapshot of all symbols |
| `engine.get_data(["BTC-USDT-SWAP"])` | Snapshot of specific symbols |
| `engine.bar_count` | Total bars aggregated |

### Frequency format

`1s`, `5s`, `10s`, `30s`, `1m`, `1min`, `5min`, `1h`, `1hr`

## Performance (304 symbols stress test)

| Metric | Value |
|--------|-------|
| `get_data()` all 304 symbols | **< 0.5ms** |
| `get_data()` filtered 2 symbols | **< 0.01ms** |
| Memory (steady state, window=1000) | **~20 MB** |
| Data completeness at 10s | **304/304 symbols** |
| Bar throughput | **~60 bars/sec** |

## Project Structure

```
FactorEngine/
  dataflow/
    collector.py      # OKX WebSocket candle1s subscription
    dataflow.py       # Dataflow thread + bar aggregation + cache
  factorengine/
    engine.py         # Engine entry point + get_data()
  tests/
    test_live.py      # Live test script
  docs/               # Design docs, test report, tutorial
```

## Requirements

- Python >= 3.11
- aiohttp
- numpy

## License

MIT
