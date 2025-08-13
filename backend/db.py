# backend/db.py
import os, json, asyncio
from typing import Optional, List, Dict, Any
import psycopg2
from psycopg2.extras import RealDictCursor
from .config import DATABASE_URL

pg_conn = None

def pg_connect():
    """Return a global autocommit connection."""
    global pg_conn
    if pg_conn:
        return pg_conn
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    pg_conn = psycopg2.connect(DATABASE_URL)
    pg_conn.autocommit = True
    return pg_conn

def pg_exec(sql: str, params=None) -> bool:
    conn = pg_connect()
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
    return True

def pg_fetchone(sql: str, params=None) -> Optional[dict]:
    conn = pg_connect()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params or ())
        row = cur.fetchone()
    return dict(row) if row else None

def ensure_schema() -> bool:
    """Create required tables & indexes (idempotent)."""
    conn = pg_connect()
    with conn.cursor() as cur:
        # findings (agent outputs)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS findings (
          ts_utc   TIMESTAMPTZ DEFAULT NOW(),
          agent    TEXT,
          symbol   TEXT,
          score    DOUBLE PRECISION,
          label    TEXT,
          details  JSONB
        );
        """)

        # agent heartbeats
        cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_runs (
          ts_utc TIMESTAMPTZ DEFAULT NOW(),
          agent  TEXT,
          status TEXT
        );
        """)

        # position state (flat/long/short)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS position_state (
          symbol      TEXT PRIMARY KEY,
          status      TEXT NOT NULL CHECK (status IN ('flat','long','short')),
          qty         DOUBLE PRECISION DEFAULT 0,
          avg_price   DOUBLE PRECISION,
          updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          last_action TEXT,
          last_conf   DOUBLE PRECISION
        );
        """)

        # indexes
        cur.execute("CREATE INDEX IF NOT EXISTS idx_findings_symbol_ts ON findings(symbol, ts_utc DESC);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_ts ON agent_runs(agent, ts_utc DESC);")

    return True

def connect_sync():
    ensure_schema()

async def connect_async():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, connect_sync)

def insert_finding(agent: str, symbol: str, score: float, label: str, details: dict):
    conn = pg_connect()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO findings(agent, symbol, score, label, details) VALUES (%s,%s,%s,%s,%s)",
            (agent, symbol, score, label, json.dumps(details)),
        )

def heartbeat(agent: str, status: str = "ok"):
    conn = pg_connect()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO agent_runs(agent, status) VALUES (%s,%s)", (agent, status))

def latest_trend_snapshot(symbol: str) -> Optional[dict]:
    """Latest trend_score details JSON for a symbol."""
    conn = pg_connect()
    with conn.cursor() as cur:
        cur.execute("""
          SELECT details FROM findings
          WHERE symbol=%s AND agent='trend_score'
          ORDER BY ts_utc DESC LIMIT 1
        """, (symbol,))
        row = cur.fetchone()
    return row[0] if row else None

def fetch_findings(symbol: Optional[str], limit: int) -> List[Dict[str, Any]]:
    conn = pg_connect()
    q = "SELECT ts_utc, agent, symbol, score, label, details FROM findings"
    params: List[Any] = []
    if symbol:
        q += " WHERE symbol=%s"
        params.append(symbol)
    q += " ORDER BY ts_utc DESC LIMIT %s"
    params.append(limit)
    out: List[Dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(q, params)
        for ts, a, sym, sc, lb, det in cur.fetchall():
            out.append({
                "ts_utc": str(ts),
                "agent": a,
                "symbol": sym,
                "score": float(sc) if sc is not None else None,
                "label": lb,
                "details": det,
            })
    return out

def list_agents_last_run() -> List[Dict[str, Any]]:
    conn = pg_connect()
    out: List[Dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute("SELECT agent, MAX(ts_utc) FROM agent_runs GROUP BY agent")
        for a, ts in cur.fetchall():
            out.append({"agent": a, "last_run": str(ts)})
    return out
