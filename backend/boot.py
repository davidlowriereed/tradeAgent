
from __future__ import annotations
import asyncio, time, os
from dataclasses import dataclass, field
from enum import Enum

READINESS_GRACE_SEC = int(os.getenv("READINESS_GRACE_SEC", "0"))
DB_CONNECT_TIMEOUT_SEC = float(os.getenv("DB_CONNECT_TIMEOUT_SEC", "5"))
MIGRATIONS_TIMEOUT_SEC = float(os.getenv("MIGRATIONS_TIMEOUT_SEC", "10"))
BOOT_FAILFAST = os.getenv("BOOT_FAILFAST", "0").lower() in ("1", "true", "yes")

class Stage(Enum):
    BOOTING = "BOOTING"
    DB_POOL = "DB_POOL"
    DB_MIGRATIONS = "DB_MIGRATIONS"
    AGENTS = "AGENTS"
    READY = "READY"

@dataclass
class BootState:
    stage: Stage = Stage.BOOTING
    started_at: float = field(default_factory=time.time)
    ready_at: float | None = None
    errors: dict[str, str] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)

class BootOrchestrator:
    def __init__(self, *, db_connect, run_migrations, start_agents):
        self.state = BootState()
        self._ready_event = asyncio.Event()
        self._db_connect = db_connect
        self._run_migrations = run_migrations
        self._start_agents = start_agents

    @property
    def ready(self) -> bool:
        return self._ready_event.is_set()

    async def _run_step(self, name: str, coro_fn, timeout: float, retryable: bool = True):
        backoff = 1.0
        while True:
            try:
                self.state.attempts[name] = self.state.attempts.get(name, 0) + 1
                await asyncio.wait_for(coro_fn(), timeout=timeout)
                self.state.errors.pop(name, None)
                return
            except Exception as e:
                self.state.errors[name] = f"{type(e).__name__}: {e}"
                if BOOT_FAILFAST or not retryable:
                    raise
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def run(self):
        self.state.stage = Stage.DB_POOL
        await self._run_step("db_pool", self._db_connect, DB_CONNECT_TIMEOUT_SEC)
        self.state.stage = Stage.DB_MIGRATIONS
        await self._run_step("migrations", self._run_migrations, MIGRATIONS_TIMEOUT_SEC)
        self.state.stage = Stage.AGENTS
        try:
            await self._run_step("agents", self._start_agents, 5.0, retryable=False)
        except Exception as e:
            self.state.errors["agents"] = f"non-fatal: {type(e).__name__}: {e}"
        if READINESS_GRACE_SEC > 0:
            await asyncio.sleep(READINESS_GRACE_SEC)
        self.state.stage = Stage.READY
        self.state.ready_at = time.time()
        self._ready_event.set()
