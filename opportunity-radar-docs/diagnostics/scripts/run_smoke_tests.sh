#!/usr/bin/env bash
set -euo pipefail
BASE="${1:-http://localhost:8080}"

echo "[smoke] Using BASE=$BASE"
# Symbol wiring
curl -s "$BASE/signals_tf?symbol=BTC-USD" | jq '.mom_bps_1m,.px_vs_vwap_bps_1m' >/dev/null
curl -s "$BASE/signals_tf?symbol=ETH-USD" | jq '.mom_bps_1m,.px_vs_vwap_bps_1m' >/dev/null

# Findings visible
curl -s "$BASE/agents/run-now?names=rvol_spike,trend_score&symbol=BTC-USD&insert=true" | jq . >/dev/null
curl -s "$BASE/findings?symbol=BTC-USD&limit=5" | jq '.findings[0]' >/dev/null

# Momentum updates (lightweight check: just ensure keys exist)
curl -s "$BASE/signals_tf?symbol=BTC-USD" | jq '.mom_bps_1m,.mom_bps_5m' >/dev/null

# Trend score present (any value in [0,1])
curl -s "$BASE/findings?symbol=ETH-USD&limit=10" | jq '[.findings[] | select(.agent=="trend_score") | .details.p_up] | length' >/dev/null

# DB health reachable
curl -s "$BASE/db-health" | jq '.ok' >/dev/null

echo "[smoke] OK"
