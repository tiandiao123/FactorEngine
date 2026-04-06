"""Minimal factor specification for the scheduler prototype."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

VALID_SOURCES = {"bars", "trades", "books"}


@dataclass(slots=True)
class FactorSpec:
    """Describe a factor's input source, window and compute function."""

    name: str
    source: str
    window: int
    compute_fn: Callable[[np.ndarray], float]

    def __post_init__(self):
        if self.source not in VALID_SOURCES:
            raise ValueError(f"Unsupported factor source: {self.source!r}")
        if self.window <= 0:
            raise ValueError(f"window must be > 0, got {self.window}")

