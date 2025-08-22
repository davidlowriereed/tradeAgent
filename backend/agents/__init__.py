# backend/agents/__init__.py
from .trend_score import TrendScoreAgent
from .opening_drive import OpeningDriveReversalAgent
from .session_reversal import SessionReversalAgent
from .rvol_spike import RVOLSpikeAgent
from .cvd_divergence import CvdDivergenceAgent
from .llm_analyst import LLMAnalystAgent
from .macro_watcher import MacroWatcherAgent
from .posture_guard import PostureGuardAgent

# map short names -> classes (not instances)
REGISTRY = {
    "trend_score": TrendScoreAgent,
    "opening_drive": OpeningDriveReversalAgent,
    "session_reversal": SessionReversalAgent,
    "rvol_spike": RVOLSpikeAgent,
    "cvd_divergence": CVDDivergenceAgent,
    "llm_analyst": LLMAnalystAgent,
    "macro_watcher": MacroWatcherAgent,
    "posture_guard": PostureGuardAgent,
}




