"""Verify Binance aggregate audit bars against official 1m klines.

This verifier is a sanity check for aggregate audit logs:

    audit 5s bars -> aggregate to 1m
    Binance official 1m klines -> compare OHLCV

It does not replay raw trades.  It compares the online aggregate bars that
ClockDriver wrote to disk with Binance's official minute kline API.

Example:
    python examples/verifier/verify_binance_audit_vs_kline.py \
      --audit-file examples/engine/data/audit/binance_trades_demo/aggregate_bars_20260524_080202_part000001.jsonl \
      --symbols BTC-USDT,ETH-USDT,SOL-USDT \
      --start "2026-05-24T16:05:00+08:00" \
      --end "2026-05-24T16:30:00+08:00"
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


BINANCE_KLINE_URL = "https://fapi.binance.com/fapi/v1/klines"
DEFAULT_SYMBOLS = ("BTC-USDT", "ETH-USDT", "SOL-USDT")
ONE_MINUTE_MS = 60_000


@dataclass(slots=True)
class MinuteBar:
    symbol: str
    bucket_ts_ms: int
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float
    vwap: float | None = None
    buy_vol: float = 0.0
    sell_vol: float = 0.0
    trade_count: int = 0
    source_count: int = 0


@dataclass(slots=True)
class Diff:
    field: str
    audit: float | None
    official: float | None
    diff: float | None
    rel_diff: float | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare FactorEngine Binance aggregate audit bars with official Binance 1m klines.",
    )
    parser.add_argument(
        "--audit-file",
        required=True,
        help="Path to aggregate_bars_*.jsonl written by AuditWriter.",
    )
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated unified symbols, e.g. BTC-USDT,ETH-USDT.",
    )
    parser.add_argument(
        "--start",
        required=True,
        help="Inclusive start time. Use ISO with timezone, e.g. 2026-05-24T16:05:00+08:00.",
    )
    parser.add_argument(
        "--end",
        required=True,
        help="Exclusive end time. Use only fully closed minutes.",
    )
    parser.add_argument(
        "--price-tol",
        type=float,
        default=1e-8,
        help="Absolute tolerance for open/high/low/close differences.",
    )
    parser.add_argument(
        "--price-rtol",
        type=float,
        default=0.0,
        help=(
            "Relative tolerance for open/high/low/close differences. "
            "A field matches if absolute or relative tolerance passes."
        ),
    )
    parser.add_argument(
        "--volume-tol",
        type=float,
        default=1e-8,
        help="Absolute tolerance for volume differences.",
    )
    parser.add_argument(
        "--volume-rtol",
        type=float,
        default=0.0,
        help=(
            "Relative tolerance for volume differences. "
            "A field matches if absolute or relative tolerance passes."
        ),
    )
    parser.add_argument(
        "--max-mismatches",
        type=int,
        default=20,
        help="Maximum mismatch rows to print per symbol.",
    )
    parser.add_argument(
        "--audit-ts-is-end",
        action="store_true",
        help=(
            "Treat audit bucket_ts_ms as the bucket end boundary. "
            "For current trades-only ClockDriver audit, this should be set."
        ),
    )
    return parser.parse_args()


def parse_time_ms(value: str) -> int:
    """Parse epoch milliseconds or ISO datetime into epoch milliseconds."""
    if value.isdigit():
        return int(value)

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(
            f"Naive datetime is ambiguous: {value!r}. "
            "Please include timezone, e.g. +08:00 or Z."
        )
    return int(dt.timestamp() * 1000)


def fmt_ts(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()


def to_binance_symbol(symbol: str) -> str:
    return symbol.replace("-", "").upper()


def read_audit_rows(
    path: Path,
    *,
    symbols: set[str],
    start_ms: int,
    end_ms: int,
    audit_ts_is_end: bool = False,
) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_no}: {exc}") from exc

            if row.get("exchange") != "binance":
                continue
            if row.get("symbol") not in symbols:
                continue
            ts = int(row.get("bucket_ts_ms", -1))
            if audit_ts_is_end:
                in_range = start_ms < ts <= end_ms
            else:
                in_range = start_ms <= ts < end_ms
            if in_range:
                rows.append(row)
    return rows


def aggregate_audit_to_1m(
    rows: Iterable[dict],
    *,
    audit_ts_is_end: bool = False,
) -> dict[tuple[str, int], MinuteBar]:
    grouped: dict[tuple[str, int], list[dict]] = {}
    for row in rows:
        ts = int(row["bucket_ts_ms"])
        if audit_ts_is_end:
            freq_ms = int(float(row.get("freq_seconds", 0.0)) * 1000)
            if freq_ms <= 0:
                raise ValueError(
                    "Audit row is missing a valid freq_seconds field; "
                    "cannot use --audit-ts-is-end."
                )
            ts -= freq_ms
        minute_ts = ts - (ts % ONE_MINUTE_MS)
        grouped.setdefault((row["symbol"], minute_ts), []).append(row)

    result: dict[tuple[str, int], MinuteBar] = {}
    for key, bucket_rows in grouped.items():
        bucket_rows.sort(key=lambda r: int(r["bucket_ts_ms"]))
        symbol, minute_ts = key

        first = _first_non_null(bucket_rows, "open")
        last = _last_non_null(bucket_rows, "close")
        highs = [_as_float(r.get("high")) for r in bucket_rows]
        lows = [_as_float(r.get("low")) for r in bucket_rows]
        volumes = [_as_float(r.get("volume")) or 0.0 for r in bucket_rows]

        total_volume = sum(volumes)
        weighted_vwap = 0.0
        vwap_volume = 0.0
        for row, volume in zip(bucket_rows, volumes, strict=True):
            vwap = _as_float(row.get("vwap"))
            if vwap is not None and volume > 0:
                weighted_vwap += vwap * volume
                vwap_volume += volume

        result[key] = MinuteBar(
            symbol=symbol,
            bucket_ts_ms=minute_ts,
            open=first,
            high=max((v for v in highs if v is not None), default=None),
            low=min((v for v in lows if v is not None), default=None),
            close=last,
            volume=total_volume,
            vwap=weighted_vwap / vwap_volume if vwap_volume > 0 else None,
            buy_vol=sum(_as_float(r.get("buy_vol")) or 0.0 for r in bucket_rows),
            sell_vol=sum(_as_float(r.get("sell_vol")) or 0.0 for r in bucket_rows),
            trade_count=sum(int(r.get("trade_count") or 0) for r in bucket_rows),
            source_count=len(bucket_rows),
        )
    return result


def fetch_official_klines(
    symbol: str,
    *,
    start_ms: int,
    end_ms: int,
) -> dict[tuple[str, int], MinuteBar]:
    binance_symbol = to_binance_symbol(symbol)
    current = start_ms
    bars: dict[tuple[str, int], MinuteBar] = {}

    while current < end_ms:
        params = {
            "symbol": binance_symbol,
            "interval": "1m",
            "startTime": current,
            "endTime": end_ms - 1,
            "limit": 1500,
        }
        url = BINANCE_KLINE_URL + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.load(resp)

        if not payload:
            break

        last_open = current
        for item in payload:
            open_time = int(item[0])
            if not (start_ms <= open_time < end_ms):
                continue
            bars[(symbol, open_time)] = MinuteBar(
                symbol=symbol,
                bucket_ts_ms=open_time,
                open=float(item[1]),
                high=float(item[2]),
                low=float(item[3]),
                close=float(item[4]),
                volume=float(item[5]),
                trade_count=int(item[8]),
                source_count=1,
            )
            last_open = open_time

        next_start = last_open + ONE_MINUTE_MS
        if next_start <= current:
            break
        current = next_start

    return bars


def compare_bars(
    audit: MinuteBar,
    official: MinuteBar,
    *,
    price_tol: float,
    price_rtol: float,
    volume_tol: float,
    volume_rtol: float,
) -> list[Diff]:
    diffs: list[Diff] = []
    for field in ("open", "high", "low", "close"):
        audit_value = getattr(audit, field)
        official_value = getattr(official, field)
        diff = _diff(audit_value, official_value)
        rel_diff = _rel_diff(audit_value, official_value)
        if not _within_tolerance(diff, rel_diff, atol=price_tol, rtol=price_rtol):
            diffs.append(Diff(field, audit_value, official_value, diff, rel_diff))

    diff = _diff(audit.volume, official.volume)
    rel_diff = _rel_diff(audit.volume, official.volume)
    if not _within_tolerance(diff, rel_diff, atol=volume_tol, rtol=volume_rtol):
        diffs.append(Diff("volume", audit.volume, official.volume, diff, rel_diff))
    return diffs


def main() -> int:
    args = parse_args()
    audit_file = Path(args.audit_file)
    symbols = {s.strip() for s in args.symbols.split(",") if s.strip()}
    start_ms = parse_time_ms(args.start)
    end_ms = parse_time_ms(args.end)
    if end_ms <= start_ms:
        raise ValueError("--end must be after --start")
    if args.price_tol < 0 or args.volume_tol < 0:
        raise ValueError("--price-tol and --volume-tol must be >= 0")
    if args.price_rtol < 0 or args.volume_rtol < 0:
        raise ValueError("--price-rtol and --volume-rtol must be >= 0")

    start_minute = start_ms - (start_ms % ONE_MINUTE_MS)
    end_minute = end_ms - (end_ms % ONE_MINUTE_MS)
    if start_ms != start_minute or end_ms != end_minute:
        print(
            "WARNING: start/end are not minute-aligned; verifier compares minute buckets "
            f"[{fmt_ts(start_minute)}, {fmt_ts(end_minute)})",
            file=sys.stderr,
        )
    start_ms, end_ms = start_minute, end_minute

    audit_rows = read_audit_rows(
        audit_file,
        symbols=symbols,
        start_ms=start_ms,
        end_ms=end_ms,
        audit_ts_is_end=args.audit_ts_is_end,
    )
    audit_bars = aggregate_audit_to_1m(
        audit_rows,
        audit_ts_is_end=args.audit_ts_is_end,
    )

    print("Verifier: Binance audit aggregate vs official 1m kline")
    print(f"audit_file: {audit_file}")
    print(f"range UTC:  {fmt_ts(start_ms)} -> {fmt_ts(end_ms)}")
    print(f"symbols:    {', '.join(sorted(symbols))}")
    print(f"audit rows: {len(audit_rows)}")
    print(f"audit ts:   {'end boundary' if args.audit_ts_is_end else 'start boundary'}")
    print(f"price tol:  abs={args.price_tol} rel={args.price_rtol}")
    print(f"volume tol: abs={args.volume_tol} rel={args.volume_rtol}")
    print()

    total_mismatches = 0
    for symbol in sorted(symbols):
        official = fetch_official_klines(symbol, start_ms=start_ms, end_ms=end_ms)
        keys = sorted(
            {key for key in audit_bars if key[0] == symbol}
            | {key for key in official if key[0] == symbol}
        )

        matched = 0
        missing_audit = 0
        missing_official = 0
        mismatches: list[tuple[int, list[Diff], MinuteBar | None, MinuteBar | None]] = []

        for key in keys:
            audit_bar = audit_bars.get(key)
            official_bar = official.get(key)
            if audit_bar is None:
                missing_audit += 1
                mismatches.append(
                    (key[1], [Diff("missing_audit", None, None, None, None)], None, official_bar)
                )
                continue
            if official_bar is None:
                missing_official += 1
                mismatches.append(
                    (key[1], [Diff("missing_official", None, None, None, None)], audit_bar, None)
                )
                continue
            diffs = compare_bars(
                audit_bar,
                official_bar,
                price_tol=args.price_tol,
                price_rtol=args.price_rtol,
                volume_tol=args.volume_tol,
                volume_rtol=args.volume_rtol,
            )
            if diffs:
                mismatches.append((key[1], diffs, audit_bar, official_bar))
            else:
                matched += 1

        total_mismatches += len(mismatches)
        print(f"{symbol}:")
        print(f"  official minutes: {len(official)}")
        print(f"  audit minutes:    {sum(1 for key in audit_bars if key[0] == symbol)}")
        print(f"  matched:          {matched}")
        print(f"  missing_audit:    {missing_audit}")
        print(f"  missing_official: {missing_official}")
        print(f"  mismatched:       {len(mismatches) - missing_audit - missing_official}")

        for ts_ms, diffs, audit_bar, official_bar in mismatches[: args.max_mismatches]:
            print(f"  - {fmt_ts(ts_ms)}")
            if audit_bar is not None:
                print(
                    "    audit:    "
                    f"O={audit_bar.open} H={audit_bar.high} L={audit_bar.low} "
                    f"C={audit_bar.close} V={audit_bar.volume} "
                    f"count={audit_bar.trade_count} src5s={audit_bar.source_count}"
                )
            if official_bar is not None:
                print(
                    "    official: "
                    f"O={official_bar.open} H={official_bar.high} L={official_bar.low} "
                    f"C={official_bar.close} V={official_bar.volume} "
                    f"count={official_bar.trade_count}"
                )
            for d in diffs:
                print(
                    f"    diff {d.field}: audit={d.audit} official={d.official} "
                    f"diff={d.diff} rel_diff={d.rel_diff}"
                )
        print()

    return 1 if total_mismatches else 0


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _first_non_null(rows: list[dict], field: str) -> float | None:
    for row in rows:
        value = _as_float(row.get(field))
        if value is not None:
            return value
    return None


def _last_non_null(rows: list[dict], field: str) -> float | None:
    for row in reversed(rows):
        value = _as_float(row.get(field))
        if value is not None:
            return value
    return None


def _diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return a - b


def _rel_diff(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    denominator = max(abs(a), abs(b))
    if denominator == 0:
        return 0.0
    return abs(a - b) / denominator


def _within_tolerance(
    diff: float | None,
    rel_diff: float | None,
    *,
    atol: float,
    rtol: float,
) -> bool:
    if diff is None or rel_diff is None:
        return False
    return abs(diff) <= atol or rel_diff <= rtol


if __name__ == "__main__":
    raise SystemExit(main())
