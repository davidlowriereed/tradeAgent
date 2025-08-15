*** a/backend/main.py
--- b/backend/main.py
@@
-from .app import app
+try:
+    # Optional: load .env for local/dev; harmless in prod
+    from dotenv import load_dotenv  # type: ignore
+    load_dotenv()
+except Exception:
+    pass
+from .app import app
