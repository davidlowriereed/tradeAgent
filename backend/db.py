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
