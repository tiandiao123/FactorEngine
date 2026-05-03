"""Benchmark: measure OKX BTC-USDT-SWAP trades throughput.

Connects to OKX public WS, subscribes to trades for one or more symbols,
and prints per-second trade counts for a configurable duration.

Usage:
    python -m tests.trades.bench_trades_throughput              # BTC only, 30s
    python -m tests.trades.bench_trades_throughput --duration 60
    python -m tests.trades.bench_trades_throughput --symbols BTC-USDT-SWAP ETH-USDT-SWAP SOL-USDT-SWAP
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections import defaultdict

import aiohttp

WS_URL = "wss://ws.okx.com:8443/ws/v5/public"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


async def run(symbols: list[str], duration: int):
    counts: dict[str, list[int]] = defaultdict(list)
    current_second: dict[str, int] = {}
    current_count: dict[str, int] = defaultdict(int)
    total_msgs = 0
    total_trades = 0
    json_parse_ns = 0
    t_start = time.monotonic()

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(WS_URL, heartbeat=20) as ws:
            args = [{"channel": "trades", "instId": s} for s in symbols]
            await ws.send_json({"op": "subscribe", "args": args})
            log.info("Subscribed to trades for %s — collecting for %ds ...", symbols, duration)

            async for msg in ws:
                if time.monotonic() - t_start > duration:
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue

                t0 = time.perf_counter_ns()
                payload = json.loads(msg.data)
                json_parse_ns += time.perf_counter_ns() - t0
                total_msgs += 1

                records = payload.get("data")
                if not records:
                    continue

                symbol = payload.get("arg", {}).get("instId", "?")
                now_sec = int(time.time())

                for _ in records:
                    total_trades += 1
                    if current_second.get(symbol) != now_sec:
                        if symbol in current_second:
                            counts[symbol].append(current_count[symbol])
                        current_second[symbol] = now_sec
                        current_count[symbol] = 0
                    current_count[symbol] += 1

                if total_msgs % 200 == 0:
                    elapsed = time.monotonic() - t_start
                    log.info(
                        "[%.0fs] msgs=%d  trades=%d  avg_json=%.1fμs",
                        elapsed, total_msgs, total_trades,
                        json_parse_ns / total_msgs / 1000,
                    )

            for sym in symbols:
                if current_count.get(sym, 0) > 0:
                    counts[sym].append(current_count[sym])

    elapsed = time.monotonic() - t_start
    print("\n" + "=" * 60)
    print(f"  OKX Trades Throughput Benchmark  ({elapsed:.1f}s)")
    print("=" * 60)

    for sym in symbols:
        sec_counts = counts[sym]
        if not sec_counts:
            print(f"\n  {sym}: no data received")
            continue
        avg = sum(sec_counts) / len(sec_counts)
        peak = max(sec_counts)
        p50 = sorted(sec_counts)[len(sec_counts) // 2]
        print(f"\n  {sym}:")
        print(f"    seconds sampled : {len(sec_counts)}")
        print(f"    avg trades/sec  : {avg:.1f}")
        print(f"    median (p50)    : {p50}")
        print(f"    peak            : {peak}")
        print(f"    min             : {min(sec_counts)}")

    print(f"\n  Total across all symbols:")
    print(f"    WS messages     : {total_msgs}")
    print(f"    trades parsed   : {total_trades}")
    print(f"    avg json.loads  : {json_parse_ns / max(total_msgs, 1) / 1000:.1f} μs/msg")
    print(f"    msg throughput  : {total_msgs / elapsed:.0f} msg/s")
    print(f"    trade throughput: {total_trades / elapsed:.0f} trades/s")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="OKX trades throughput benchmark")
    parser.add_argument(
        "--symbols", nargs="+", default=["BTC-USDT-SWAP"],
        help="Symbols to subscribe (default: BTC-USDT-SWAP)",
    )
    parser.add_argument(
        "--duration", type=int, default=30,
        help="Seconds to collect (default: 30)",
    )
    args = parser.parse_args()
    asyncio.run(run(args.symbols, args.duration))


if __name__ == "__main__":
    main()
