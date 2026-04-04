"""Live test — start Engine, pull data every 10s, print snapshot.

Usage:
    cd FactorEngine
    python -m tests.test_live
    python -m tests.test_live BTC-USDT-SWAP ETH-USDT-SWAP
"""

import logging
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
    try:
        while True:
            time.sleep(engine.pull_interval_seconds)
            snapshot = engine.get_data()
            n_symbols = len(snapshot)
            total_bars = engine.bar_count

            if n_symbols == 0:
                logger.info("snapshot: empty (waiting for data...) | total bars: %d", total_bars)
                continue

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
