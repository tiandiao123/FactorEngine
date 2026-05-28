#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

python "${SCRIPT_DIR}/verify_binance_audit_vs_kline.py" \
  --audit-file "${REPO_ROOT}/examples/engine/timeBuckets/data/audit/binance_exchange_time_demo/aggregate_bars_20260528_081536_part000001.jsonl" \
  --symbols BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT,DOGE-USDT,ADA-USDT,LINK-USDT,AVAX-USDT,DOT-USDT,NEAR-USDT,LTC-USDT,BCH-USDT,ETC-USDT,ARB-USDT,OP-USDT,SUI-USDT,HYPE-USDT,ZEC-USDT \
  --start "2026-05-28T08:16:00Z" \
  --end "2026-05-28T08:33:00Z" \
  --reference aggtrades \
  --price-rtol 0.0001 \
  --volume-rtol 0.001 \
  --max-mismatches 3
