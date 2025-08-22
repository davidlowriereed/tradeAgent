# backend/startup_checks.py
from __future__ import annotations
import logging, os
from typing import Sequence
from .db import db_health, table_counts, fetch_latest_feature_snapshot

log = logging.getLogger("startup")

def _symbols_from_env() -> list[str]:
    raw = os.getenv("SYMBOL", os.getenv("SYMBOLS", ""))  # support both
    return [s.strip() for s in raw.split(",") if s.strip()]

async def verify_db_data(min_total_rows: int = 0) -> dict:
    """
    Verify DB is reachable and we can SELECT from expected tables.
    Does not fail on empty tables unless min_total_rows > 0.
    """
    health = await db_health()  # {'ok': True/False, 'mode': 'verified'|'degraded', ...}
    if not health.get("ok"):
        raise RuntimeError(f"DB health failed: {health}")

    symbols = _symbols_from_env()
    counts = await table_counts(symbols)
    total = sum(counts[t]["total"] for t in counts)
    ok = total >= min_total_rows
    log.info("DB verify: totals=%s by_symbol=%s", {k: v["total"] for k, v in counts.items()},
             {k: v["by_symbol"] for k, v in counts.items()})
    if not ok:
        # Don’t block boot—just return info (you can flip this to raise if you want hard gating)
        return {"ok": False, "reason": "no data yet", "health": health, "counts": counts}
    return {"ok": True, "health": health, "counts": counts}

async def warm_caches(max_symbols: int = 3) -> dict:
    """
    Optionally prefetch the last feature snapshot for a few symbols.
    Non-fatal if nothing exists yet.
    """
    symbols = _symbols_from_env()[:max_symbols]
    snapshots = []
    for s in symbols:
        snap = await fetch_latest_feature_snapshot(s)
        if snap:
            snapshots.append(snap)
    return {"symbols": symbols, "snapshots": snapshots}
