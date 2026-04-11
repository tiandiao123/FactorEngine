"""Result container for one factor evaluation tick."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class FactorSnapshot:
    """A minimal snapshot of factor values produced at one evaluation tick."""

    tick_id: int
    ts_eval_ms: int
    duration_ms: float
    values: dict[str, dict[str, float]] = field(default_factory=dict)

