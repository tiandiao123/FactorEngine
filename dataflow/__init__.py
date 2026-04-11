"""Dataflow top-level package.

Re-exports public API from the livetrading sub-package so that existing
``from dataflow.events import ...`` / ``from dataflow.manager import ...``
imports keep working after the move to dataflow/livetrading/.
"""

import sys

from .livetrading import *  # noqa: F401,F403
from .livetrading import __all__  # noqa: F401

# Expose sub-packages as ``dataflow.<mod>`` so that old-style
# ``from dataflow.events import X`` / ``from dataflow.okx.symbols import Y``
# imports keep resolving after the move to dataflow/livetrading/.
from .livetrading import bars, books, cache, collector, events, manager, okx, trades  # noqa: F401
from .livetrading.okx import symbols as _okx_symbols  # noqa: F401

_submodule_aliases = {
    "dataflow.bars": bars,
    "dataflow.books": books,
    "dataflow.cache": cache,
    "dataflow.collector": collector,
    "dataflow.events": events,
    "dataflow.manager": manager,
    "dataflow.okx": okx,
    "dataflow.okx.symbols": _okx_symbols,
    "dataflow.trades": trades,
}
for _alias, _mod in _submodule_aliases.items():
    sys.modules.setdefault(_alias, _mod)
