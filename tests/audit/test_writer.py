from __future__ import annotations

import json
import time

from dataflow.audit import AuditConfig, AuditWriter
from dataflow.audit.schema import AggregateBarEvent


def test_audit_writer_writes_aggregate_jsonl(tmp_path):
    writer = AuditWriter(
        AuditConfig(
            enabled=True,
            root_dir=str(tmp_path),
            flush_interval_seconds=0.01,
        )
    )
    writer.start()
    writer.write_aggregate_bar(
        AggregateBarEvent(
            bucket_ts_ms=1000,
            flush_ts_ms=1100,
            exchange="binance",
            data_source="trades",
            freq_seconds=1.0,
            symbol="BTC-USDT",
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=10.0,
            vwap=1.25,
            buy_vol=6.0,
            sell_vol=4.0,
            trade_count=3,
        )
    )
    writer.stop()

    files = list(tmp_path.glob("aggregate_bars_*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert rows == [
        {
            "bucket_ts_ms": 1000,
            "flush_ts_ms": 1100,
            "exchange": "binance",
            "data_source": "trades",
            "freq_seconds": 1.0,
            "symbol": "BTC-USDT",
            "open": 1.0,
            "high": 2.0,
            "low": 0.5,
            "close": 1.5,
            "volume": 10.0,
            "vwap": 1.25,
            "buy_vol": 6.0,
            "sell_vol": 4.0,
            "trade_count": 3,
            "source": "online_clockdriver",
        }
    ]
    assert writer.stats()["written"] == 1


def test_audit_writer_drops_when_queue_full(tmp_path):
    writer = AuditWriter(
        AuditConfig(
            enabled=True,
            root_dir=str(tmp_path),
            queue_size=1,
            flush_interval_seconds=10.0,
        )
    )

    for i in range(100):
        writer.write_aggregate_bar(
            AggregateBarEvent(
                bucket_ts_ms=i,
                flush_ts_ms=i,
                exchange="binance",
                data_source="trades",
                freq_seconds=1.0,
                symbol="BTC-USDT",
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1.0,
                vwap=None,
                buy_vol=None,
                sell_vol=None,
                trade_count=0,
            )
        )

    assert writer.stats()["dropped"] > 0


def test_audit_writer_rotates_by_interval(tmp_path):
    writer = AuditWriter(
        AuditConfig(
            enabled=True,
            root_dir=str(tmp_path),
            flush_interval_seconds=0.01,
            rotate_interval_seconds=1,
        )
    )
    writer.start()
    writer.write_aggregate_bar(
        AggregateBarEvent(
            bucket_ts_ms=1000,
            flush_ts_ms=1100,
            exchange="binance",
            data_source="trades",
            freq_seconds=1.0,
            symbol="BTC-USDT",
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
            vwap=None,
            buy_vol=None,
            sell_vol=None,
            trade_count=0,
        )
    )
    time.sleep(1.2)
    writer.write_aggregate_bar(
        AggregateBarEvent(
            bucket_ts_ms=2000,
            flush_ts_ms=2100,
            exchange="binance",
            data_source="trades",
            freq_seconds=1.0,
            symbol="ETH-USDT",
            open=2.0,
            high=2.0,
            low=2.0,
            close=2.0,
            volume=2.0,
            vwap=None,
            buy_vol=None,
            sell_vol=None,
            trade_count=0,
        )
    )
    writer.stop()

    files = list(tmp_path.glob("aggregate_bars_*.jsonl"))
    assert len(files) >= 2
