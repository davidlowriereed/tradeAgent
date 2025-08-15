# What has been built so far

## Working
- Backend boots and serves `/health`, `/agents`, `/signals_tf`, `/debug/state`, `/debug/features`.
- Trade cache fills; bars build for 1m/5m/15m; features (momentum, VWAP deviation, ATR) compute and are finite.
- Agents framework runs on schedule; `rvol_spike` and `cvd_divergence` execute and can emit findings.
- TrendScore agent implemented to use `compute_signals()` (no private imports); posture guard scaffolding present.
- `/health` summarizes agent `last_run` without importing scheduler state at request time.
- Frontend dashboard renders tiles, “Latest Findings” list, top-of-book, trades cached, basic agent health.

## Partially working / open issues
- **Symbol selection in UI** – selection doesn’t consistently propagate to all fetches. Some tiles appear static across symbols.  
  Likely cause: one or more fetches omit `?symbol=${current}` or state is not synchronized before refresh.
- **“Latest Findings” not updating** – agents run but list doesn’t reflect new rows promptly.  
  Causes: DB inserts failing (e.g., connectivity) and UI not falling back to in-mem results; fetch interval/debouncing; response filtered for wrong symbol.
- **Momentum (1m) tile** – previously only 5m updated.  
  Root cause was building/reading bars only at 5m for that tile; fix is to call `momentum_bps` on the 1m bar set in `/signals_tf` and surface it in the UI.
- **Trend Score empty** – originally due to TrendScore importing non-existent private helpers (`_dcvd`/`_rvol`/`_mom_bps`).  
  Now fixed to use `compute_signals()`, but verify it’s in the agent registry and running per symbol with inserts enabled.
- **Slack dependency** – import error (`aiohttp`) surfaced. Slack service now uses **httpx** to avoid extra runtime deps; confirm httpx is in requirements.
- **DB health** – `/db-health` returned `no_connection` earlier; when DB is down, findings should still appear via `RECENT_FINDINGS` fallback.
- **State globals** – historical issues with `_best_quotes` and duplicate `best_px` definitions are resolved by centralizing in `state.py`.

## Housekeeping done
- Defensive numeric casting (`_num`) across signals/TF endpoints (never NaN/Inf).
- RVOL window corrected to (5 vs 20) with safe division: `return (v_recent / v_base) if v_base else 0.0`
- Scheduler’s agent list unified; `list_agents_last_run()` used by `/health`.
- In-memory fallback for findings when DB insert fails.

## Recommended immediate next steps
1. **Front-end symbol plumbing audit**  
   Ensure every fetch appends `?symbol=${selected}`; centralize `getSymbol()`; store in hash or localStorage; re-render on change. Add a debug badge that echoes the symbol each tile used.
2. **Findings pipeline**  
   Log DB insert errors; on UI, if `/findings` is empty but `/agents/run-now` shows success, hit a `/findings?source=fallback` path or let the server return merged (DB + in-mem) results by default.
3. **TrendScore visibility**  
   Confirm it’s in `AGENTS`, runs every `TS_INTERVAL`, and inserts (`insert_finding`) with `details.p_up`. Add tile call to a dedicated endpoint (or reuse `/findings` with last `trend_score`) for the top-left card.
4. **Requirements file**  
   Pin: `fastapi~=0.111`, `uvicorn[standard]~=0.30`, `httpx~=0.27`, `asyncpg~=0.29`, `psycopg2-binary~=2.9`, `python-dateutil~=2.9`, `pydantic~=2.8`, `orjson~=3.10` (optional).
5. **Playbooks**  
   Add a Make target: `make diag` to run the import/symbol auditor and curl smoke tests from the Requirements’ acceptance section.
