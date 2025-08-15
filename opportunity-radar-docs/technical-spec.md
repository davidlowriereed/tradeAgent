# Technical specification

## Architecture (high level)

**Frontend:** Static HTML/JS dashboard (single page) that calls backend JSON endpoints with `?symbol=` and auto-refreshes every 5–10s. “Run agents now” triggers a backend kick.

**Backend (FastAPI + Uvicorn):**
- **State layer (`backend/state.py`):** in-memory deques per symbol for trades; caches for best quotes & last price; posture cache; recent in-memory findings buffer.
- **Bar/feature layer (`backend/bars.py`):** builds 1m/5m/15m bars from trade cache; computes ATR, momentum (bps), price vs VWAP (bps), RVOL ratios.
- **Signals layer (`backend/signals.py`):** assembles tile data from bars + quotes; multi-TF endpoint payloads.
- **Scheduler (`backend/scheduler.py`):** async loop that runs agents by interval, writes findings to DB (or in-mem fallback), updates posture state.
- **Agents (`backend/agents/*.py`):** each returns an optional finding dict. TrendScore computes p_up from simple features and applies persistence rules to posture.
- **DB layer (`backend/db.py`):** asyncpg connection helpers, `insert_finding`, `pg_exec/pg_fetch*`.
- **Services:** Slack webhook client (uses httpx), macro watcher (optional).

**Database (PostgreSQL):**
```
findings(id, ts_utc, agent, symbol, score, label, details jsonb)
position_state(symbol pk, status, qty, avg_price, updated_at, last_action, last_conf)
```

**Deployment:** Containerized, K8s/DO App Platform. Readiness = `/health`. Environment variables configure symbols, thresholds, LLM, Slack, DB.

## Key endpoints (selection)

| Endpoint        | Method | Params                        | Purpose                                  | Returns (shape) |
|-----------------|--------|-------------------------------|------------------------------------------|-----------------|
| `/health`       | GET    | —                             | Liveness; trades cached; last run per agent | `{status, symbols[], trades_cached{sym:n}, agents{name:{status,last_run}}}` |
| `/db-health`    | GET    | —                             | DB connectivity                           | `{ok: bool, error?: string}` |
| `/agents`       | GET    | —                             | Agent registry + last run                 | `{agents:[{agent, last_run}]}` |
| `/agents/run-now` | POST | `names=csv, symbol, insert=true` | Force a run                             | `{ok, ran:[name], results:[{agent, error?}]}` |
| `/findings`     | GET    | `symbol, limit`               | Latest findings                           | `{findings:[{ts_utc,agent,symbol,score,label,details}...]}` |
| `/signals`      | GET    | `symbol`                      | Legacy tile signals                       | `{mom1_bps, mom5_bps, rvol_vs_recent, px_vs_vwap_bps, best_bid, best_ask, last_price, trades_cached}` |
| `/signals_tf`   | GET    | `symbol`                      | Bar-based multi-TF                        | `{mom_bps_1m/5m/15m, px_vs_vwap_bps_*, rvol_*, atr_*}` |
| `/debug/state`  | GET    | `symbol`                      | Raw state                                 | `{symbol, trades_cached, last_trade[], last_trade_age_s, bars_1m_count, bars_1m_tail[]}` |
| `/debug/features` | GET  | `symbol`                      | Computed features                         | `{symbol, bars:{1m,5m,15m}, mom_bps_1m, mom_bps_5m, px_vs_vwap_bps_1m, atr_1m ...}` |

## Data contracts

### Finding
```json
{
  "ts_utc": "2025-08-14T19:27:18.854Z",
  "agent": "rvol_spike",
  "symbol": "BTC-USD",
  "score": 7.16,
  "label": "rvol_spike",
  "details": { "rvol": 7.16, "volume_5m": 533.7, "best_bid": 100.52, "best_ask": 100.59 }
}
```

### Signals (legacy)
Numbers are finite floats (no NaN/Inf) or null for quotes:
```json
{
  "mom1_bps": 12.3, "mom5_bps": -8.2,
  "rvol_vs_recent": 1.45,
  "px_vs_vwap_bps": 23.1,
  "best_bid": 100.52, "best_ask": 100.59,
  "last_price": 100.57,
  "trades_cached": 482
}
```

### Signals TF
```json
{
  "mom_bps_1m": -16.4, "mom_bps_5m": -19.5, "mom_bps_15m": 0.0,
  "px_vs_vwap_bps_1m": 19.4, "px_vs_vwap_bps_5m": 19.4, "px_vs_vwap_bps_15m": 19.4,
  "rvol_1m": 0.92, "rvol_5m": 1.31, "rvol_15m": 0.74,
  "atr_1m": 0.53, "atr_5m": 0.62, "atr_15m": 0.0
}
```

## Configuration (env)

- **Symbols:** `SYMBOL="BTC-USD,ETH-USD,ADA-USD"`
- **DB:** `DATABASE_URL=postgres://...`
- **Slack:** `ALERT_WEBHOOK_URL`, `SLACK_ANALYSIS_ONLY=true|false`, `ALERT_VERBOSE=true|false`
- **LLM:** `LLM_ENABLE`, `OPENAI_MODEL`, `OPENAI_API_KEY`, `LLM_MIN_INTERVAL`, `LLM_ALERT_MIN_CONF`, `LLM_ANALYST_MIN_SCORE`
- **Trend/posture:** `TS_INTERVAL`, `TS_ENTRY`, `TS_EXIT`, `TS_PERSIST`, `TS_WEIGHTS`, `TS_MTF_WEIGHTS`
- **Features:** `FEATURE_BARS`, `FEATURE_NEW_TREND`, `FEATURE_REVERSAL`, `FEATURE_LIQUIDITY`
- **Macro watcher:** `MACRO_WINDOWS`, `MACRO_WINDOW_MINUTES`, `MACRO_RVOL_MIN`, `MACRO_INTERVAL_SEC`

## Dependencies

Python (3.11):  
`fastapi, uvicorn[standard], httpx, asyncpg, psycopg2-binary, python-dateutil, pydantic (v2), numpy (optional), orjson (optional)`

*(If you prefer aiohttp, add it explicitly; current Slack client uses httpx.)*

## Container run

```
uvicorn backend.main:app --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips='*'
```

## Operational concerns

- **Readiness:** `/health` must not import heavy modules at request time; it reads last-run timestamps from scheduler memory.
- **Fallbacks:** If DB insert fails, finding is appended to in-memory `RECENT_FINDINGS` (exposed via `/findings` when DB is down).
- **Numeric hygiene:** all tile numbers are finite (no NaN/Inf); quotes may be null.
- **Backpressure:** trades deque bounded to 50k per symbol.
- **Observability:** per-agent `heartbeat(name, status)` and timestamps in `/agents` & `/health`.
