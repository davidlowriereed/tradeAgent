# backend/main.py
try:
    # Optional: load .env for local/dev; harmless if no .env present
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

from .app import app
