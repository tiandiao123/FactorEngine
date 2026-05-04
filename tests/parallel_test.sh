#!/usr/bin/env bash
#
# Parallel test runner for FactorEngine.
# Uses pytest-xdist to distribute tests across CPU cores.
#
# Usage:
#   ./tests/parallel_test.sh            # auto-detect cores, run all tests
#   ./tests/parallel_test.sh -n 2       # use 2 workers
#   ./tests/parallel_test.sh -k kurt    # filter tests by keyword
#   ./tests/parallel_test.sh --serial   # fallback to sequential mode
#
# Prerequisites:
#   pip install pytest-xdist
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

WORKERS="auto"
SERIAL=false
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -n)
            WORKERS="$2"
            shift 2
            ;;
        --serial)
            SERIAL=true
            shift
            ;;
        *)
            EXTRA_ARGS+=("$1")
            shift
            ;;
    esac
done

if ! python -c "import xdist" 2>/dev/null; then
    echo "[warn] pytest-xdist not installed, falling back to sequential mode"
    echo "       Install with: pip install pytest-xdist"
    SERIAL=true
fi

echo "=============================================="
echo "  FactorEngine Test Suite"
echo "=============================================="
echo "  Project root : $PROJECT_ROOT"
echo "  CPU cores    : $(nproc)"

if $SERIAL; then
    echo "  Mode         : sequential"
    echo "=============================================="
    python -m pytest tests/ -v "${EXTRA_ARGS[@]}"
else
    echo "  Mode         : parallel (workers=$WORKERS)"
    echo "=============================================="
    python -m pytest tests/ -v -n "$WORKERS" "${EXTRA_ARGS[@]}"
fi
