# backend/db.py
from __future__ import annotations
import asyncio, json, os, ssl, base64
from typing import Optional, Dict, Any

DB_CONNECT_TIMEOUT_SEC = float(os.getenv('DB_CONNECT_TIMEOUT_SEC', '5'))
MIGRATIONS_LOCK_KEY = 2147483601

from datetime import datetime, timezone

try:
    import asyncpg  # type: ignore
    from asyncpg.types import Json
except Exception:
    asyncpg = None
    Json = None

# -------------------- Heartbeat --------------------
HEARTBEATS: dict[str, dict] = {}
async def heartbeat(name: str, status: str = "ok") -> None:
    HEARTBEATS[name] = {"status": status, "last_run": datetime.now(timezone.utc).isoformat()}
heartbeats = HEARTBEATS  # compat alias

# -------------------- Pool --------------------
_POOL: Optional["asyncpg.pool.Pool"] = None
_LOCK = asyncio.Lock()


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

# inside connect_pool()
async with _LOCK:
    if _POOL is not None:
        return _POOL
    if asyncpg is None:
        raise RuntimeError("asyncpg not installed")
    _POOL = await asyncpg.create_pool(
        dsn=_dsn(),
        ssl=_ssl_ctx(),
        timeout=DB_CONNECT_TIMEOUT_SEC,
        command_timeout=DB_CONNECT_TIMEOUT_SEC,
        min_size=1,
        max_size=5,
    )
    # DO NOT run ensure_schema() here (boot orchestrator will)
    return _POOL

# -------------------- Health --------------------
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

# -------------------- Schema (findings only) --------------------
async def ensure_schema():
    """Create findings table (jsonb details). Other tables assumed to exist."""
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

# Backward-compat alias expected by app.py
async def ensure_schema_v2():
    # -------------------- Findings --------------------
async def insert_finding_row(row: dict) -> None:
    ts = row.get("ts_utc")
    details = row.get("details") or {}
    if not isinstance(details, str):
        details = json.dumps(details, separators=(",", ":"))
    pool = await connect_pool()                 # <-- ensure pool defined here
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO findings (agent, symbol, ts_utc, score, label, details)
            VALUES ($1, $2, COALESCE($3, NOW()), $4, $5, $6::jsonb)
            """,
            str(row["agent"]), str(row["symbol"]), ts, float(row["score"]),
            str(row["label"]), details,
        )

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

# -------------------- Bars (1m) --------------------
async def insert_bar_1m(symbol: str, ts_utc, *args, **kwargs) -> bool:
    """
    Upsert a 1m bar.
    Supports either:
      insert_bar_1m(symbol, ts_utc, {"o":...,"h":...,"l":...,"c":...,"v":...,"vwap":..., "trades":...})
    or
      insert_bar_1m(symbol, ts_utc, o,h,l,c,v, vwap=None, trades=None)
    """
    if args and isinstance(args[0], dict):
        bar = args[0]
        o = float(bar.get("o"))
        h = float(bar.get("h"))
        l = float(bar.get("l"))
        c = float(bar.get("c"))
        v = float(bar.get("v"))
        vwap = bar.get("vwap"); vwap = None if vwap is None else float(vwap)
        trades = bar.get("trades"); trades = None if trades is None else int(trades)
    else:
        # positional form
        o,h,l,c,v = (float(x) for x in args[:5])
        vwap = kwargs.get("vwap", None)
        vwap = None if vwap is None else float(vwap)
        trades = kwargs.get("trades", None)
        trades = None if trades is None else int(trades)

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

# -------------------- Features (1m snapshot) --------------------
async def insert_features_1m(symbol: str, ts_utc, feat: dict) -> bool:
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

# -------------------- Returns view refresh (safe no-op placeholder) --------------------
async def refresh_return_views() -> None:
    """
    Optional hook called by scheduler; keep as a safe no-op unless you add real SQL.
    """
    try:
        pool = await connect_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:
        # Don't raise; scheduler should be resilient.
        pass

# -------------------- Minimal posture & equity stubs --------------------
_POSTURE: dict[str, dict] = {}
_EQUITY: dict[str, list[dict]] = {}

async def get_posture(symbol: str) -> dict:
    return _POSTURE.get(symbol, {"symbol": symbol, "posture": "NO_POSITION", "size": 0, "price": None})

async def set_posture(symbol: str, posture: str, size: float, price: Optional[float], reason: Optional[str] = None) -> None:
    _POSTURE[symbol] = {"symbol": symbol, "posture": posture, "size": float(size), "price": price, "reason": reason, "ts_utc": datetime.now(timezone.utc).isoformat()}

async def record_trade(symbol: str, side: str, qty: float, price: float) -> None:
    _EQUITY.setdefault(symbol, []).append({"ts_utc": datetime.now(timezone.utc).isoformat(), "side": side, "qty": float(qty), "price": float(price)})

async def equity_curve(symbol: str) -> dict:
    return {"symbol": symbol, "equity": _EQUITY.get(symbol, [])}

async def run_migrations_idempotent():
    pool = await connect_pool()
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1);", MIGRATIONS_LOCK_KEY)
        try:
            # put idempotent DDL/DML here
            pass
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1);", MIGRATIONS_LOCK_KEY)
