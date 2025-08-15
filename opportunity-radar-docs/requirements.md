# Requirements document

## Functional requirements (FR)

**FR1 – Symbol isolation**  
- The dashboard’s selected symbol drives every API call via `?symbol=SYMBOL`.  
- All tiles, trend score, momentum, RVOL, and “Latest Findings” update to the selected symbol.  
- Browser refresh preserves the last selected symbol (hash or localStorage).

**FR2 – Latest Findings stream**  
- Agents publish findings to DB; `/findings?symbol=…&limit=n` returns the most recent n rows for that symbol.  
- When DB is unavailable, `/findings` returns from in-mem buffer.  
- UI auto-refreshes findings every <=5s and prepends new rows without clearing the list.

**FR3 – Momentum & VWAP tiles**  
- `/signals_tf` computes `mom_bps_1m` and `mom_bps_5m` from fresh bars. Both update every request.  
- `/signals` exposes a single-TF “fast path” (1m) for legacy tiles.

**FR4 – Trend Score (p_up)**  
- TrendScoreAgent runs at `TS_INTERVAL` seconds per symbol and computes `p_up`, `p_1m`, `p_5m`, `p_15m`.  
- Findings are inserted with `label="trend_score"` and `details.p_up` in [0,1].  
- UI tile shows `p_up` and three sub-probs.

**FR5 – Run-now**  
- `/agents/run-now?names=list&symbol=…&insert=true` triggers immediate runs, returns per-agent results (or errors), and inserts findings when requested.

**FR6 – Diagnostics**  
- `/debug/state` and `/debug/features` return non-error payloads for any configured symbol.

**FR7 – Health**  
- `/health` returns `{status:"ok"}` and agent `last_run` ISO timestamps even when DB is down.

**FR8 – CSV export**  
- UI triggers a CSV download of findings for the current symbol and date window.

## Non-functional requirements (NFR)

- **Latency:** p50 < 150ms for `/signals*` and `/findings` at 3 symbols.  
- **Availability:** >=99.9% for read endpoints; graceful degradation when DB/LLM/Slack are unavailable.  
- **Safety:** input validation; finite numeric outputs; no unhandled exceptions leak stack traces in prod.  
- **Maintainability:** linters, type hints, clear module boundaries (state/bars/signals/scheduler/agents).  
- **Observability:** structured logs with agent name, symbol, and durations; `heartbeat()` status.

## Acceptance criteria (quick tests)

### Symbol wiring
```bash
curl -s "$BASE/signals_tf?symbol=BTC-USD" | jq '.mom_bps_1m,.px_vs_vwap_bps_1m'
curl -s "$BASE/signals_tf?symbol=ETH-USD" | jq '.mom_bps_1m,.px_vs_vwap_bps_1m'
# Values must differ over time; not a static copy.
```

### Findings visible
```bash
curl -s "$BASE/agents/run-now?names=rvol_spike,trend_score&symbol=BTC-USD&insert=true" | jq .
curl -s "$BASE/findings?symbol=BTC-USD&limit=5" | jq '.findings[0]'
# Expect the just-inserted agent in .findings[0].agent
```

### Momentum updates (1m & 5m)
```bash
for s in 1 2 3; do curl -s "$BASE/signals_tf?symbol=BTC-USD" | jq '.mom_bps_1m,.mom_bps_5m'; sleep 5; done
# 1m and 5m both change over subsequent calls.
```

### Trend score populated
```bash
curl -s "$BASE/findings?symbol=ETH-USD&limit=10" | jq '.findings[] | select(.agent=="trend_score") | .details.p_up' | head
# Values exist and are in [0,1].
```

### DB fallback
```
# Stop DB or set invalid DATABASE_URL; run an agent; verify /findings still returns
# a recent item (from in-mem buffer) and /db-health says {ok:false}.
```
