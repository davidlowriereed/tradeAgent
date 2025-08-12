
import os

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

#Macro watcher
MACRO_WINDOWS=12:30,14:00
MACRO_WINDOW_MINUTES=10
MACRO_RVOL_MIN=1.3
MACRO_INTERVAL_SEC=60

# Agents thresholds
ALERT_MIN_RVOL = float(os.getenv("ALERT_MIN_RVOL", "5.0"))
ALERT_CVD_DELTA = float(os.getenv("ALERT_CVD_DELTA", "75"))

# TrendScore / posture config
TS_INTERVAL = int(os.getenv("TS_INTERVAL", "30"))
TS_ENTRY = float(os.getenv("TS_ENTRY", "0.65"))
TS_EXIT  = float(os.getenv("TS_EXIT",  "0.35"))
TS_PERSIST = int(os.getenv("TS_PERSIST", "2"))
TS_WEIGHTS = os.getenv("TS_WEIGHTS", "default")
TS_MTF_WEIGHTS = os.getenv("TS_MTF_WEIGHTS", "default")

POSTURE_GUARD_INTERVAL = int(os.getenv("POSTURE_GUARD_INTERVAL", "60"))
