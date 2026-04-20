"""Make tests/kernel/reference/ importable for benchmark scripts."""

import sys
from pathlib import Path

_REF_DIR = str(Path(__file__).resolve().parents[1] / "reference")
if _REF_DIR not in sys.path:
    sys.path.insert(0, _REF_DIR)
