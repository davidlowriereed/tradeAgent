
from collections import defaultdict, deque
from typing import Dict, Tuple
from .config import SYMBOLS

# Trades buffer: (ts, price, size, side)
trades: Dict[str, deque] = {sym: deque(maxlen=50_000) for sym in SYMBOLS}

# Best bid/ask cache
best_px: Dict[str, Tuple[float, float]] = {sym: (None, None) for sym in SYMBOLS}

# Posture shared state
POSTURE_STATE: Dict[str, dict] = {}

# agent cooldowns / posting digests etc can be in-memory here if needed
_last_post_digest: Dict[tuple, float] = defaultdict(float)
