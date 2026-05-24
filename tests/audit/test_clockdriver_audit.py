from __future__ import annotations

import json
import queue
import time

import numpy as np

from dataflow.audit import AuditConfig, AuditWriter
from dataflow.clock_driver import ClockDriver


class FakeWorker:
    def __init__(self, data):
        self._data = data

    def flush(self):
        return self._data


def test_clockdriver_writes_aggregate_audit_records(tmp_path):
    bar_queue = queue.Queue(maxsize=8)
    audit = AuditWriter(
        AuditConfig(
            enabled=True,
            root_dir=str(tmp_path),
            flush_interval_seconds=0.01,
        )
    )
    audit.start()

    trades_bars = {
        "BTC-USDT": np.array(
            [5000, 100.0, 106.0, 98.0, 103.0, 80.0, np.nan, np.nan],
            dtype=np.float64,
        )
    }
    trades_snaps = {
        "BTC-USDT": {
            "vwap": 102.5,
            "buy_vol": 50.0,
            "sell_vol": 30.0,
            "trade_count": 12,
        }
    }

    cd = ClockDriver(
        freq_seconds=0.5,
        bar_queue=bar_queue,
        audit=audit,
        exchange="binance",
        data_source="trades",
    )
    cd.register("trades", FakeWorker((trades_bars, trades_snaps)))
    cd._flush_and_aggregate()
    audit.stop()

    files = list(tmp_path.glob("aggregate_bars_*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["exchange"] == "binance"
    assert row["data_source"] == "trades"
    assert row["symbol"] == "BTC-USDT"
    assert row["bucket_ts_ms"] == 5000
    assert row["open"] == 100.0
    assert row["close"] == 103.0
    assert row["volume"] == 80.0
    assert row["vwap"] == 102.5
    assert row["buy_vol"] == 50.0
    assert row["sell_vol"] == 30.0
    assert row["trade_count"] == 12
    assert row["source"] == "online_clockdriver"

    round_bars, round_trades = bar_queue.get_nowait()
    assert "BTC-USDT" in round_bars
    assert round_trades["BTC-USDT"]["trade_count"] == 12


def test_clockdriver_audit_errors_do_not_block_queue():
    class BadAudit:
        def write_aggregate_batch(self, **kwargs):
            raise RuntimeError("audit failed")

    bar_queue = queue.Queue(maxsize=8)
    kline_data = {
        "BTC-USDT": np.array(
            [5000, 100.0, 106.0, 98.0, 103.0, 80.0, 0.0, 0.0],
            dtype=np.float64,
        )
    }
    cd = ClockDriver(
        freq_seconds=0.5,
        bar_queue=bar_queue,
        audit=BadAudit(),
        exchange="binance",
        data_source="kline",
    )
    cd.register("kline", FakeWorker(kline_data))
    cd._flush_and_aggregate()

    round_bars, round_trades = bar_queue.get_nowait()
    assert "BTC-USDT" in round_bars
    assert round_trades is None


def test_clockdriver_supports_audit_only_without_bar_queue(tmp_path):
    audit = AuditWriter(
        AuditConfig(
            enabled=True,
            root_dir=str(tmp_path),
            flush_interval_seconds=0.01,
        )
    )
    audit.start()
    kline_data = {
        "BTC-USDT": np.array(
            [5000, 100.0, 106.0, 98.0, 103.0, 80.0, 0.0, 0.0],
            dtype=np.float64,
        )
    }
    cd = ClockDriver(
        freq_seconds=0.5,
        bar_queue=None,
        audit=audit,
        exchange="binance",
        data_source="kline",
    )
    cd.register("kline", FakeWorker(kline_data))
    cd._flush_and_aggregate()
    audit.stop()

    files = list(tmp_path.glob("aggregate_bars_*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text().splitlines()]
    assert rows[0]["symbol"] == "BTC-USDT"
    assert rows[0]["data_source"] == "kline"
