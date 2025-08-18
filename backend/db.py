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


async def insert_finding_row(row: dict) -> bool:
    return await insert_finding_values(
        row.get("symbol",""), row.get("agent",""),
        float(row.get("score",0.0)), row.get("label",""),
        row.get("details",{}), row.get("ts_utc")
    )

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


async def insert_finding_values(symbol: str, agent: str, score: float, label: str, details: dict, ts_utc: str | None = None) -> bool:
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
        VALUES(COALESCE($1, NOW()), $2, $3, $4, $5, $6::jsonb)
        """, ts_utc, agent, symbol, float(score), label, details)
    return True


# ---------- Bars & Features insert helpers ----------

async def insert_bar_1m(symbol: str, ts_utc, o, h, l, c, v, vwap=None, trades=None) -> bool:
    """
    Upsert a 1m bar. ts_utc can be ISO string or datetime; DB column is timestamptz.
    """
    pool = await connect_pool()
    if not pool:
        return False
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO bars_1m(symbol, ts_utc, o,h,l,c,v,vwap,trades)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (symbol, ts_utc) DO UPDATE SET
              o=EXCLUDED.o, h=EXCLUDED.h, l=EXCLUDED.l, c=EXCLUDED.c,
              v=EXCLUDED.v, vwap=EXCLUDED.vwap, trades=EXCLUDED.trades
            """,
            symbol, ts_utc, o, h, l, c, v, vwap, trades
        )
    return True


async def insert_features_1m(
    symbol: str, ts_utc,
    mom_bps_1m=None, mom_bps_5m=None, mom_bps_15m=None,
    px_vs_vwap_bps_1m=None, px_vs_vwap_bps_5m=None, px_vs_vwap_bps_15m=None,
    rvol_1m=None, rvol_5m=None, rvol_15m=None,
    atr_1m=None, atr_5m=None, atr_15m=None,
    schema_version: int = 1
) -> bool:
    """
    Upsert a 1m feature row aligned to the bar timestamp.
    """
    pool = await connect_pool()
    if not pool:
        return False
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO features_1m(
                symbol, ts_utc,
                mom_bps_1m, mom_bps_5m, mom_bps_15m,
                px_vs_vwap_bps_1m, px_vs_vwap_bps_5m, px_vs_vwap_bps_15m,
                rvol_1m, rvol_5m, rvol_15m,
                atr_1m, atr_5m, atr_15m,
                schema_version
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15
            )
            ON CONFLICT (symbol, ts_utc) DO UPDATE SET
                mom_bps_1m=EXCLUDED.mom_bps_1m,
                mom_bps_5m=EXCLUDED.mom_bps_5m,
                mom_bps_15m=EXCLUDED.mom_bps_15m,
                px_vs_vwap_bps_1m=EXCLUDED.px_vs_vwap_bps_1m,
                px_vs_vwap_bps_5m=EXCLUDED.px_vs_vwap_bps_5m,
                px_vs_vwap_bps_15m=EXCLUDED.px_vs_vwap_bps_15m,
                rvol_1m=EXCLUDED.rvol_1m,
                rvol_5m=EXCLUDED.rvol_5m,
                rvol_15m=EXCLUDED.rvol_15m,
                atr_1m=EXCLUDED.atr_1m,
                atr_5m=EXCLUDED.atr_5m,
                atr_15m=EXCLUDED.atr_15m,
                schema_version=EXCLUDED.schema_version
            """,
            symbol, ts_utc,
            mom_bps_1m, mom_bps_5m, mom_bps_15m,
            px_vs_vwap_bps_1m, px_vs_vwap_bps_5m, px_vs_vwap_bps_15m,
            rvol_1m, rvol_5m, rvol_15m,
            atr_1m, atr_5m, atr_15m,
            schema_version
        )
    return True


async def refresh_return_views() -> bool:
    """
    Refresh forward-return materialized views. (Non-concurrent to keep it simple.)
    """
    pool = await connect_pool()
    if not pool:
        return False
    async with pool.acquire() as conn:
        await conn.execute("REFRESH MATERIALIZED VIEW labels_ret_5m;")
        await conn.execute("REFRESH MATERIALIZED VIEW labels_ret_15m;")
    return True

