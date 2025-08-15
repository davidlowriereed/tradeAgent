
from __future__ import annotations
from typing import Optional

class Agent:
    name: str
    interval_sec: int
    def __init__(self, name: str, interval_sec: int = 10):
        self.name = name
        self.interval_sec = interval_sec
    async def run_once(self, symbol: str) -> Optional[dict]:
        raise NotImplementedError
