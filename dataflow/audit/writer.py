"""Asynchronous JSONL writer for aggregate audit records."""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AuditConfig
from .schema import AggregateBarEvent, SymbolDiffEvent

logger = logging.getLogger(__name__)


_SENTINEL = object()


class AuditWriter:
    """Write aggregate audit records from a background thread.

    Producers use non-blocking enqueue.  If the queue is full, audit records
    are dropped and counted so the realtime path is not back-pressured by
    disk I/O.
    """

    def __init__(self, config: AuditConfig):
        self._config = config
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=config.queue_size)
        self._thread: threading.Thread | None = None
        self._running = False
        self._file = None
        self._file_path: Path | None = None
        self._file_open_ts = 0.0
        self._file_bytes = 0
        self._part = 0
        self._session = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

        self._enqueued = 0
        self._written = 0
        self._dropped = 0
        self._write_errors = 0
        self._flush_count = 0
        self._last_flush_ts = 0.0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="audit-writer"
        )
        self._thread.start()
        logger.info("AuditWriter started: root=%s", self._config.root_dir)

    def stop(self) -> None:
        if not self._running and self._thread is None:
            return
        self._running = False
        self._enqueue_sentinel()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        logger.info(
            "AuditWriter stopped (written=%d, dropped=%d, errors=%d)",
            self._written, self._dropped, self._write_errors,
        )

    def write_aggregate_bar(self, event: AggregateBarEvent) -> None:
        self._enqueue(("aggregate_bar", event.to_dict()))

    def write_symbol_diff(self, event: SymbolDiffEvent) -> None:
        self._enqueue(("symbol_diff", event.to_dict()))

    def write_aggregate_batch(
        self,
        *,
        exchange: str,
        data_source: str,
        freq_seconds: float,
        bars: dict[str, Any],
        trades: dict[str, dict] | None,
        flush_ts_ms: int | None = None,
        symbol_version: str | None = None,
        active_symbols_count: int | None = None,
    ) -> None:
        flush_ts = int(time.time() * 1000) if flush_ts_ms is None else flush_ts_ms
        trade_map = trades or {}
        for symbol, bar in bars.items():
            trade = trade_map.get(symbol, {})
            self.write_aggregate_bar(
                AggregateBarEvent(
                    bucket_ts_ms=int(bar[0]),
                    flush_ts_ms=flush_ts,
                    exchange=exchange,
                    data_source=data_source,
                    freq_seconds=float(freq_seconds),
                    symbol=symbol,
                    open=_finite_or_none(bar[1]),
                    high=_finite_or_none(bar[2]),
                    low=_finite_or_none(bar[3]),
                    close=_finite_or_none(bar[4]),
                    volume=_finite_or_none(bar[5]),
                    vwap=_finite_or_none(trade.get("vwap")),
                    buy_vol=_finite_or_none(trade.get("buy_vol")),
                    sell_vol=_finite_or_none(trade.get("sell_vol")),
                    trade_count=int(trade.get("trade_count", 0) or 0),
                    symbol_version=symbol_version,
                    active_symbols_count=active_symbols_count,
                )
            )

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "queued": self._queue.qsize(),
            "enqueued": self._enqueued,
            "written": self._written,
            "dropped": self._dropped,
            "write_errors": self._write_errors,
            "flush_count": self._flush_count,
            "last_flush_ts": int(self._last_flush_ts * 1000) if self._last_flush_ts else 0,
            "current_file": str(self._file_path) if self._file_path else "",
        }

    def _enqueue(self, record: tuple[str, dict]) -> None:
        try:
            self._queue.put_nowait(record)
            self._enqueued += 1
        except queue.Full:
            self._dropped += 1
            if self._dropped % 1000 == 1:
                logger.warning(
                    "AuditWriter queue full; dropped audit record (total=%d)",
                    self._dropped,
                )

    def _enqueue_sentinel(self) -> None:
        while True:
            try:
                self._queue.put_nowait(_SENTINEL)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._dropped += 1
                except queue.Empty:
                    pass

    def _run(self) -> None:
        pending_records = 0
        pending_bytes = 0
        last_flush = time.monotonic()
        last_fsync = time.monotonic()

        try:
            self._open_file()
            while True:
                timeout = max(self._config.flush_interval_seconds, 0.1)
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    item = None

                if item is _SENTINEL:
                    break

                if item is not None:
                    try:
                        written_bytes = self._write_record(item)
                        pending_records += 1
                        pending_bytes += written_bytes
                    except Exception:
                        self._write_errors += 1
                        logger.exception("AuditWriter failed to write record")

                now = time.monotonic()
                should_flush = (
                    pending_records > 0
                    and (
                        now - last_flush >= self._config.flush_interval_seconds
                        or pending_records >= self._config.flush_max_records
                        or pending_bytes >= self._config.flush_max_bytes
                    )
                )
                if should_flush:
                    self._flush()
                    pending_records = 0
                    pending_bytes = 0
                    last_flush = now

                if self._should_fsync(now, last_fsync):
                    self._fsync()
                    last_fsync = now

                if self._should_rotate(now):
                    self._flush()
                    self._close_file()
                    self._open_file()
                    pending_records = 0
                    pending_bytes = 0
                    last_flush = now

            self._drain_remaining()
            self._flush()
        finally:
            self._close_file()

    def _write_record(self, item: tuple[str, dict]) -> int:
        if self._file is None:
            self._open_file()
        _kind, payload = item
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        self._file.write(line)
        encoded_len = len(line.encode("utf-8"))
        self._file_bytes += encoded_len
        self._written += 1
        return encoded_len

    def _drain_remaining(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is _SENTINEL:
                continue
            try:
                self._write_record(item)
            except Exception:
                self._write_errors += 1
                logger.exception("AuditWriter failed to drain record")

    def _open_file(self) -> None:
        root = Path(self._config.root_dir)
        root.mkdir(parents=True, exist_ok=True)
        self._part += 1
        path = root / f"aggregate_bars_{self._session}_part{self._part:06d}.jsonl"
        self._file = open(path, "a", encoding="utf-8")
        self._file_path = path
        self._file_open_ts = time.monotonic()
        self._file_bytes = path.stat().st_size if path.exists() else 0

    def _close_file(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None

    def _flush(self) -> None:
        if self._file is None:
            return
        self._file.flush()
        self._flush_count += 1
        self._last_flush_ts = time.time()

    def _fsync(self) -> None:
        if self._file is None:
            return
        self._file.flush()
        os.fsync(self._file.fileno())

    def _should_fsync(self, now: float, last_fsync: float) -> bool:
        interval = self._config.fsync_interval_seconds
        return interval is not None and interval > 0 and now - last_fsync >= interval

    def _should_rotate(self, now: float) -> bool:
        rotate_interval = self._config.rotate_interval_seconds
        if rotate_interval > 0 and now - self._file_open_ts >= rotate_interval:
            return True
        rotate_bytes = self._config.rotate_max_bytes
        return rotate_bytes > 0 and self._file_bytes >= rotate_bytes


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v
