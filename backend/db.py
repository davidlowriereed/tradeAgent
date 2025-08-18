# backend/db.py
from __future__ import annotations
import asyncio, json, os, ssl, base64
from typing import Any, Dict, Optional
from datetime import datetime, timezone

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

    await conn.execute("""
    CREATE TABLE IF NOT EXISTS bars_1m (
      symbol   text NOT NULL,
      ts_utc   timestamptz NOT NULL,
      o        numeric NOT NULL,
      h        numeric NOT NULL,
      l        numeric NOT NULL,
      c        numeric NOT NULL,
      v        numeric NOT NULL,
      vwap     numeric,
      trades   integer,
      PRIMARY KEY (symbol, ts_utc)
    );
    CREATE INDEX IF NOT EXISTS bars_1m_symbol_ts ON bars_1m(symbol, ts_utc);

    CREATE TABLE IF NOT EXISTS features_1m (
      symbol   text NOT NULL,
      ts_utc   timestamptz NOT NULL,
      mom_bps_1m  numeric,
      mom_bps_5m  numeric,
      mom_bps_15m numeric,
      px_vs_vwap_bps_1m  numeric,
      px_vs_vwap_bps_5m  numeric,
      px_vs_vwap_bps_15m numeric,
      rvol_1m   numeric,
      rvol_5m   numeric,
      rvol_15m  numeric,
      atr_1m    numeric,
      atr_5m    numeric,
      atr_15m   numeric,
      schema_version integer NOT NULL DEFAULT 1,
      PRIMARY KEY (symbol, ts_utc)
    );
    CREATE INDEX IF NOT EXISTS features_1m_symbol_ts ON features_1m(symbol, ts_utc);
    """)

    # Forward return materialized views (safe to (re)create)
    await conn.execute("""
    CREATE MATERIALIZED VIEW IF NOT EXISTS labels_ret_5m AS
    SELECT b.symbol, b.ts_utc,
           LEAD(b.c, 5) OVER (PARTITION BY b.symbol ORDER BY b.ts_utc) AS c_fwd,
           b.c AS c_now
    FROM bars_1m b;

    CREATE MATERIALIZED VIEW IF NOT EXISTS labels_ret_15m AS
    SELECT b.symbol, b.ts_utc,
           LEAD(b.c, 15) OVER (PARTITION BY b.symbol ORDER BY b.ts_utc) AS c_fwd,
           b.c AS c_now
    FROM bars_1m b;

    -- convenience views with returns (ignore null forwards)
    CREATE OR REPLACE VIEW ret_5m AS
    SELECT symbol, ts_utc, (c_fwd / NULLIF(c_now,0) - 1.0) AS ret
    FROM labels_ret_5m WHERE c_fwd IS NOT NULL;

    CREATE OR REPLACE VIEW ret_15m AS
    SELECT symbol, ts_utc, (c_fwd / NULLIF(c_now,0) - 1.0) AS ret
    FROM labels_ret_15m WHERE c_fwd IS NOT NULL;
    """)


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


async def insert_bar_1m(symbol: str, ts_utc: str, bar: Dict[str, Any]) -> None:
    """Upsert a 1m bar."""
    pool = await connect_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO bars_1m(symbol, ts_utc, o,h,l,c,v,vwap,trades)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)
        ON CONFLICT(symbol, ts_utc) DO UPDATE SET
          o=EXCLUDED.o, h=EXCLUDED.h, l=EXCLUDED.l, c=EXCLUDED.c,
          v=EXCLUDED.v, vwap=EXCLUDED.vwap, trades=EXCLUDED.trades
        """, symbol, ts_utc,
           bar.get("o"), bar.get("h"), bar.get("l"), bar.get("c"),
           bar.get("v"), bar.get("vwap"), bar.get("trades"))



async def insert_features_1m(symbol: str, ts_utc: str, fx: Dict[str, Any], schema_version: int = 1) -> None:
    """Upsert a 1m feature vector."""
    pool = await connect_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute("""
        INSERT INTO features_1m(symbol, ts_utc,
          mom_bps_1m, mom_bps_5m, mom_bps_15m,
          px_vs_vwap_bps_1m, px_vs_vwap_bps_5m, px_vs_vwap_bps_15m,
          rvol_1m, rvol_5m, rvol_15m,
          atr_1m, atr_5m, atr_15m, schema_version)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
        ON CONFLICT(symbol, ts_utc) DO UPDATE SET
          mom_bps_1m=EXCLUDED.mom_bps_1m,
          mom_bps_5m=EXCLUDED.mom_bps_5m,
          mom_bps_15m=EXCLUDED.mom_bps_15m,
          px_vs_vwap_bps_1m=EXCLUDED.px_vs_vwap_bps_1m,
          px_vs_vwap_bps_5m=EXCLUDED.px_vs_vwap_bps_5m,
          px_vs_vwap_bps_15m=EXCLUDED.px_vs_vwap_bps_15m,
          rvol_1m=EXCLUDED.rvol_1m, rvol_5m=EXCLUDED.rvol_5m, rvol_15m=EXCLUDED.rvol_15m,
          atr_1m=EXCLUDED.atr_1m, atr_5m=EXCLUDED.atr_5m, atr_15m=EXCLUDED.atr_15m,
          schema_version=EXCLUDED.schema_version
        """, symbol, ts_utc,
           fx.get("mom_bps_1m"), fx.get("mom_bps_5m"), fx.get("mom_bps_15m"),
           fx.get("px_vs_vwap_bps_1m"), fx.get("px_vs_vwap_bps_5m"), fx.get("px_vs_vwap_bps_15m"),
           fx.get("rvol_1m"), fx.get("rvol_5m"), fx.get("rvol_15m"),
           fx.get("atr_1m"), fx.get("atr_5m"), fx.get("atr_15m"),
           schema_version)


async def refresh_return_views():
    pool = await connect_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY labels_ret_5m;")
        await conn.execute("REFRESH MATERIALIZED VIEW CONCURRENTLY labels_ret_15m;")
