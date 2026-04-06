"""Backward-compatible exports for the bar dataflow implementation."""

from .bars.aggregator import BarAggregator
from .bars.worker import BarDataflowWorker as Dataflow
from .cache import BAR_NUM_FIELDS as NUM_FIELDS

__all__ = ["NUM_FIELDS", "BarAggregator", "Dataflow"]
