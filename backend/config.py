
import os

def _f(key: str, default: float) -> float:
    try: return float(os.getenv(key, str(default)))
    except Exception: return float(default)

def _i(key: str, default: int) -> int:
    try: return int(float(os.getenv(key, str(default))))
    except Exception: return int(default)

SYMBOLS = [s.strip() for s in os.getenv("SYMBOL", "BTC-USD,ETH-USD,ADA-USD").split(",") if s.strip()]

# Slack / alerts
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL") or ""
SLACK_ANALYSIS_ONLY = os.getenv("SLACK_ANALYSIS_ONLY", "true").lower() == "true"
ALERT_VERBOSE = os.getenv("ALERT_VERBOSE", "false").lower() == "true"
AGENT_ALERT_COOLDOWN_SEC = int(os.getenv("AGENT_ALERT_COOLDOWN_SEC", "120"))

# DB
DATABASE_URL = os.getenv("DATABASE_URL")

# LLM
LLM_ENABLE = os.getenv("LLM_ENABLE", "true").lower() == "true"
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or os.getenv("LLM_MODEL") or "gpt-4o-mini"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
LLM_MIN_INTERVAL = int(os.getenv("LLM_MIN_INTERVAL", "180"))
LLM_MAX_INPUT_TOKENS = int(os.getenv("LLM_MAX_INPUT_TOKENS", "4000"))
LLM_ALERT_MIN_CONF = float(os.getenv("LLM_ALERT_MIN_CONF", "0.60"))
LLM_ANALYST_MIN_SCORE = float(os.getenv("LLM_ANALYST_MIN_SCORE", "3.0"))
LLM_USE_PROXY = os.getenv("LLM_USE_PROXY", "false").lower() == "true"
LLM_IGNORE_PROXY = os.getenv("LLM_IGNORE_PROXY", "true").lower() == "true"

# --- Macro watcher (env-driven, resilient parsing) ---
# Example env: MACRO_WINDOWS="12:30,14:00,18:00"
MACRO_WINDOWS_STR = os.getenv("MACRO_WINDOWS", "12:30,14:00")
MACRO_WINDOWS = [w.strip() for w in MACRO_WINDOWS_STR.split(",") if w.strip()]

MACRO_WINDOW_MINUTES = int(os.getenv("MACRO_WINDOW_MINUTES", "10"))
MACRO_RVOL_MIN       = float(os.getenv("MACRO_RVOL_MIN", "1.3"))
MACRO_INTERVAL_SEC   = int(os.getenv("MACRO_INTERVAL_SEC", "60"))


# ---- existing values; ensure these are numeric ----
ALERT_MIN_RVOL       = _f("ALERT_MIN_RVOL", 5.0)
ALERT_CVD_DELTA      = _f("ALERT_CVD_DELTA", 75.0)
AGENT_ALERT_COOLDOWN_SEC = _i("AGENT_ALERT_COOLDOWN_SEC", 120)

# TrendScore / posture config
TS_INTERVAL = int(os.getenv("TS_INTERVAL", "30"))
TS_ENTRY = float(os.getenv("TS_ENTRY", "0.65"))
TS_EXIT  = float(os.getenv("TS_EXIT",  "0.35"))
TS_PERSIST = int(os.getenv("TS_PERSIST", "2"))
TS_WEIGHTS = os.getenv("TS_WEIGHTS", "default")
TS_MTF_WEIGHTS = os.getenv("TS_MTF_WEIGHTS", "default")

POSTURE_GUARD_INTERVAL = int(os.getenv("POSTURE_GUARD_INTERVAL", "60"))

# --- Runtime mode & feature flags ---
MODE = os.getenv("MODE", "realtime")  # "realtime" | "backtest"
FEATURE_BARS = os.getenv("FEATURE_BARS", "true").lower() == "true"
FEATURE_NEW_TREND = os.getenv("FEATURE_NEW_TREND", "true").lower() == "true"
FEATURE_REVERSAL = os.getenv("FEATURE_REVERSAL", "false").lower() == "true"
FEATURE_LIQUIDITY = os.getenv("FEATURE_LIQUIDITY", "false").lower() == "true"

# Trend / posture thresholds (make sure all are numbers)
POSTURE_ENTRY_CONF   = _f("POSTURE_ENTRY_CONF", 0.60)
PG_CVD_5M_NEG        = _f("PG_CVD_5M_NEG", 1500)
PG_CVD_2M_NEG        = _f("PG_CVD_2M_NEG", 800)
PG_RVOL_RATIO        = _f("PG_RVOL_RATIO", 1.2)
PG_PUSH_RVOL_MIN     = _f("PG_PUSH_RVOL_MIN", 0.70)
PG_MOM5_DOWN_BPS     = _f("PG_MOM5_DOWN_BPS", -8)
PG_VWAP_MINUTES      = _i("PG_VWAP_MINUTES", 20)
POSTURE_MAX_AGE_MIN  = _i("POSTURE_MAX_AGE_MIN", 30)
PG_PERSIST_K         = _i("PG_PERSIST_K", 5)





