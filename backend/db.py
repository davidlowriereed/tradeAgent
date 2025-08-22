# backend/db.py
from __future__ import annotations

import os
import ssl
import json
import base64
import asyncio
from typing import Any, Dict, Optional

try:
    import asyncpg  # type: ignore
except Exception:  # pragma: no cover
    asyncpg = None  # type: ignore


# --- Config ------------------------------------------------------------------

DB_CONNECT_TIMEOUT_SEC: float = float(os.getenv("DB_CONNECT_TIMEOUT_SEC", "10"))
MIGRATIONS_LOCK_KEY: int = int(os.getenv("MIGRATIONS_LOCK_KEY", "2147483601"))

# Pool + lock are module singletons so the whole app shares one pool.
_POOL: Optional["asyncpg.Pool"] = None
_LOCK: "asyncio.Lock" = asyncio.Lock()


# --- DSN / SSL helpers -------------------------------------------------------

def _dsn() -> str:
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    return dsn


def _ssl_ctx() -> Optional[ssl.SSLContext]:
    """
    Build an SSL context if required.
    Rules:
      - If env PGSSL_DISABLE is truthy -> return None.
      - If DATABASE_URL contains `sslmode=require` OR env PGSSL_REQUIRE is truthy,
        create a default context.
      - If env PG_CA_CERT_BASE64 is present, load it into the context.
    """
    if os.getenv("PGSSL_DISABLE"):
        return None

    dsn_lower = _dsn().lower()
    require = "sslmode=require" in dsn_lower or bool(os.getenv("PGSSL_REQUIRE"))

    if not require:
        return None

    ctx = ssl.create_default_context()

    b64 = os.getenv("PG_CA_CERT_BASE64", "").strip()
    if b64:
        try:
            pem = base64.b64decode(b64).decode("utf-8", "ignore")
            # `cadata` accepts a PEM string
            ctx.load_verify_locations(cadata=pem)
        except Exception as e:  # don't block startup on optional custom CA
            print(f"[db] WARNING: failed to load PG_CA_CERT_BASE64: {e}")

    return ctx


# --- Pool management ----------------------------------------------------------

async def connect_pool():
    """
    Create the global asyncpg pool once and return it.
    """
    global _POOL
    if _POOL is not None:
        return _POOL
    async with _LOCK:
        if _POOL is not None:
            return _POOL
        if asyncpg is None:
            raise RuntimeError("asyncpg not installed")
        _POOL = await asyncpg.create_pool(
            dsn=_dsn(),
            timeout=DB_CONNECT_TIMEOUT_SEC,
            command_timeout=DB_CONNECT_TIMEOUT_SEC,
            min_size=int(os.getenv("PG_POOL_MIN", "1")),
            max_size=int(os.getenv("PG_POOL_MAX", "5")),
            ssl=_ssl_ctx(),
        )
        # Run a very light migration/ensure pass after pool creation
        return _POOL


# --- Health / migrations ------------------------------------------------------

async def db_health() -> Dict[str, Any]:
    """
    Simple connection + round-trip check.
    Returns { ok: bool, mode: "verified" | "degraded", error?: str }
    """
    try:
        pool = await connect_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("select 1;")
        return {"ok": True, "mode": "verified"}
    except Exception as e:
        return {"ok": False, "mode": "degraded", "error": str(e)}


async def run_migrations_idempotent() -> None:
    """
    Acquire an advisory lock and run migrations exactly once.
    Keep it idempotent so multiple app instances don't fight.
    """
    pool = await connect_pool()
    async with pool.acquire() as conn:
        # lock for the duration of migrations
        try:
            await conn.execute("select pg_advisory_lock($1);", MIGRATIONS_LOCK_KEY)
            # Keep this block idempotent (CREATE TABLE IF NOT EXISTS etc.)
            await ensure_schema()
        finally:
            await conn.execute("select pg_advisory_unlock($1);", MIGRATIONS_LOCK_KEY)


async def ensure_schema() -> None:
    """
    Lightweight placeholder. Your project already has ensure_schema_v2()
    in some branches; if present elsewhere, it's fine to keep both.
    Here we only create the minimal objects used by the app if they
    don't already exist.
    """
    pool = await connect_pool()
    async with pool.acquire() as conn:
        # Minimal schema — adjust to your real schema / keep idempotent.
        await conn.execute(
            """
            create table if not exists findings(
                id bigserial primary key,
                ts_utc timestamptz not null default now(),
                agent text not null,
                symbol text not null,
                score double precision not null,
                label text not null,
                details jsonb not null default '{}'::jsonb
            );
            """
        )
        await conn.execute(
            """
            create table if not exists bars_1m(
                ts_utc timestamptz primary key,
                symbol text not null,
                open double precision not null,
                high double precision not null,
                low double precision not null,
                close double precision not null,
                volume double precision not null,
                primary key (ts_utc, symbol)
            );
            """
        )
        await conn.execute(
            """
            create table if not exists features_1m(
                ts_utc timestamptz not null,
                symbol text not null,
                features jsonb not null,
                primary key (ts_utc, symbol)
            );
            """
        )


# Some code imports ensure_schema_v2; provide a thin alias to avoid crashes.
ensure_schema_v2 = ensure_schema


# --- Writes / utilities -------------------------------------------------------

def _to_jsonb(value: Any) -> str:
    try:
        import orjson  # type: ignore
        return orjson.dumps(value).decode("utf-8")
    except Exception:
        return json.dumps(value, separators=(",", ":"), default=str)


async def heartbeat() -> str:
    pool = await connect_pool()
    async with pool.acquire() as conn:
        ts = await conn.fetchval("select now()::text;")
        return str(ts)


async def refresh_return_views() -> None:
    """
    If you have materialized views, refresh them here. It's safe as a no-op.
    """
    return None


async def insert_finding_row(row: Dict[str, Any]) -> None:
    details = row.get("details") or {}
    details_json = _to_jsonb(details)

    pool = await connect_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            insert into findings (agent, symbol, ts_utc, score, label, details)
            values ($1, $2, coalesce($3, now()), $4, $5, $6::jsonb)
            """,
            str(row.get("agent")),
            str(row.get("symbol")),
            row.get("ts_utc"),
            float(row.get("score", 0.0)),
            str(row.get("label", "")),
            details_json,
        )


async def insert_bar_1m(row: Dict[str, Any]) -> None:
    pool = await connect_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            insert into bars_1m (ts_utc, symbol, open, high, low, close, volume)
            values ($1, $2, $3, $4, $5, $6, $7)
            on conflict (ts_utc, symbol) do nothing
            """,
            row.get("ts_utc"),
            str(row.get("symbol")),
            float(row.get("open", 0.0)),
            float(row.get("high", 0.0)),
            float(row.get("low", 0.0)),
            float(row.get("close", 0.0)),
            float(row.get("volume", 0.0)),
        )


async def insert_features_1m(symbol: str, ts_utc, features: Dict[str, Any]) -> None:
    pool = await connect_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            insert into features_1m (ts_utc, symbol, features)
            values ($1, $2, $3::jsonb)
            on conflict (ts_utc, symbol) do update
                set features = excluded.features
            """,
            ts_utc,
            str(symbol),
            _to_jsonb(features),
        )
