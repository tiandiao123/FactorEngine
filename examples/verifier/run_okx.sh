#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

python "${SCRIPT_DIR}/verify_okx_audit_vs_candles.py" \
  --audit-file "${REPO_ROOT}/examples/engine/timeBuckets/data/audit/okx_dynamic_exchange_time_demo/aggregate_bars_20260528_130133_part000001.jsonl" \
  --symbols BTC-USDT-SWAP,ETH-USDT-SWAP,SOL-USDT-SWAP,XRP-USDT-SWAP,DOGE-USDT-SWAP,ADA-USDT-SWAP,LINK-USDT-SWAP,AVAX-USDT-SWAP,DOT-USDT-SWAP,NEAR-USDT-SWAP \
  --start "2026-05-28T13:06:00Z" \
  --end "2026-05-28T13:10:00Z" \
  --price-rtol 0.0001 \
  --volume-rtol 0.001 \
  --max-mismatches 3
