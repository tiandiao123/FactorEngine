"""Manual live test for the Python scheduler prototype."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys
import time

import aiohttp

sys.path.insert(0, ".")

from dataflow.okx.symbols import fetch_all_swap_symbols
from factorengine.engine import Engine
from factorengine.scheduler import (
    FactorRuntime,
    FactorSpec,
    Scheduler,
    compute_bar_momentum,
    compute_book_l1_imbalance,
    compute_trade_imbalance,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_scheduler_live")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live test for the scheduler prototype.")
    parser.add_argument(
        "symbols",
        nargs="*",
        help="Symbol list. If omitted, defaults to BTC-USDT-SWAP unless --all is used.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Fetch all live OKX SWAP symbols.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="Scheduler interval in seconds.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=5,
        help="How many symbols to print on each factor snapshot.",
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


def main():
    args = parse_args()
    symbols = resolve_symbols(args)
    sample_limit = max(1, args.sample_limit)

    engine = Engine(
        symbols=symbols,
        data_freq="5s",
        pull_interval=f"{int(args.interval)}s" if args.interval.is_integer() else "10s",
        bar_window_length=1000,
        trade_window_length=10_000,
        book_history_length=1_000,
        enable_trades=True,
        trade_channels=("trades-all",),
        enable_books=True,
        book_channels=("books5",),
    )
    engine.start()

    factor_specs = [
        FactorSpec("bar_momentum_20", "bars", 20, compute_bar_momentum),
        FactorSpec("trade_imbalance_500", "trades", 500, compute_trade_imbalance),
        FactorSpec("book_l1_imbalance_50", "books", 50, compute_book_l1_imbalance),
    ]
    runtime = FactorRuntime(engine=engine, symbols=symbols, factor_specs=factor_specs)

    def _on_tick(tick_id: int, ts_eval_ms: int):
        snapshot = runtime.evaluate(tick_id, ts_eval_ms)
        ts_eval = dt.datetime.fromtimestamp(ts_eval_ms / 1000).isoformat(timespec="seconds")
        lines = [
            f"tick_id={snapshot.tick_id} ts_eval={ts_eval} duration_ms={snapshot.duration_ms:.3f}",
            f"symbols={len(symbols)} sample_limit={sample_limit}",
        ]
        for symbol in symbols[:sample_limit]:
            values = snapshot.values.get(symbol, {})
            rendered = " ".join(f"{name}={value:.6f}" for name, value in values.items())
            lines.append(f"{symbol}: {rendered}")
        if len(symbols) > sample_limit:
            lines.append(f"... {len(symbols) - sample_limit} more symbols omitted")
        logger.info("\n%s", "\n".join(lines))

    scheduler = Scheduler(interval_seconds=args.interval, on_tick=_on_tick)
    scheduler.start()
    logger.info(
        "Scheduler live test started. symbols=%d interval=%.3fs sample_limit=%d",
        len(symbols),
        args.interval,
        sample_limit,
    )

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping...")
        scheduler.stop()
        engine.stop()
        logger.info("Done.")


if __name__ == "__main__":
    main()
