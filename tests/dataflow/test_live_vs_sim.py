"""Compare get_data() output format between live and simulation modes.

Runs both modes side by side, collects one snapshot each, and verifies
that the array shapes, dtypes, and column semantics are identical.

Usage:
    cd FactorEngine
    python -m tests.test_live_vs_sim
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
logger = logging.getLogger("test_live_vs_sim")

COLUMNS = ["ts", "open", "high", "low", "close", "vol", "vol_ccy", "vol_ccy_quote"]
SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
WAIT_SECONDS = 12  # enough time for both modes to accumulate bars


def collect_snapshot(mode: str) -> dict:
    """Start engine in given mode, wait, collect snapshot, stop."""
    logger.info("--- Starting %s mode ---", mode)
    if mode == "live":
        engine = Engine(
            symbols=SYMBOLS,
            data_freq="5s",
            pull_interval="10s",
            bar_window_length=100,
            mode="live",
        )
    else:
        engine = Engine(
            symbols=SYMBOLS,
            data_freq="5s",
            pull_interval="10s",
            bar_window_length=100,
            mode="simulation",
            sim_bar_interval=1.0,
            sim_seed=42,
        )

    engine.start()
    logger.info("  Waiting %ds for data...", WAIT_SECONDS)
    time.sleep(WAIT_SECONDS)
    snapshot = engine.get_data()
    bar_count = engine.bar_count
    engine.stop()
    logger.info("  Stopped. bar_count=%d, symbols_with_data=%d", bar_count, len(snapshot))
    return snapshot


def print_snapshot(label: str, snapshot: dict):
    """Print detailed format info for a snapshot."""
    logger.info("")
    logger.info("========== %s ==========", label)
    logger.info("  type(snapshot) = %s", type(snapshot).__name__)
    logger.info("  keys = %s", list(snapshot.keys()))

    for symbol, arr in snapshot.items():
        logger.info("")
        logger.info("  [%s]", symbol)
        logger.info("    type(arr)  = %s", type(arr).__name__)
        logger.info("    arr.dtype  = %s", arr.dtype)
        logger.info("    arr.shape  = %s", arr.shape)
        logger.info("    arr.ndim   = %d", arr.ndim)
        if len(arr) > 0:
            logger.info("    num_cols   = %d  (expected %d: %s)", arr.shape[1], len(COLUMNS), COLUMNS)
            latest = arr[-1]
            col_strs = [f"{COLUMNS[i]}={latest[i]:.6f}" for i in range(arr.shape[1])]
            logger.info("    latest_row = [%s]", ", ".join(col_strs))
        else:
            logger.info("    (empty)")


def compare_formats(snap_live: dict, snap_sim: dict):
    """Compare the two snapshots and report pass/fail."""
    logger.info("")
    logger.info("========== FORMAT COMPARISON ==========")
    all_pass = True

    # Check: same keys
    live_keys = set(snap_live.keys())
    sim_keys = set(snap_sim.keys())
    if live_keys == sim_keys:
        logger.info("  [PASS] Same symbols returned: %s", sorted(live_keys))
    else:
        logger.info("  [FAIL] Symbol mismatch: live=%s  sim=%s", sorted(live_keys), sorted(sim_keys))
        all_pass = False

    common = live_keys & sim_keys
    for symbol in sorted(common):
        arr_live = snap_live[symbol]
        arr_sim = snap_sim[symbol]

        checks = []

        # dtype
        if arr_live.dtype == arr_sim.dtype:
            checks.append(f"dtype={arr_live.dtype} PASS")
        else:
            checks.append(f"dtype live={arr_live.dtype} sim={arr_sim.dtype} FAIL")
            all_pass = False

        # ndim
        if arr_live.ndim == arr_sim.ndim:
            checks.append(f"ndim={arr_live.ndim} PASS")
        else:
            checks.append(f"ndim live={arr_live.ndim} sim={arr_sim.ndim} FAIL")
            all_pass = False

        # num columns (shape[1])
        if len(arr_live) > 0 and len(arr_sim) > 0:
            if arr_live.shape[1] == arr_sim.shape[1]:
                checks.append(f"cols={arr_live.shape[1]} PASS")
            else:
                checks.append(f"cols live={arr_live.shape[1]} sim={arr_sim.shape[1]} FAIL")
                all_pass = False
        elif len(arr_live) == 0 and len(arr_sim) == 0:
            checks.append("both empty PASS")
        else:
            checks.append(f"rows live={len(arr_live)} sim={len(arr_sim)} (one empty)")

        logger.info("  [%s] %s", symbol, " | ".join(checks))

    logger.info("")
    if all_pass:
        logger.info("  >>> ALL CHECKS PASSED — live and simulation output formats are identical <<<")
    else:
        logger.info("  >>> SOME CHECKS FAILED <<<")

    return all_pass


def main():
    logger.info("Collecting simulation snapshot...")
    snap_sim = collect_snapshot("simulation")

    logger.info("")
    logger.info("Collecting live snapshot...")
    snap_live = collect_snapshot("live")

    print_snapshot("SIMULATION", snap_sim)
    print_snapshot("LIVE", snap_live)
    ok = compare_formats(snap_live, snap_sim)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
