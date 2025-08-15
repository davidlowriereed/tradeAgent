# backend/db.py
import asyncio, ssl, urllib.parse
from typing import Optional, Any, Dict
from .config import DATABASE_URL
from .state import RECENT_FINDINGS
from datetime import datetime, timezone

pg_conn: Optional[Any] = None
HEARTBEATS: dict[str, dict] = {}

def _ssl_kw_from_dsn(dsn: str) -> dict:
    """Map ?sslmode=require to asyncpg's ssl kw."""
    if not dsn:
        return {}
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(dsn).query)
    mode = (qs.get("sslmode", [""])[0] or "").lower()
    if mode == "require":
        # minimal verification (works with DO managed PG)
        return {"ssl": "require"}
    # Uncomment for full verification:
    # ctx = ssl.create_default_context()
    # return {"ssl": ctx}
    return {}

async def heartbeat(name: str, status: str = "ok") -> None:
    """
    Lightweight shim used by scheduler to record agent status.
    Scheduler remains source of truth for /health; this prevents import crashes.
    """
    HEARTBEATS[name] = {
        "status": status,
        "last_run": datetime.now(timezone.utc).isoformat()
    }
# ------------------------------------

async def connect_async():
    global pg_conn
    if pg_conn is not None:
        return pg_conn
    try:
        import asyncpg
        if DATABASE_URL:
            ssl_kw = _ssl_kw_from_dsn(DATABASE_URL)
            pg_conn = await asyncpg.connect(DATABASE_URL, **ssl_kw)
        return pg_conn
    except Exception:
        return None

async def db_health() -> Dict[str, Any]:
    try:
        conn = await connect_async()
        if not conn:
            return {"ok": False}
        # lightweight ping
        await conn.execute("SELECT 1;")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

async def insert_finding(row: Dict[str, Any]) -> None:
    """Best-effort insert with in-mem fallback."""
    try:
        conn = await connect_async()
        if not conn:
            raise RuntimeError("no_db")
        await conn.execute(
            """
            INSERT INTO findings(ts_utc, agent, symbol, score, label, details)
            VALUES(timezone('utc', now()), $1, $2, $3, $4, $5::jsonb)
            """,
            row.get("agent"), row.get("symbol"), float(row.get("score") or 0.0),
            row.get("label"), row.get("details") or {}
        )
    except Exception:
        # in-mem circular buffer already defined in state.py
        RECENT_FINDINGS.appendleft(row)
