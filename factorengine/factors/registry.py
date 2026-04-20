"""
Factor Registry — central management for factor builder functions.

A factor builder is a callable that returns a *compiled* FactorGraph.
Builders are registered via the @register_factor decorator and discovered
automatically when load_all() / load_group() scans subpackages.

Directory layout:
    factorengine/factors/
    ├── registry.py          # this file
    ├── visualize.py         # graph visualization
    ├── okx_perp/            # OKX perpetual factors
    │   ├── __init__.py
    │   └── factor_bank.py   # @register_factor("okx_perp", "0001") ...
    ├── binance_perp/        # (future) Binance perpetual factors
    │   └── ...
    └── stock_cn/            # (future) A-share stock factors
        └── ...
"""

from __future__ import annotations

import importlib
import os
import pkgutil
from typing import Callable, Dict, List, Optional, Tuple

import fe_runtime as rt

FactorBuilder = Callable[[], rt.FactorGraph]

# key = (group, factor_id)
_GLOBAL_REGISTRY: Dict[Tuple[str, str], FactorBuilder] = {}


def register_factor(group: str, factor_id: str):
    """Decorator to register a factor builder function under a group.

    Args:
        group: Platform/strategy group (e.g. "okx_perp", "binance_perp", "stock_cn")
        factor_id: Unique factor ID within the group (e.g. "0001")

    Example:
        @register_factor("okx_perp", "0001")
        def build_factor_0001():
            g = rt.FactorGraph()
            ...
            g.compile()
            return g
    """
    def decorator(fn: FactorBuilder) -> FactorBuilder:
        key = (group, factor_id)
        if key in _GLOBAL_REGISTRY:
            raise ValueError(f"Factor ({group!r}, {factor_id!r}) already registered")
        _GLOBAL_REGISTRY[key] = fn
        return fn
    return decorator


def _scan_package(package_name: str):
    """Recursively import all modules under a package."""
    try:
        package = importlib.import_module(package_name)
    except ImportError:
        return
    pkg_path = getattr(package, "__path__", None)
    if pkg_path is None:
        return
    for importer, modname, ispkg in pkgutil.iter_modules(pkg_path):
        full_name = f"{package_name}.{modname}"
        if modname.startswith("_") or modname in ("registry", "visualize"):
            continue
        importlib.import_module(full_name)
        if ispkg:
            _scan_package(full_name)


class FactorRegistry:
    """Manages factor builders and produces compiled FactorGraph instances.

    Usage:
        reg = FactorRegistry()
        reg.load_all()                    # load all groups
        reg.load_group("okx_perp")        # load only one group

        graphs = reg.build_all()          # all factors across all loaded groups
        graphs = reg.build_group("okx_perp")  # only okx_perp factors
    """

    def __init__(self):
        self._builders: Dict[Tuple[str, str], FactorBuilder] = {}

    def load_all(self):
        """Auto-discover all factor modules in all subpackages."""
        _scan_package("factorengine.factors")
        self._builders.update(_GLOBAL_REGISTRY)

    def load_group(self, group: str):
        """Load only factor modules from a specific group subpackage."""
        _scan_package(f"factorengine.factors.{group}")
        for key, fn in _GLOBAL_REGISTRY.items():
            if key[0] == group:
                self._builders[key] = fn

    @property
    def groups(self) -> List[str]:
        """List all registered groups."""
        return sorted(set(g for g, _ in self._builders))

    @property
    def factor_ids(self) -> List[str]:
        """List all registered factor IDs (across all groups)."""
        return sorted(set(fid for _, fid in self._builders))

    def factor_ids_by_group(self, group: str) -> List[str]:
        """List factor IDs for a specific group."""
        return sorted(fid for g, fid in self._builders if g == group)

    def build(self, factor_id: str, group: Optional[str] = None) -> rt.FactorGraph:
        """Build a single factor graph.

        If group is None, searches all groups (raises if ambiguous).
        """
        if group is not None:
            key = (group, factor_id)
            if key not in self._builders:
                raise KeyError(f"Factor ({group!r}, {factor_id!r}) not registered")
            return self._builders[key]()

        matches = [(g, fid) for (g, fid) in self._builders if fid == factor_id]
        if len(matches) == 0:
            raise KeyError(f"Factor {factor_id!r} not registered in any group")
        if len(matches) > 1:
            groups = [g for g, _ in matches]
            raise KeyError(
                f"Factor {factor_id!r} exists in multiple groups: {groups}. "
                f"Specify group explicitly."
            )
        return self._builders[matches[0]]()

    def build_all(self) -> Dict[str, rt.FactorGraph]:
        """Build all registered factors. Returns {factor_id: compiled FactorGraph}."""
        return {fid: self._builders[(g, fid)]()
                for g, fid in sorted(self._builders)}

    def build_group(self, group: str) -> Dict[str, rt.FactorGraph]:
        """Build all factors in a specific group."""
        return {fid: self._builders[(group, fid)]()
                for g, fid in sorted(self._builders) if g == group}

    def __len__(self) -> int:
        return len(self._builders)

    def __contains__(self, factor_id: str) -> bool:
        return any(fid == factor_id for _, fid in self._builders)
