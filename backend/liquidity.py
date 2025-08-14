
# backend/liquidity.py
from typing import Tuple

def get_liquidity_state() -> Tuple[bool, float]:
    """
    Placeholder: returns (risk_on, liquidity_score)
    Real implementation should compute DXY slope, HY spread Δ30d, 2s10s steepening,
    TIPS proxy, and CB balance-sheet Δ90d.
    """
    return (True, 0.0)
