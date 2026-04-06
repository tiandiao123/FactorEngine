"""Manual live smoke test for the refactored dataflow stack.

This script exercises the formal Engine/DataflowManager path instead of the
standalone microstructure debug script. It is intended for manual verification
that bars, trades and books can be collected together.

Usage:
    cd FactorEngine
    python -m tests.test_dataflow_live
    python -m tests.test_dataflow_live BTC-USDT-SWAP ETH-USDT-SWAP
"""

from __future__ import annotations

import logging
import sys
import time

sys.path.insert(0, ".")

from dataflow.events import ASK_PX_SLICE, ASK_SZ_SLICE, BID_PX_SLICE, BID_SZ_SLICE
from factorengine.engine import Engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_dataflow_live")


def _fmt_bar(snapshot: dict, symbol: str) -> str:
    arr = snapshot.get(symbol)
    if arr is None or len(arr) == 0:
        return "bar=missing"
    latest = arr[-1]
    return f"bar_close={latest[4]:.4f} bar_vol={latest[5]:.4f} rows={arr.shape[0]}"


def _fmt_trade(snapshot: dict, symbol: str) -> str:
    arr = snapshot.get(symbol)
    if arr is None or len(arr) == 0:
        return "trade=missing"
    latest = arr[-1]
    side = "buy" if latest[2] > 0 else "sell"
    return (
        f"trade_px={latest[0]:.4f} trade_sz={latest[1]:.4f} "
        f"trade_side={side}"
    )


def _fmt_book(snapshot: dict, symbol: str) -> str:
    arr = snapshot.get(symbol)
    if arr is None or len(arr) == 0:
        return "book=missing"
    latest = arr[-1]
    bid_px = latest[BID_PX_SLICE.start]
    bid_sz = latest[BID_SZ_SLICE.start]
    ask_px = latest[ASK_PX_SLICE.start]
    ask_sz = latest[ASK_SZ_SLICE.start]
    spread = ask_px - bid_px
    return (
        f"bid={bid_px:.4f}@{bid_sz:.4f} "
        f"ask={ask_px:.4f}@{ask_sz:.4f} "
        f"spread={spread:.10f}"
    )


def main():
    symbols = sys.argv[1:] or ["BTC-USDT-SWAP"]

    engine = Engine(
        symbols=symbols,
        data_freq="5s",
        pull_interval="5s",
        bar_window_length=1000,
        trade_window_length=10_000,
        book_history_length=1_000,
        enable_trades=True,
        trade_channels=("trades-all",),
        enable_books=True,
        book_channels=("books5",),
    )
    engine.start()

    logger.info(
        "Engine started for dataflow live test. symbols=%s pull_interval=%ss",
        symbols,
        engine.pull_interval_seconds,
    )
    try:
        while True:
            time.sleep(engine.pull_interval_seconds)
            bar_snapshot = engine.get_data(symbols)
            trade_snapshot = engine.get_trade_data(symbols)
            book_snapshot = engine.get_book_data(symbols)

            lines = [
                f"bar_count={engine.bar_count} trade_count={engine.trade_count} book_count={engine.book_count}"
            ]
            for symbol in symbols:
                lines.append(
                    f"{symbol}: "
                    f"{_fmt_bar(bar_snapshot, symbol)} | "
                    f"{_fmt_trade(trade_snapshot, symbol)} | "
                    f"{_fmt_book(book_snapshot, symbol)}"
                )
            logger.info("\n%s", "\n".join(lines))
    except KeyboardInterrupt:
        logger.info("Stopping...")
        engine.stop()
        logger.info("Done.")


if __name__ == "__main__":
    main()
