# Product description

**Name:** Opportunity Radar  
**One-liner:** A real-time, agent-driven market radar for crypto that surfaces actionable signals (momentum, RVOL, VWAP skews, trend probability) across multiple symbols with automated alerting and lightweight posture logic.

## Who it’s for

- **Discretionary traders** who want timely, high-signal alerts without staring at charts.
- **Quants/PMs** who want a pluggable signal bus and a thin UI for triage.
- **Ops/Dev** who need health & diagnostics endpoints for reliability.

## Problems it solves

- Consolidates heterogeneous micro-signals into a normalized “finding” stream.
- Reduces noise via configurable thresholds, persistence rules, and multi-TF features.
- Exposes fast diagnostics (`/debug/*`) so data issues are debuggable in seconds.

## Key outcomes

- <1s p50 API latency for tiles/diagnostics.
- Agents run at fixed cadences (5–60s) and publish findings with consistent schemas.
- Real-time tiles per symbol: price vs VWAP (bps), momentum (1m/5m), RVOL (5 vs 20), trend score probability, last top-of-book, and “Latest Findings”.

## Core features

- **Multi-symbol:** BTC-USD, ETH-USD, ADA-USD (extensible via env).
- **Agent framework:** `rvol_spike`, `cvd_divergence`, `trend_score`, `llm_analyst` (+ optional reversal agents).
- **Finding stream:** normalized rows `{ts, agent, symbol, score, label, details JSON}`.
- **Tiles API:** `/signals` (legacy) and `/signals_tf` (bar-based).
- **Debug API:** `/debug/state`, `/debug/features` for direct introspection.
- **Health:** `/health`, `/db-health`, `/agents`.
- **Alerting:** optional Slack webhook with posting policy.
- **CSV export:** findings per symbol (download from UI).
