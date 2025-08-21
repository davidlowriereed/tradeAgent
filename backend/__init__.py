
# Instantiate each agent once; scheduler expects actual Agent instances, not strings.
REGISTRY = {
    "trend_score": TrendScoreAgent(),
    "cvd_divergence": CvdDivergenceAgent(),
    "rvol_spike": RVOLSpikeAgent(),
    "opening_drive": OpeningDriveReversalAgent(),
    "session_reversal": SessionReversalAgent(),
    "llm_analyst": LLMAnalystAgent(),
    "macro_watcher": MacroWatcherAgent(),
    "posture_guard": PostureGuardAgent(),
}

# Backward-compatibility / aliases for legacy env values
REGISTRY.update({
    "momentum": REGISTRY["trend_score"],
    "rvol": REGISTRY["rvol_spike"],
})

