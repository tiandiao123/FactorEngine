"""Make tests/kernel/reference/ importable so test modules can `from ts_ops import ...`."""

import sys
from pathlib import Path

_REF_DIR = str(Path(__file__).resolve().parent / "reference")
if _REF_DIR not in sys.path:
    sys.path.insert(0, _REF_DIR)
