"""Real-time factor inference demo — Binance Futures with exchange-time bucketing.

Uses exchange="binance", data_source="trades", trade_time_mode="exchange_time"
so that OHLCV bars are bucketed by the exchange's trade timestamp rather than
local receive time.  This reduces mis-bucketing around second boundaries at
the cost of a small delay (~allowed_lateness_ms).

This demo automatically picks the top-N USDT-M perpetuals by 24h quote
volume and runs them at 1s bars with a 500ms allowed lateness window.
"""
import json
import logging
import os
import sys
import time
import urllib.request

from dataflow.audit import AuditConfig
from factorengine.engine import Engine

TESTNET = False
TOP_N = 200
DATA_FREQ = "5s"
NUM_THREADS = 8
BAR_WINDOW = 200
PRINT_DETAIL_EVERY = 5
PRINT_TOP_K = 8
AUDIT_ENABLED = True
AUDIT_ROOT = "data/audit/binance_exchange_time_demo"

ALLOWED_LATENESS_MS = 200
WARMUP_BARS = 120
LOG_FILE = "data/logs/binance_exchange_time_demo.log"


class _Tee:
    """Write print output to both terminal and a log file."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
_print_log = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
sys.stdout = _Tee(sys.__stdout__, _print_log)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)


def _vis_pad(s: str, width: int) -> str:
    """Left-justify a string to `width` columns of *display* width."""
    import unicodedata
    vis = sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)
    pad = max(0, width - vis)
    return s + " " * pad


def pick_top_symbols(n: int) -> list[str]:
    """Return top-N USDT-M perpetuals by 24h quote volume, unified format."""
    info_url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    tick_url = "https://fapi.binance.com/fapi/v1/ticker/24hr"

    with urllib.request.urlopen(info_url, timeout=10) as r:
        info = json.load(r)
    with urllib.request.urlopen(tick_url, timeout=10) as r:
        ticks = json.load(r)

    eligible = {
        s["symbol"]: s["baseAsset"]
        for s in info["symbols"]
        if s.get("contractType") == "PERPETUAL"
        and s.get("quoteAsset") == "USDT"
        and s.get("status") == "TRADING"
    }

    ranked = sorted(
        (t for t in ticks if t["symbol"] in eligible),
        key=lambda t: float(t.get("quoteVolume", 0.0)),
        reverse=True,
    )
    chosen = ranked[:n]
    return [f"{eligible[t['symbol']]}-USDT" for t in chosen]


symbols = pick_top_symbols(TOP_N)
print(f"Picked top {len(symbols)} symbols by 24h quoteVolume")
print(f"  head: {symbols[:5]}")
print(f"  tail: {symbols[-5:]}")

engine = Engine(
    symbols=symbols,
    mode="live",
    exchange="binance",
    data_freq=DATA_FREQ,
    factor_group=["okx_perp_trades"],
    num_threads=NUM_THREADS,
    bar_window_length=BAR_WINDOW,
    data_source="trades",
    trade_time_mode="exchange_time",
    allowed_lateness_ms=ALLOWED_LATENESS_MS,
    testnet=TESTNET,
    audit_config=AuditConfig(
        enabled=AUDIT_ENABLED,
        root_dir=AUDIT_ROOT,
        flush_interval_seconds=1.0,
        flush_max_records=10_000,
        flush_max_bytes=4 * 1024 * 1024,
        rotate_interval_seconds=3600,
        rotate_max_bytes=512 * 1024 * 1024,
    ),
)

print(f"Exchange:          {engine.exchange}")
print(f"Symbols:           {len(engine.symbols)}")
print(f"Factors:           {len(engine.factor_ids)}")
print(f"Factor IDs:        {engine.factor_ids}")
print(f"Data source:       {engine.data_source}")
print(f"Data freq:         {engine.data_freq}")
print(f"Trade time mode:   exchange_time")
print(f"Allowed lateness:  {ALLOWED_LATENESS_MS}ms")
print(f"Audit:             {AUDIT_ENABLED} -> {AUDIT_ROOT}")

engine.start()

freq_seconds = engine.data_freq_seconds

try:
    for cycle in range(1, 6000):
        time.sleep(freq_seconds)

        factors = engine.get_factor_outputs()

        nonzero_total = 0
        nan_total = 0
        for sym in engine.symbols:
            for v in factors.get(sym, {}).values():
                if v != v:
                    nan_total += 1
                elif v != 0.0:
                    nonzero_total += 1
        factor_total = max(1, len(engine.symbols) * len(engine.factor_ids))

        warmup_pct = min(100, int(100 * engine.bars_pushed / WARMUP_BARS))

        print(
            f"\n═══ Cycle {cycle:3d}  bars_pushed={engine.bars_pushed:5d}  "
            f"warmup={warmup_pct}%  "
            f"trades={engine.trades_stats}  "
            f"clock={engine.clock_stats}  "
            f"audit={engine.audit_stats}  "
            f"nonzero={nonzero_total}/{factor_total} nan={nan_total}"
        )

        if cycle != 1 and cycle % PRINT_DETAIL_EVERY != 0:
            continue

        debug = engine.last_bar_debug
        if debug:
            print(
                f"  ── BarData pushed (bar_index={debug['bar_index']}, "
                f"ts={debug['ts']}, showing first {PRINT_TOP_K}) ──"
            )
            for sym, bd in list(debug["bars"].items())[:PRINT_TOP_K]:
                print(
                    f"    {_vis_pad(sym, 14)}  "
                    f"O={bd['open']:<10.4f} H={bd['high']:<10.4f} "
                    f"L={bd['low']:<10.4f} C={bd['close']:<10.4f} "
                    f"V={bd['volume']:<12.4f} ret={bd['ret']:+.6f}"
                )
                print(
                    f"    {'':14s}  "
                    f"vwap={bd['vwap']:<14.6f} "
                    f"buy_vol={bd['buy_vol']:<12.4f} "
                    f"sell_vol={bd['sell_vol']:<12.4f}"
                )

        ranked = []
        for sym in engine.symbols:
            fvals = factors.get(sym, {})
            n_nontrivial = sum(1 for v in fvals.values() if v == v and v != 0.0)
            if n_nontrivial > 0:
                ranked.append((n_nontrivial, sym, fvals))
        ranked.sort(reverse=True)

        if not ranked:
            print(
                f"  (all factors still 0.0 — warmup needs ~{WARMUP_BARS} bars, "
                f"have {engine.bars_pushed})"
            )
        else:
            print(f"  ── Top {min(PRINT_TOP_K, len(ranked))} symbols by warm factors ──")
            for _, sym, fvals in ranked[:PRINT_TOP_K]:
                items = "  ".join(
                    f"{fid}={v:+.4f}" if v == v else f"{fid}=NaN"
                    for fid, v in sorted(fvals.items())
                    if (v != v) or v != 0.0
                )
                print(f"  {_vis_pad(sym, 14)}  {items}")

finally:
    engine.stop()
    print("Done.")
