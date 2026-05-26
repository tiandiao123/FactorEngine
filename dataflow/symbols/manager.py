"""Versioned symbol universe diff and staging logic."""

from __future__ import annotations

import threading
import time

from .models import SymbolDiff, SymbolUniverse


class SymbolManager:
    """Manage active symbols and pending universe diffs.

    The manager never mutates workers or runtime state directly. It only
    validates full universe snapshots, computes diffs, and stages a pending
    diff for a ClockDriver post-flush commit.
    """

    def __init__(
        self,
        initial_symbols: list[str] | tuple[str, ...],
        *,
        exchange: str,
        version: str = "initial",
        allow_empty: bool = False,
        max_change_ratio: float | None = None,
        max_added_per_commit: int | None = None,
        max_removed_per_commit: int | None = None,
    ):
        self._exchange = exchange
        self._active = frozenset(_normalize_symbols(initial_symbols))
        if not self._active and not allow_empty:
            raise ValueError("initial_symbols cannot be empty unless allow_empty=True")
        self._version = version
        self._pending: SymbolDiff | None = None
        self._allow_empty = allow_empty
        self._max_change_ratio = max_change_ratio
        self._max_added_per_commit = max_added_per_commit
        self._max_removed_per_commit = max_removed_per_commit
        self._lock = threading.Lock()
        now_ms = int(time.time() * 1000)
        self._last_stage_ts_ms = 0
        self._last_commit_ts_ms = now_ms
        self._rejected_updates = 0

    @property
    def active_symbols(self) -> frozenset[str]:
        with self._lock:
            return self._active

    @property
    def version(self) -> str:
        with self._lock:
            return self._version

    @property
    def pending_diff(self) -> SymbolDiff | None:
        with self._lock:
            return self._pending

    def stage_universe(self, universe: SymbolUniverse) -> SymbolDiff | None:
        """Validate a full universe and stage its diff for later commit."""
        target = frozenset(_normalize_symbols(universe.symbols))
        with self._lock:
            self._validate_universe(universe, target)
            added = target - self._active
            removed = self._active - target
            unchanged = self._active & target
            diff = SymbolDiff(
                version=universe.version,
                added=frozenset(added),
                removed=frozenset(removed),
                unchanged=frozenset(unchanged),
                target=frozenset(target),
                reason=universe.metadata.get("reason") if universe.metadata else None,
            )
            if diff.is_empty:
                self._version = universe.version
                self._pending = None
                self._last_stage_ts_ms = int(time.time() * 1000)
                return None
            self._validate_change_size(diff)
            self._pending = diff
            self._last_stage_ts_ms = int(time.time() * 1000)
            return diff

    def update_symbols(
        self,
        symbols: list[str] | tuple[str, ...],
        *,
        version: str,
        metadata: dict | None = None,
        allow_empty: bool = False,
    ) -> SymbolDiff | None:
        universe = SymbolUniverse(
            version=version,
            exchange=self._exchange,
            symbols=tuple(symbols),
            metadata=metadata or {},
            allow_empty=allow_empty,
        )
        return self.stage_universe(universe)

    def commit_pending(self) -> SymbolDiff | None:
        """Commit the pending diff and return it."""
        with self._lock:
            diff = self._pending
            if diff is None:
                return None
            self._active = diff.target
            self._version = diff.version
            self._pending = None
            self._last_commit_ts_ms = int(time.time() * 1000)
            return diff

    def stats(self) -> dict:
        with self._lock:
            return {
                "version": self._version,
                "active_count": len(self._active),
                "pending_added": len(self._pending.added) if self._pending else 0,
                "pending_removed": len(self._pending.removed) if self._pending else 0,
                "last_stage_ts": self._last_stage_ts_ms,
                "last_commit_ts": self._last_commit_ts_ms,
                "rejected_updates": self._rejected_updates,
            }

    def _validate_universe(
        self,
        universe: SymbolUniverse,
        target: frozenset[str],
    ) -> None:
        if universe.exchange != self._exchange:
            self._rejected_updates += 1
            raise ValueError(
                f"Universe exchange {universe.exchange!r} does not match "
                f"manager exchange {self._exchange!r}"
            )
        if universe.version <= self._version:
            self._rejected_updates += 1
            raise ValueError(
                f"Universe version {universe.version!r} must be newer than "
                f"current version {self._version!r}"
            )
        if not target and not (universe.allow_empty or self._allow_empty):
            self._rejected_updates += 1
            raise ValueError("Empty symbol universe rejected by default")

    def _validate_change_size(self, diff: SymbolDiff) -> None:
        added_count = len(diff.added)
        removed_count = len(diff.removed)
        if self._max_added_per_commit is not None and added_count > self._max_added_per_commit:
            self._rejected_updates += 1
            raise ValueError(
                f"Added symbols {added_count} exceeds limit {self._max_added_per_commit}"
            )
        if self._max_removed_per_commit is not None and removed_count > self._max_removed_per_commit:
            self._rejected_updates += 1
            raise ValueError(
                f"Removed symbols {removed_count} exceeds limit {self._max_removed_per_commit}"
            )
        if self._max_change_ratio is not None and self._active:
            changed = added_count + removed_count
            ratio = changed / len(self._active)
            if ratio > self._max_change_ratio:
                self._rejected_updates += 1
                raise ValueError(
                    f"Change ratio {ratio:.3f} exceeds limit {self._max_change_ratio:.3f}"
                )


def _normalize_symbols(symbols: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    normalized: list[str] = []
    for symbol in symbols:
        sym = symbol.strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        normalized.append(sym)
    return tuple(sorted(normalized))
