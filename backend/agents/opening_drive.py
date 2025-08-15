
from __future__ import annotations
from typing import Optional
from .base import Agent

class OpeningDriveReversalAgent(Agent):
    name = "opening_drive"
    def __init__(self, interval_sec: int = 60):
        super().__init__(self.name, interval_sec)
    async def run_once(self, symbol: str) -> Optional[dict]:
        return None
