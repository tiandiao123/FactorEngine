from __future__ import annotations

import json

from examples.verifier.verify_binance_audit_vs_kline import (
    build_symbol_universe_timeline,
    read_symbol_diff_events,
)


def test_verifier_reads_symbol_diff_events_in_effective_order(tmp_path):
    path = tmp_path / "audit.jsonl"
    rows = [
        {
            "event_type": "symbol_diff",
            "exchange": "binance",
            "commit_ts_ms": 3000,
            "effective_from_ts_ms": 2000,
            "symbol_version": "v3",
            "added": [],
            "removed": ["ETH-USDT"],
        },
        {
            "event_type": "aggregate_bar",
            "exchange": "binance",
            "symbol": "BTC-USDT",
            "bucket_ts_ms": 2500,
        },
        {
            "event_type": "symbol_diff",
            "exchange": "binance",
            "commit_ts_ms": 2000,
            "effective_from_ts_ms": 1000,
            "symbol_version": "v2",
            "added": ["ETH-USDT"],
            "removed": [],
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    events = read_symbol_diff_events(path)

    assert [event["symbol_version"] for event in events] == ["v2", "v3"]


def test_verifier_timeline_marks_removed_symbol_inactive():
    timeline = build_symbol_universe_timeline(
        {"BTC-USDT", "ETH-USDT"},
        [
            {
                "effective_from_ts_ms": 1000,
                "added": ["ETH-USDT"],
                "removed": [],
            },
            {
                "effective_from_ts_ms": 2000,
                "added": [],
                "removed": ["ETH-USDT"],
            },
        ],
    )

    assert timeline.is_active("BTC-USDT", 500)
    assert not timeline.is_active("ETH-USDT", 500)
    assert timeline.is_active("ETH-USDT", 1500)
    assert not timeline.is_active("ETH-USDT", 2500)
