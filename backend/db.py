# backend/db.py
from __future__ import annotations
import asyncio, json, os, ssl, base64
from typing import Optional, Any, Dict
from datetime import datetime, timezone
import json

from .config import DATABASE_URL
from .state import RECENT_FINDINGS

# ----- heartbeat (used by /health) -----
HEARTBEATS: dict[str, dict] = {}
async def heartbeat(name: str, status: str = "ok") -> None:
    HEARTBEATS[name] = {"status": status, "last_run": datetime.now(timezone.utc).isoformat()}
heartbeats = HEARTBEATS  # compat alias

# ----- pool + error state -----
POOL: Optional["asyncpg.pool.Pool"] = None
_last_db_error: Optional[str] = None
_tls_mode: str = "unknown"  # "verify-ca" | "require" | "insecure"


def _build_ssl_context_from_env() -> Optional[ssl.SSLContext]:
    """Return a verifying SSLContext if a CA is provided via env."""
    ca_pem = os.getenv("DATABASE_CA_CERT")
    ca_b64 = os.getenv("DATABASE_CA_CERT_B64")
    if not ca_pem and ca_b64:
        try:
            ca_pem = base64.b64decode(ca_b64).decode("utf-8")
        except Exception:
            ca_pem = None
    if not ca_pem:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=ca_pem)
    return ctx

async def _make_pool(ssl_opt):
    import asyncpg
    return await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1, max_size=5,
        ssl=ssl_opt,
        command_timeout=10,
    )

async def connect_pool():
    """Create asyncpg pool with best-effort TLS. Safe to call repeatedly."""
    global POOL, _last_db_error, _tls_mode
    if POOL is not None:
        return POOL
    try:
        # 0) explicit insecure override (debug only)
        if os.getenv("DB_TLS_INSECURE", "").lower() in ("1", "true", "yes"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            POOL = await _make_pool(ctx)
            _tls_mode, _last_db_error = "insecure", None
            return POOL

        # 1) verify-ca if CA provided
        ctx = _build_ssl_context_from_env()
        if ctx:
            try:
                POOL = await _make_pool(ctx)
                _tls_mode, _last_db_error = "verify-ca", None
                return POOL
            except ssl.SSLCertVerificationError as e:
                _last_db_error = f"{type(e).__name__}: {e}"

        # 2) try "require" (ssl=True) which is encrypted and may verify via system store
        try:
            POOL = await _make_pool(True)
            _tls_mode, _last_db_error = "require", None
            return POOL
        except ssl.SSLCertVerificationError as e:
            _last_db_error = f"{type(e).__name__}: {e}"

        # 3) final fallback: encrypted/no-verify (temporary safety net)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        POOL = await _make_pool(ctx)
        _tls_mode, _last_db_error = "insecure", None
        return POOL

    except Exception as e:
        _last_db_error = f"{type(e).__name__}: {e}"
        POOL = None
        return None

async def ensure_schema():
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
    """Health with error detail and TLS mode for easier debugging."""
    global _last_db_error, _tls_mode
    try:
        pool = await connect_pool()
        if not pool:
            return {"ok": False, "mode": _tls_mode, "error": _last_db_error or "no pool"}
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1;")
        return {"ok": True, "mode": _tls_mode}
    except Exception as e:
        _last_db_error = f"{type(e).__name__}: {e}"
        return {"ok": False, "mode": _tls_mode, "error": _last_db_error}


async def insert_finding(row: Dict[str, Any]) -> None:
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
        # keep your graceful fallback
        RECENT_FINDINGS.appendleft(row)

async def fetch_recent_findings(symbol: Optional[str], limit: int = 20):
    """Return newest rows from DB when available; else fall back to in-mem buffer."""
    pool = await connect_pool()
    if not pool:
        return list(RECENT_FINDINGS)[-limit:][::-1]
    where = "WHERE symbol = $2" if symbol else ""
    sql = f"""
      SELECT ts_utc, agent, symbol, score, label, details
      FROM findings
      {where}
      ORDER BY ts_utc DESC
      LIMIT $1
    """
    async with pool.acquire() as conn:
        rows = await (conn.fetch(sql, limit, symbol) if symbol else conn.fetch(sql, limit))
    # asyncpg Record → dict
    return [dict(r) for r in rows]

# --- background supervisor to keep DB healthy ---
async def db_supervisor_loop(interval_sec: int = 30):
    global _last_db_error
    while True:
        try:
            pool = await connect_pool()
            if pool:
                await ensure_schema()
        except Exception as e:
            _last_db_error = f"{type(e).__name__}: {e}"
        await asyncio.sleep(interval_sec)


async def insert_finding(symbol: str, agent: str, score: float, label: str, details: dict, ts_utc: str | None = None):
    pool = await connect_pool()
    if not pool:
        # in-memory fallback
        RECENT_FINDINGS.append({
            "ts_utc": ts_utc, "agent": agent, "symbol": symbol,
            "score": score, "label": label, "details": details
        })
        return False
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO findings(ts_utc, agent, symbol, score, label, details)
        VALUES(COALESCE($1, NOW()), $2, $3, $4, $5, $6)
        """, ts_utc, agent, symbol, float(score), label, json.dumps(details))
    return True

async def insert_finding_row(row: dict):
    return await insert_finding(
        row.get("symbol",""), row.get("agent",""), float(row.get("score",0.0)),
        row.get("label",""), row.get("details",{}), row.get("ts_utc")
    )

