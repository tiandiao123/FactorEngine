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

import argparse
import asyncio
import logging
import numpy as np
import sys
import time

import aiohttp

sys.path.insert(0, ".")

from dataflow.events import ASK_PX_SLICE, ASK_SZ_SLICE, BID_PX_SLICE, BID_SZ_SLICE
from dataflow.okx.symbols import fetch_all_swap_symbols
from factorengine.engine import Engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_dataflow_live")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live smoke test for refactored dataflow.")
    parser.add_argument(
        "symbols",
        nargs="*",
        help="Symbol list. If omitted, defaults to BTC-USDT-SWAP unless --all is used.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Fetch all live OKX SWAP symbols and test the whole universe.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=5,
        help="How many symbols to print in detail each cycle.",
    )
    return parser.parse_args()


def resolve_symbols(args: argparse.Namespace) -> list[str]:
    if args.symbols:
        return args.symbols
    if not args.all:
        return ["BTC-USDT-SWAP"]

    logger.info("No symbols specified and --all enabled, fetching all SWAP contracts...")

    async def _fetch() -> list[str]:
        async with aiohttp.ClientSession() as session:
            return await fetch_all_swap_symbols(session)

    return asyncio.run(_fetch())


def _fmt_bar(snapshot: dict, symbol: str) -> str:
    arr = snapshot.get(symbol)
    if arr is None or len(arr) == 0:
        return "bars: missing"

    latest = arr[-1]
    recent = arr[-min(3, len(arr)) :]
    lines = [
        f"bars rows={arr.shape[0]} latest_close={latest[4]:.4f} latest_vol={latest[5]:.4f}"
        f" vol_ccy={latest[6]:.4f} vol_ccy_quote={latest[7]:.2f}"
    ]
    start_idx = arr.shape[0] - recent.shape[0] + 1
    for idx, row in enumerate(recent, start=start_idx):
        lines.append(
            f"  bar[{idx}] ts={int(row[0])} O={row[1]:.4f} H={row[2]:.4f} "
            f"L={row[3]:.4f} C={row[4]:.4f} V={row[5]:.4f} "
            f"Vccy={row[6]:.4f} Vquote={row[7]:.2f}"
        )
    return "\n".join(lines)


def _fmt_trade(snapshot: dict, symbol: str) -> str:
    arr = snapshot.get(symbol)
    if arr is None or len(arr) == 0:
        return "trades: missing"

    latest = arr[-1]
    recent = arr[-min(5, len(arr)) :]
    side = "buy" if latest[2] > 0 else "sell"
    lines = [
        f"trades rows={arr.shape[0]} latest_px={latest[0]:.4f} latest_sz={latest[1]:.4f} latest_side={side}"
    ]
    start_idx = arr.shape[0] - recent.shape[0] + 1
    for idx, row in enumerate(recent, start=start_idx):
        row_side = "buy" if row[2] > 0 else "sell"
        lines.append(
            f"  trade[{idx}] px={row[0]:.4f} sz={row[1]:.4f} side={row_side}"
        )
    return "\n".join(lines)


def _fmt_book(snapshot: dict, symbol: str) -> str:
    arr = snapshot.get(symbol)
    if arr is None or len(arr) == 0:
        return "books: missing"

    latest = arr[-1]
    bid_px = latest[BID_PX_SLICE.start]
    bid_sz = latest[BID_SZ_SLICE.start]
    ask_px = latest[ASK_PX_SLICE.start]
    ask_sz = latest[ASK_SZ_SLICE.start]
    spread = ask_px - bid_px
    bid_levels = latest[BID_PX_SLICE]
    bid_sizes = latest[BID_SZ_SLICE]
    ask_levels = latest[ASK_PX_SLICE]
    ask_sizes = latest[ASK_SZ_SLICE]

    def _fmt_levels(level_px: np.ndarray, level_sz: np.ndarray) -> str:
        parts = []
        for idx, (px, sz) in enumerate(zip(level_px, level_sz), start=1):
            if np.isnan(px) or np.isnan(sz):
                continue
            parts.append(f"L{idx}:{px:.4f}@{sz:.4f}")
        return " | ".join(parts) if parts else "missing"

    lines = [
        f"books rows={arr.shape[0]} best_bid={bid_px:.4f}@{bid_sz:.4f} "
        f"best_ask={ask_px:.4f}@{ask_sz:.4f} spread={spread:.10f}",
        f"  bids { _fmt_levels(bid_levels, bid_sizes) }",
        f"  asks { _fmt_levels(ask_levels, ask_sizes) }",
    ]
    return "\n".join(lines)


def main():
    args = parse_args()
    symbols = resolve_symbols(args)
    sample_limit = max(1, args.sample_limit)

    engine = Engine(
        symbols=symbols,
        data_freq="5s",
        pull_interval="10s",
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
        "Engine started for dataflow live test. symbols=%d pull_interval=%ss sample_limit=%d",
        len(symbols),
        engine.pull_interval_seconds,
        sample_limit,
    )
    try:
        while True:
            time.sleep(engine.pull_interval_seconds)
            # bar_snapshot:
            # {
            #   "BTC-USDT-SWAP": ndarray(shape=(N, 6)),
            #   ...
            # }
            # 每行一根 bar，列顺序:
            # [ts, open, high, low, close, vol, vol_ccy, vol_ccy_quote]
            bar_snapshot = engine.get_data(symbols)

            # trade_snapshot:
            # {
            #   "BTC-USDT-SWAP": ndarray(shape=(N, 3)),
            #   ...
            # }
            # 每行一笔成交，列顺序:
            # [px, sz, side]
            # side: buy=1, sell=-1
            trade_snapshot = engine.get_trade_data(symbols)

            # book_snapshot:
            # {
            #   "BTC-USDT-SWAP": ndarray(shape=(N, 20)),
            #   ...
            # }
            # 每行一次 books5 更新，列顺序:
            # [
            #   bid_px1, bid_px2, bid_px3, bid_px4, bid_px5,
            #   bid_sz1, bid_sz2, bid_sz3, bid_sz4, bid_sz5,
            #   ask_px1, ask_px2, ask_px3, ask_px4, ask_px5,
            #   ask_sz1, ask_sz2, ask_sz3, ask_sz4, ask_sz5,
            # ]
            book_snapshot = engine.get_book_data(symbols)

            lines = [
                f"bar_count={engine.bar_count} trade_count={engine.trade_count} book_count={engine.book_count}"
            ]
            lines.append(
                f"snapshot: bars={len(bar_snapshot)} trades={len(trade_snapshot)} books={len(book_snapshot)}"
            )
            detail_symbols = symbols[:sample_limit]
            for symbol in detail_symbols:
                lines.append(
                    f"{symbol}:\n"
                    f"{_fmt_bar(bar_snapshot, symbol)}\n"
                    f"{_fmt_trade(trade_snapshot, symbol)}\n"
                    f"{_fmt_book(book_snapshot, symbol)}"
                )
            if len(symbols) > sample_limit:
                lines.append(f"... {len(symbols) - sample_limit} more symbols omitted")
            logger.info("\n%s", "\n".join(lines))
    except KeyboardInterrupt:
        logger.info("Stopping...")
        engine.stop()
        logger.info("Done.")


if __name__ == "__main__":
    main()
