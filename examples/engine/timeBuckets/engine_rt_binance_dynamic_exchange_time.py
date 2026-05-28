"""Dynamic symbol universe demo — Binance Futures with exchange-time buckets.

This example rotates through several small Binance USDT-M perpetual symbol
lists while using trade_time_mode="exchange_time".  Trades are bucketed by the
exchange trade timestamp, then finalized after allowed_lateness_ms so the
runtime receives each bucket at most once.

The log is intentionally verbose enough to verify the full dynamic path:

    SymbolManager pending diff
      -> ClockDriver post-flush commit
      -> BinanceTradesWorker collector rebuild
      -> C++ InferenceEngine add/remove SymbolRunner
      -> factor outputs filtered by active universe
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import fe_runtime
from dataflow.audit import AuditConfig
from factorengine.engine import Engine


TESTNET = False
DATA_FREQ = "5s"
NUM_THREADS = 4
BAR_WINDOW = 200
UPDATE_EVERY_SECONDS = 1200 
RUN_SECONDS = 7200
PRINT_EVERY_CYCLES = 1
PRINT_TOP_K = 8
AUDIT_ENABLED = True
AUDIT_ROOT = str(EXAMPLE_DIR / "data/audit/binance_dynamic_exchange_time_demo")
LOG_FILE = str(EXAMPLE_DIR / "data/logs/binance_dynamic_exchange_time_demo.log")

ALLOWED_LATENESS_MS = 200


def _configure_logging(log_file: str) -> logging.Logger:
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    return logging.getLogger("dynamic_exchange_time_demo")


logger = _configure_logging(LOG_FILE)


SYMBOL_LISTS = [
    ["BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT"],
    ["BTC-USDT", "ETH-USDT", "DOGE-USDT", "ADA-USDT", "LINK-USDT"],
    ["SOL-USDT", "BNB-USDT", "AVAX-USDT", "DOT-USDT", "NEAR-USDT"],
    ["BTC-USDT", "XRP-USDT", "LTC-USDT", "BCH-USDT", "ETC-USDT"],
    ["ETH-USDT", "SOL-USDT", "ARB-USDT", "OP-USDT", "SUI-USDT"],
    ["BTC-USDT", "ETH-USDT", "SOL-USDT", "DOGE-USDT", "XRP-USDT", "ADA-USDT"],
]


def _version(list_idx: int) -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"manual-rotate-exchange-time:{ts}:{list_idx}"


def _runtime_symbols(engine: Engine) -> list[str]:
    inference = getattr(engine, "_inference", None)
    if inference is None:
        return []
    try:
        return list(inference.symbols())
    except Exception as exc:
        return [f"<runtime symbols unavailable: {exc}>"]


def _format_diff(diff) -> str:
    if diff is None:
        return "None"
    return (
        f"version={diff.version} "
        f"added={sorted(diff.added)} "
        f"removed={sorted(diff.removed)} "
        f"unchanged={len(diff.unchanged)} "
        f"target={sorted(diff.target)}"
    )


def _nonzero_summary(engine: Engine, factors: dict[str, dict[str, float]]) -> tuple[int, int, int]:
    nonzero_total = 0
    nan_total = 0
    factor_total = max(1, len(engine.active_symbols) * len(engine.factor_ids))
    for sym in sorted(engine.active_symbols):
        for value in factors.get(sym, {}).values():
            if value != value:
                nan_total += 1
            elif value != 0.0:
                nonzero_total += 1
    return nonzero_total, nan_total, factor_total


def _log_state(engine: Engine, *, cycle: int, list_idx: int, next_update_in: float) -> None:
    factors = engine.get_factor_outputs()
    ready_factors = engine.get_ready_factor_outputs()
    symbol_status = engine.get_symbol_status()
    nonzero_total, nan_total, factor_total = _nonzero_summary(engine, factors)
    active = sorted(engine.active_symbols)
    runtime_symbols = _runtime_symbols(engine)
    ready_symbols = sorted(sym for sym, status in symbol_status.items() if status.get("ready"))
    max_warmup_bars = max(
        (int(status.get("warmup_bars", 0)) for status in symbol_status.values()),
        default=0,
    )
    min_symbol_bars = min(
        (int(status.get("bars_pushed", 0)) for status in symbol_status.values()),
        default=0,
    )
    warmup_pct = (
        min(100, int(100 * min_symbol_bars / max_warmup_bars))
        if max_warmup_bars > 0
        else 0
    )

    logger.info(
        f"\n═══ Cycle {cycle:4d} list={list_idx} "
        f"bars_pushed={engine.bars_pushed:5d} "
        f"symbol_warmup={warmup_pct}% ({min_symbol_bars}/{max_warmup_bars}bars) "
        f"next_update_in={next_update_in:6.1f}s"
    )
    logger.info(f"  active({len(active)}):  {active}")
    logger.info(f"  runtime({len(runtime_symbols)}): {runtime_symbols}")
    logger.info(f"  symbol_version: {engine.symbol_version}")
    logger.info(f"  pending_diff:   {_format_diff(engine.pending_symbol_diff)}")
    logger.info(f"  symbol_stats:   {engine.symbol_stats}")
    logger.info(f"  trades_stats:   {engine.trades_stats}")
    logger.info(f"  clock_stats:    {engine.clock_stats}")
    logger.info(f"  audit_stats:    {engine.audit_stats}")
    logger.info(f"  factors:        nonzero={nonzero_total}/{factor_total} nan={nan_total}")
    logger.info(
        f"  ready_symbols:  {len(ready_symbols)}/{len(active)} "
        f"ready_outputs={len(ready_factors)}"
    )

    if factors:
        ranked = []
        for sym in active:
            fvals = factors.get(sym, {})
            status = symbol_status.get(sym, {})
            n_nontrivial = sum(1 for v in fvals.values() if v == v and v != 0.0)
            ranked.append((
                int(bool(status.get("ready"))),
                int(status.get("ready_factor_count", 0)),
                sym,
                fvals,
                status,
                n_nontrivial,
            ))
        ranked.sort(reverse=True)
        logger.info(f"  ── Top {min(PRINT_TOP_K, len(ranked))} symbols by runtime readiness ──")
        for _, _, sym, fvals, status, n_nontrivial in ranked[:PRINT_TOP_K]:
            ready = bool(status.get("ready"))
            bars_pushed = status.get("bars_pushed", 0)
            warmup_bars = status.get("warmup_bars", 0)
            ready_factor_count = status.get("ready_factor_count", 0)
            factor_count = status.get("factor_count", len(fvals))
            status_note = ""
            if not status:
                status_note = "status unavailable"
            elif warmup_bars <= 0:
                status_note = "warmup metadata unavailable"
            shown = "  ".join(
                f"{fid}={v:+.4f}" if v == v else f"{fid}=NaN"
                for fid, v in sorted(fvals.items())
                if (v != v) or v != 0.0
            )
            if not shown:
                shown = "(no nonzero outputs yet)"
            logger.info(
                f"  {sym:<14s} ready={str(ready):<5s} "
                f"bars={bars_pushed:4d}/{warmup_bars:<4d} "
                f"ready_factors={ready_factor_count:3d}/{factor_count:<3d} "
                f"nonzero={n_nontrivial:3d}  {shown}"
                f"{f' [{status_note}]' if status_note else ''}"
            )


initial_symbols = SYMBOL_LISTS[0]

engine = Engine(
    symbols=initial_symbols,
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
    enable_dynamic_symbols=True,
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

logger.info("Dynamic Binance exchange-time trades symbol experiment")
logger.info(f"Log file:         {LOG_FILE}")
logger.info(f"Project root:     {PROJECT_ROOT}")
logger.info(f"Example dir:      {EXAMPLE_DIR}")
logger.info(f"fe_runtime:       {getattr(fe_runtime, '__file__', '<builtin>')}")
logger.info(
    "Status API:       %s",
    hasattr(fe_runtime.InferenceEngine(), "get_all_symbol_status"),
)
logger.info(f"Exchange:         {engine.exchange}")
logger.info(f"Data source:      {engine.data_source}")
logger.info(f"Data freq:        {engine.data_freq}")
logger.info("Trade time mode:  exchange_time")
logger.info(f"Allowed lateness: {ALLOWED_LATENESS_MS}ms")
logger.info(f"Factor IDs:       {engine.factor_ids}")
logger.info(f"Audit:            {AUDIT_ENABLED} -> {AUDIT_ROOT}")
logger.info(f"Update every:     {UPDATE_EVERY_SECONDS}s")
logger.info(f"Run duration:     {RUN_SECONDS}s")
for i, symbols in enumerate(SYMBOL_LISTS):
    logger.info(f"  list[{i}] ({len(symbols)}): {symbols}")


engine.start()

freq_seconds = engine.data_freq_seconds
current_list_idx = 0
end_ts = time.monotonic() + RUN_SECONDS
next_update_ts = time.monotonic() + UPDATE_EVERY_SECONDS

try:
    cycle = 0
    while time.monotonic() < end_ts:
        cycle += 1
        time.sleep(freq_seconds)
        now = time.monotonic()
        if now >= end_ts:
            break

        if now >= next_update_ts:
            current_list_idx = (current_list_idx + 1) % len(SYMBOL_LISTS)
            target_symbols = SYMBOL_LISTS[current_list_idx]
            version = _version(current_list_idx)
            diff = engine.update_symbols(
                target_symbols,
                version=version,
                metadata={
                    "reason": "manual_rotation_exchange_time_experiment",
                    "list_idx": current_list_idx,
                    "update_every_seconds": UPDATE_EVERY_SECONDS,
                    "trade_time_mode": "exchange_time",
                    "allowed_lateness_ms": ALLOWED_LATENESS_MS,
                },
            )
            logger.info("\n>>> staged dynamic symbol update")
            logger.info(f"    target list[{current_list_idx}]: {target_symbols}")
            logger.info(f"    {_format_diff(diff)}")
            next_update_ts += UPDATE_EVERY_SECONDS

        if cycle == 1 or cycle % PRINT_EVERY_CYCLES == 0:
            _log_state(
                engine,
                cycle=cycle,
                list_idx=current_list_idx,
                next_update_in=max(0.0, next_update_ts - now),
            )

except KeyboardInterrupt:
    logger.info("Interrupted by user.")
finally:
    logger.info("Run duration reached or interrupted; stopping engine.")
    engine.stop()
    logger.info("Done.")
