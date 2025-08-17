# backend/db.py
from __future__ import annotations
import asyncio, json, os, ssl
from typing import Optional, Any, Dict
from datetime import datetime, timezone
from .config import DATABASE_URL
import ssl

from .state import RECENT_FINDINGS

# ----- heartbeat (used by /health) -----
HEARTBEATS: dict[str, dict] = {}
async def heartbeat(name: str, status: str = "ok") -> None:
    HEARTBEATS[name] = {"status": status, "last_run": datetime.now(timezone.utc).isoformat()}
heartbeats = HEARTBEATS  # compat alias

# ----- pool + error state -----
POOL: Optional["asyncpg.pool.Pool"] = None
_last_db_error: Optional[str] = None

async def connect_pool():
    """Create a tiny asyncpg pool with TLS. Safe to call repeatedly."""
    global POOL, _last_db_error
    if POOL is not None:
        return POOL
    import asyncpg
    try:
        # Prefer a verifying SSLContext using the DO-provided CA (DATABASE_CA_CERT).
        ca_pem = os.getenv("DATABASE_CA_CERT")
        ssl_opt: "ssl.SSLContext | bool"
        if ca_pem:
            ctx = ssl.create_default_context()
            # load_verify_locations supports PEM string via cadata
            ctx.load_verify_locations(cadata=ca_pem)
            ssl_opt = ctx
        else:
            # Fallback: encrypted but non-verifying (≈ sslmode=require). Securely attach your DB so DATABASE_CA_CERT appears.
            ssl_opt = True

        POOL = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1, max_size=5,
            ssl=ssl_opt,
            command_timeout=10,
        )
        
        _last_db_error = None
        return POOL
    except Exception as e:
        _last_db_error = f"{type(e).__name__}: {e}"
        POOL = None
        return None

async def ensure_schema():
    """Create tables if missing (idempotent)."""
    pool = await connect_pool()
    if not pool:
        return
    stmts = [
        """
        CREATE TABLE IF NOT EXISTS findings (
          id BIGSERIAL PRIMARY KEY,
          ts_utc TIMESTAMPTZ NOT NULL DEFAULT (now() at time zone 'utc'),
          agent TEXT NOT NULL,
          symbol TEXT NOT NULL,
          score DOUBLE PRECISION NOT NULL,
          label TEXT NOT NULL,
          details JSONB NOT NULL DEFAULT '{}'
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS position_state (
          symbol TEXT PRIMARY KEY,
          status TEXT NOT NULL DEFAULT 'no_position',
          qty DOUBLE PRECISION NOT NULL DEFAULT 0,
          avg_price DOUBLE PRECISION NOT NULL DEFAULT 0,
          updated_at TIMESTAMPTZ NOT NULL DEFAULT (now() at time zone 'utc'),
          last_action TEXT,
          last_conf DOUBLE PRECISION
        );
        """,
    ]
    async with pool.acquire() as conn:
        for s in stmts:
            await conn.execute(s)

async def db_health() -> Dict[str, Any]:
    """Health with error detail for easier debugging."""
    global _last_db_error
    try:
        pool = await connect_pool()
        if not pool:
            return {"ok": False, "error": _last_db_error or "no pool"}
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1;")
        return {"ok": True}
    except Exception as e:
        _last_db_error = f"{type(e).__name__}: {e}"
        return {"ok": False, "error": _last_db_error}

async def insert_finding(row: Dict[str, Any]) -> None:
    """Best-effort insert; falls back to in-mem ring buffer if DB is down."""
    try:
        pool = await connect_pool()
        if not pool:
            raise RuntimeError("no_pool")
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO findings(ts_utc, agent, symbol, score, label, details)
                VALUES(timezone('utc', now()), $1, $2, $3, $4, $5::jsonb)
                """,
                row.get("agent"),
                row.get("symbol"),
                float(row.get("score") or 0.0),
                row.get("label"),
                json.dumps(row.get("details") or {}),
            )
    except Exception:
        RECENT_FINDINGS.appendleft(row)
