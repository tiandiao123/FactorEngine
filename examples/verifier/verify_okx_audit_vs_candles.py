"""Verify OKX aggregate audit bars against official OKX 1m candles.

This verifier is a sanity check for OKX trades-only aggregate audit logs:

    audit sub-minute bars -> aggregate to 1m
    OKX official 1m candles -> compare OHLCV

For OKX swaps, the audit ``volume`` comes from trade ``sz`` and is compared
with the candle ``vol`` field.  The OKX candle response also includes
``volCcy`` and ``volCcyQuote``, but those are not the same unit.
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


OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/history-candles"
DEFAULT_SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")
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
    trade_count: int = 0
    source_count: int = 0


@dataclass(slots=True)
class Diff:
    field: str
    audit: float | None
    official: float | None
    diff: float | None
    rel_diff: float | None


@dataclass(slots=True)
class SymbolUniverseTimeline:
    initial_symbols: frozenset[str]
    events: list[dict]

    def is_active(self, symbol: str, bucket_ts_ms: int) -> bool:
        active = set(self.initial_symbols)
        for event in self.events:
            effective_ts = event.get("effective_from_ts_ms")
            if effective_ts is None:
                effective_ts = event.get("commit_ts_ms")
            if effective_ts is None or int(effective_ts) > bucket_ts_ms:
                break
            active.update(event.get("added", []))
            active.difference_update(event.get("removed", []))
        return symbol in active


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare FactorEngine OKX aggregate audit bars with official OKX 1m candles.",
    )
    parser.add_argument(
        "--audit-file",
        required=True,
        help="Path to aggregate_bars_*.jsonl written by AuditWriter.",
    )
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated OKX symbols, e.g. BTC-USDT-SWAP,ETH-USDT-SWAP.",
    )
    parser.add_argument(
        "--start",
        required=True,
        help="Inclusive start time. Use ISO with timezone, e.g. 2026-05-28T12:00:00Z.",
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
        help="Relative tolerance for open/high/low/close differences.",
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
        help="Relative tolerance for volume differences.",
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
        help="Treat audit bucket_ts_ms as the bucket end boundary.",
    )
    return parser.parse_args()


def parse_time_ms(value: str) -> int:
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

            if row.get("exchange") != "okx":
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


def read_symbol_diff_events(path: Path, *, exchange: str = "okx") -> list[dict]:
    events: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON at {path}:{line_no}: {exc}") from exc
            if row.get("event_type") != "symbol_diff":
                continue
            if row.get("exchange") != exchange:
                continue
            events.append(row)
    return sorted(
        events,
        key=lambda event: int(
            event.get("effective_from_ts_ms")
            if event.get("effective_from_ts_ms") is not None
            else event.get("commit_ts_ms", 0)
        ),
    )


def build_symbol_universe_timeline(
    symbols: set[str],
    events: list[dict],
) -> SymbolUniverseTimeline:
    initial_symbols = set(symbols)
    for event in events:
        initial_symbols.difference_update(event.get("added", []))
    return SymbolUniverseTimeline(
        initial_symbols=frozenset(initial_symbols),
        events=events,
    )


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
        highs = [_as_float(r.get("high")) for r in bucket_rows]
        lows = [_as_float(r.get("low")) for r in bucket_rows]

        result[key] = MinuteBar(
            symbol=symbol,
            bucket_ts_ms=minute_ts,
            open=_first_non_null(bucket_rows, "open"),
            high=max((v for v in highs if v is not None), default=None),
            low=min((v for v in lows if v is not None), default=None),
            close=_last_non_null(bucket_rows, "close"),
            volume=sum(_as_float(r.get("volume")) or 0.0 for r in bucket_rows),
            trade_count=sum(int(r.get("trade_count") or 0) for r in bucket_rows),
            source_count=len(bucket_rows),
        )
    return result


def fetch_official_candles(
    symbol: str,
    *,
    start_ms: int,
    end_ms: int,
) -> dict[tuple[str, int], MinuteBar]:
    params = {
        "instId": symbol,
        "bar": "1m",
        # OKX treats `before` as an exclusive lower boundary for this endpoint.
        "before": max(0, start_ms - ONE_MINUTE_MS),
        "after": end_ms,
        "limit": 300,
    }
    url = OKX_CANDLES_URL + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=10) as resp:
        payload = json.load(resp)

    if payload.get("code") != "0":
        raise RuntimeError(f"OKX candles request failed for {symbol}: {payload}")

    bars: dict[tuple[str, int], MinuteBar] = {}
    for item in payload.get("data", []):
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
            trade_count=0,
            source_count=1,
        )
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
    symbol_diff_events = read_symbol_diff_events(audit_file)
    universe_timeline = build_symbol_universe_timeline(symbols, symbol_diff_events)
    audit_bars = aggregate_audit_to_1m(
        audit_rows,
        audit_ts_is_end=args.audit_ts_is_end,
    )

    print("Verifier: OKX audit aggregate vs official 1m candles")
    print(f"audit_file: {audit_file}")
    print(f"range UTC:  {fmt_ts(start_ms)} -> {fmt_ts(end_ms)}")
    print(f"symbols:    {', '.join(sorted(symbols))}")
    print(f"audit rows: {len(audit_rows)}")
    print(f"symbol diffs: {len(symbol_diff_events)}")
    print(f"audit ts:   {'end boundary' if args.audit_ts_is_end else 'start boundary'}")
    print(f"price tol:  abs={args.price_tol} rel={args.price_rtol}")
    print(f"volume tol: abs={args.volume_tol} rel={args.volume_rtol}")
    print()

    total_mismatches = 0
    for symbol in sorted(symbols):
        official = fetch_official_candles(symbol, start_ms=start_ms, end_ms=end_ms)
        keys = sorted(
            {key for key in audit_bars if key[0] == symbol}
            | {key for key in official if key[0] == symbol}
        )

        matched = 0
        missing_audit = 0
        inactive_missing_audit = 0
        missing_official = 0
        mismatches: list[tuple[int, list[Diff], MinuteBar | None, MinuteBar | None]] = []

        for key in keys:
            audit_bar = audit_bars.get(key)
            official_bar = official.get(key)
            if audit_bar is None:
                if not universe_timeline.is_active(symbol, key[1]):
                    inactive_missing_audit += 1
                    continue
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
        print(f"  inactive_missing: {inactive_missing_audit}")
        print(f"  missing_official: {missing_official}")
        print(f"  mismatched:       {len(mismatches) - missing_audit - missing_official}")

        for ts_ms, diffs, audit_bar, official_bar in mismatches[: args.max_mismatches]:
            print(f"  - {fmt_ts(ts_ms)}")
            if audit_bar is not None:
                print(
                    "    audit:    "
                    f"O={audit_bar.open} H={audit_bar.high} L={audit_bar.low} "
                    f"C={audit_bar.close} V={audit_bar.volume} "
                    f"count={audit_bar.trade_count} src={audit_bar.source_count}"
                )
            if official_bar is not None:
                print(
                    "    official: "
                    f"O={official_bar.open} H={official_bar.high} L={official_bar.low} "
                    f"C={official_bar.close} V={official_bar.volume}"
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
