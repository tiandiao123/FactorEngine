"""Live test — start Engine, pull data every 10s, print snapshot.

Usage:
    cd FactorEngine
    python -m tests.test_live
    python -m tests.test_live BTC-USDT-SWAP ETH-USDT-SWAP
"""

import csv
import datetime as dt
import logging
from pathlib import Path
import sys
import time

import aiohttp
import asyncio

sys.path.insert(0, ".")
from factorengine.engine import Engine
from dataflow.collector import fetch_all_swap_symbols

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_live")
SNAPSHOT_PATH = Path.cwd() / "factorengine_snapshot_latest.csv"
SNAPSHOT_DIR = Path.cwd() / "factorengine_snapshots"


def _fmt_bar_time(ts_ms: float) -> str:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).isoformat()


def _snapshot_file_path(snapshot_time: dt.datetime) -> Path:
    stamp = snapshot_time.strftime("%Y%m%d_%H%M%S_%f")
    return SNAPSHOT_DIR / f"snapshot_{stamp}.csv"


def _write_snapshot_csv(snapshot: dict, total_bars: int, path: Path, saved_at: dt.datetime):
    """Persist the latest bar for each symbol to a CSV file."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)

    with tmp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "saved_at_local",
            "symbol",
            "rows",
            "total_bars",
            "bar_ts_ms",
            "bar_time_utc",
            "open",
            "high",
            "low",
            "close",
            "vol",
        ])
        for sym, arr in snapshot.items():
            latest = arr[-1]
            writer.writerow([
                saved_at.isoformat(),
                sym,
                arr.shape[0],
                total_bars,
                int(latest[0]),
                _fmt_bar_time(float(latest[0])),
                float(latest[1]),
                float(latest[2]),
                float(latest[3]),
                float(latest[4]),
                float(latest[5]),
            ])

    tmp_path.replace(path)


def _save_snapshot_files(snapshot: dict, total_bars: int):
    """Write both the rolling latest file and a timestamped historical snapshot."""
    snapshot_time = dt.datetime.now().astimezone()
    _write_snapshot_csv(snapshot, total_bars, SNAPSHOT_PATH, snapshot_time)
    _write_snapshot_csv(snapshot, total_bars, _snapshot_file_path(snapshot_time), snapshot_time)


def main():
    # Parse symbols from command line, or fetch all
    if len(sys.argv) > 1:
        symbols = sys.argv[1:]
    else:
        logger.info("No symbols specified, fetching all SWAP contracts...")
        loop = asyncio.new_event_loop()
        async def _fetch():
            async with aiohttp.ClientSession() as session:
                return await fetch_all_swap_symbols(session)
        symbols = loop.run_until_complete(_fetch())
        loop.close()

    engine = Engine(
        symbols=symbols,
        data_freq="5s",
        pull_interval="10s",
        window_length=1000,
    )
    engine.start()

    logger.info("Engine started. Pulling data every %s... (Ctrl+C to stop)", engine.pull_interval)
    logger.info("Latest snapshot CSV will be written to %s", SNAPSHOT_PATH)
    logger.info("Timestamped snapshot CSVs will be written to %s", SNAPSHOT_DIR)
    try:
        while True:
            time.sleep(engine.pull_interval_seconds)
            # snapshot format:
            # {
            #   "BTC-USDT-SWAP": ndarray(shape=(N, 6)),
            #   "ETH-USDT-SWAP": ndarray(shape=(M, 6)),
            #   ...
            # }
            # Each ndarray is the full rolling window currently kept in memory,
            # not just the latest 5s bar.
            # Columns: [ts, open, high, low, close, vol]
            snapshot = engine.get_data()
            n_symbols = len(snapshot)
            total_bars = engine.bar_count

            if n_symbols == 0:
                logger.info("snapshot: empty (waiting for data...) | total bars: %d", total_bars)
                continue

            _save_snapshot_files(snapshot, total_bars)

            # Print first 5 symbols as sample
            lines = []
            for i, (sym, arr) in enumerate(snapshot.items()):
                if i >= 5:
                    break
                latest = arr[-1]  # [ts, open, high, low, close, vol]
                lines.append(
                    f"  {sym}: rows={arr.shape[0]} C={latest[4]:.4f} V={latest[5]:.2f}"
                )
            sample = "\n".join(lines)
            logger.info(
                "snapshot: %d symbols | total bars: %d\n%s",
                n_symbols, total_bars, sample,
            )
    except KeyboardInterrupt:
        logger.info("Stopping...")
        engine.stop()
        logger.info("Done.")


if __name__ == "__main__":
    main()
