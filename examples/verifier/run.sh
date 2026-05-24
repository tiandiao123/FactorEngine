#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

python "${SCRIPT_DIR}/verify_binance_audit_vs_kline.py" \
  --audit-file "${REPO_ROOT}/examples/engine/data/audit/binance_trades_demo/aggregate_bars_20260524_101827_part000001.jsonl" \
  --symbols BTC-USDT,ETH-USDT,SOL-USDT \
  --start "2026-05-24T10:19:00Z" \
  --end "2026-05-24T10:24:00Z" \
  --audit-ts-is-end \
  --price-rtol 0.001 \
  --volume-rtol 0.02 \
  --max-mismatches 3
