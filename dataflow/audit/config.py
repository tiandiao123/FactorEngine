"""Configuration for asynchronous audit logging."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class AuditConfig:
    """Runtime configuration for aggregate audit logging.

    The first implementation intentionally records only ClockDriver aggregate
    bars.  It does not persist raw websocket messages or per-trade events.
    """

    enabled: bool = False
    root_dir: str = "data/audit"
    queue_size: int = 100_000
    flush_interval_seconds: float = 1.0
    flush_max_records: int = 10_000
    flush_max_bytes: int = 4 * 1024 * 1024
    rotate_interval_seconds: int = 3600
    rotate_max_bytes: int = 512 * 1024 * 1024
    fsync_interval_seconds: float | None = None
