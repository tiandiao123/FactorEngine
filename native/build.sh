#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${SCRIPT_DIR}/build"

PYBIND11_DIR="$(python3 -m pybind11 --cmakedir 2>/dev/null)" || {
    echo "pybind11 not found. Install it:  pip install pybind11"
    exit 1
}

echo "=== fe_ops build ==="
echo "  Python:    $(python3 --version 2>&1)"
echo "  pybind11:  ${PYBIND11_DIR}"
echo "  Build dir: ${BUILD_DIR}"

rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

cmake .. \
    -Dpybind11_DIR="${PYBIND11_DIR}" \
    -DCMAKE_BUILD_TYPE=Release

make -j"$(nproc)"

echo ""
echo "=== Done ==="
ls -lh "${BUILD_DIR}"/fe_ops*.so
# echo ""
# echo "Run tests:     cd $(dirname "${SCRIPT_DIR}") && python tests/kernel/test_ops_alignment.py"
# echo "Run benchmark: cd $(dirname "${SCRIPT_DIR}") && python tests/kernel/benchmark/bench_ops.py"
