"""Smoke test for simulation mode — no network required.

Usage:
    cd FactorEngine
    python -m tests.test_simulation
"""

from __future__ import annotations

import logging
import sys
import time

sys.path.insert(0, ".")

from dataflow.simulation.symbols import DEFAULT_SYMBOLS
from factorengine.engine import Engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_simulation")


def main():
    symbols = DEFAULT_SYMBOLS
    sim_bar_interval = 1.0   # generate a bar every 1 second
    pull_interval = "5s"     # fetch snapshot every 5 seconds
    num_cycles = 3           # how many pull cycles to run

    engine = Engine(
        symbols=symbols,
        data_freq="5s",
        pull_interval=pull_interval,
        bar_window_length=100,
        mode="simulation",
        sim_bar_interval=sim_bar_interval,
        sim_seed=42,
    )
    engine.start()
    logger.info(
        "Simulation engine started: %d symbols, bar_interval=%.1fs, pull_interval=%s",
        len(symbols), sim_bar_interval, pull_interval,
    )

    try:
        for cycle in range(1, num_cycles + 1):
            time.sleep(engine.pull_interval_seconds)
            snapshot = engine.get_data()
            logger.info(
                "=== Cycle %d/%d  bar_count=%d  symbols_with_data=%d ===",
                cycle, num_cycles, engine.bar_count, len(snapshot),
            )
            for symbol in symbols:
                arr = snapshot.get(symbol)
                if arr is None or len(arr) == 0:
                    logger.info("  %s: no data yet", symbol)
                    continue
                latest = arr[-1]
                logger.info(
                    "  %s: rows=%d  latest=[ts=%d O=%.4f H=%.4f L=%.4f C=%.4f V=%.2f Vccy=%.2f Vquote=%.2f]",
                    symbol, arr.shape[0],
                    int(latest[0]), latest[1], latest[2], latest[3],
                    latest[4], latest[5], latest[6], latest[7],
                )
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        engine.stop()
        logger.info("Done.")


if __name__ == "__main__":
    main()
