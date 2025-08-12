
from typing import Optional

class Agent:
    def __init__(self, name: str, interval_sec: int = 60):
        self.name = name
        self.interval_sec = interval_sec

    async def run_once(self, symbol: str) -> Optional[dict]:
        raise NotImplementedError
