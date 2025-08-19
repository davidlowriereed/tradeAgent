# backend/db.py
from __future__ import annotations
import asyncio, json, os, ssl, base64
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
import json
import asyncpg
from asyncpg.types import Json

from .config import DATABASE_URL
from .state import RECENT_FINDINGS

# ----- heartbeat (used by /health) -----
HEARTBEATS: dict[str, dict] = {}
async def heartbeat(name: str, status: str = "ok") -> None:
    HEARTBEATS[name] = {"status": status, "last_run": datetime.now(timezone.utc).isoformat()}
heartbeats = HEARTBEATS  # compat alias

_pool = None  # your global pool

# ----- pool + error state -----
POOL: Optional["asyncpg.pool.Pool"] = None
_last_db_error: Optional[str] = None
_tls_mode: str = "unknown"  # "verify-ca" | "require" | "insecure"

try:
    import asyncpg  # type: ignore
    from asyncpg.types import Json
except Exception:
    asyncpg = None
    Json = None

_POOL = None
_LOCK = asyncio.Lock()

async def fetch_recent_findings(symbol: Optional[str], limit: int = 20) -> list[dict]:
    """Read latest findings (jsonb 'details' comes back as a dict from asyncpg)."""
    pool = await connect_pool()
    out: list[dict] = []
    async with pool.acquire() as conn:
        if symbol:
            rows = await conn.fetch(
                """SELECT ts_utc, agent, symbol, score, label, details
                   FROM findings
                   WHERE symbol=$1
                   ORDER BY ts_utc DESC
                   LIMIT $2""",
                symbol, int(limit)
            )
        else:
            rows = await conn.fetch(
                """SELECT ts_utc, agent, symbol, score, label, details
                   FROM findings
                   ORDER BY ts_utc DESC
                   LIMIT $1""",
                int(limit)
            )
        for r in rows:
            out.append({
                "ts_utc": r["ts_utc"].isoformat() if r["ts_utc"] else None,
                "agent":  r["agent"],
                "symbol": r["symbol"],
                "score":  float(r["score"]),
                "label":  r["label"],
                "details": r["details"],   # already a dict (jsonb)
            })
    return out


async def insert_features_1m(symbol: str, ts_utc, feat: dict) -> bool:
    """Upsert 1m features snapshot (dict-based API)."""
    pool = await connect_pool()
    cols = [
        "mom_bps_1m","mom_bps_5m","mom_bps_15m",
        "px_vs_vwap_bps_1m","px_vs_vwap_bps_5m","px_vs_vwap_bps_15m",
        "rvol_1m","atr_1m","schema_version"
    ]
    vals = [
        feat.get("mom_bps_1m"), feat.get("mom_bps_5m"), feat.get("mom_bps_15m"),
        feat.get("px_vs_vwap_bps_1m"), feat.get("px_vs_vwap_bps_5m"), feat.get("px_vs_vwap_bps_15m"),
        feat.get("rvol_1m"), feat.get("atr_1m"), int(feat.get("schema_version", 1)),
    ]
    placeholders = ",".join(f"${i}" for i in range(3, 3+len(vals)))
    set_clause   = ",".join(f"{c}=EXCLUDED.{c}" for c in cols)
    async with pool.acquire() as conn:
        await conn.execute(
            f"""INSERT INTO features_1m(symbol, ts_utc, {",".join(cols)})
                VALUES($1,$2,{placeholders})
                ON CONFLICT(symbol, ts_utc) DO UPDATE SET {set_clause}""",
            symbol, ts_utc, *vals
        )
    return True


async def insert_bar_1m(symbol: str, ts_utc, o,h,l,c,v,vwap=None,trades=None) -> bool:
    """Upsert 1m OHLCV bar."""
    pool = await connect_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO bars_1m(symbol, ts_utc, o,h,l,c,v,vwap,trades)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
               ON CONFLICT(symbol, ts_utc) DO UPDATE SET
                 o=EXCLUDED.o, h=EXCLUDED.h, l=EXCLUDED.l, c=EXCLUDED.c,
                 v=EXCLUDED.v, vwap=EXCLUDED.vwap, trades=EXCLUDED.trades""",
            symbol, ts_utc, o,h,l,c,v,vwap,trades
        )
    return True


async def db_health():
    """Standardized health payload used by /db-health."""
    try:
        pool = await connect_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        mode = "insecure" if _ssl_ctx() is False else "verified"
        return {"ok": True, "mode": mode}
    except Exception as e:
        return {"ok": False, "mode": "unknown", "error": f"{type(e).__name__}: {e}"}


# optional alias so your app code can call ensure_schema_v2()
async def ensure_schema_v2():
    return await ensure_schema()


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL not set")
    return url

def _ssl_ctx():
    if os.getenv("DB_TLS_INSECURE", "0").lower() in ("1","true","yes"):
        return False
    ca_b64 = os.getenv("DATABASE_CA_CERT_B64")
    ca_pem = os.getenv("DATABASE_CA_CERT")
    if ca_b64 or ca_pem:
        pem = (base64.b64decode(ca_b64).decode("utf-8") if ca_b64 else ca_pem)
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(cadata=pem)
        return ctx
    return ssl.create_default_context()

async def connect_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    async with _LOCK:
        if _POOL is not None:
            return _POOL
        if asyncpg is None:
            raise RuntimeError("asyncpg not installed")
        ssl_ctx = _ssl_ctx()
        _POOL = await asyncpg.create_pool(dsn=_dsn(), ssl=ssl_ctx)
        await ensure_schema()
        return _POOL

async def ensure_schema():
    pool = await connect_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS findings (
            id       BIGSERIAL PRIMARY KEY,
            agent    TEXT NOT NULL,
            symbol   TEXT NOT NULL,
            ts_utc   TIMESTAMPTZ NOT NULL DEFAULT now(),
            score    DOUBLE PRECISION NOT NULL,
            label    TEXT NOT NULL,
            details  JSONB NOT NULL DEFAULT '{}'::jsonb
        );
        CREATE INDEX IF NOT EXISTS idx_findings_symbol_ts ON findings(symbol, ts_utc DESC);
        """)

async def insert_finding_row(rec: Dict[str, Any]) -> int:
    pool = await connect_pool()
    async with pool.acquire() as conn:
        details_obj = rec.get("details") or {}
        if not isinstance(details_obj, (dict, list)):
            try:
                details_obj = json.loads(str(details_obj))
            except Exception:
                details_obj = {"raw": str(details_obj)}
        q = """
            INSERT INTO findings (agent, symbol, score, label, details)
            VALUES ($1,$2,$3,$4,$5)
            RETURNING id
        """
        return await conn.fetchval(q,
            str(rec.get("agent","")),
            str(rec.get("symbol","")),
            float(rec.get("score") or 0.0),
            str(rec.get("label") or ""),
            Json(details_obj) if Json else json.dumps(details_obj)
        )
