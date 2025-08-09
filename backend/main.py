from flask import Flask, jsonify, send_file
from flask_cors import CORS
import os
import requests
import psycopg2
import csv
import io
from datetime import datetime

app = Flask(__name__)
CORS(app)

SYMBOL = os.getenv("SYMBOL", "BTC-USD")
DATABASE_URL = os.getenv("DATABASE_URL")  # Postgres connection string

trade_cache = []

# -----------------------------
# DB connection helper
# -----------------------------
def get_db_connection():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL)

# Create table if not exists
def init_db():
    conn = get_db_connection()
    if conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades_crypto (
                id SERIAL PRIMARY KEY,
                symbol TEXT,
                price NUMERIC,
                size NUMERIC,
                side TEXT,
                ts TIMESTAMP
            )
        """)
        conn.commit()
        cur.close()
        conn.close()

# -----------------------------
# Fetch live trades
# -----------------------------
def fetch_trades():
    url = f"https://api.exchange.coinbase.com/products/{SYMBOL}/trades"
    resp = requests.get(url, timeout=5)
    data = resp.json()
    trades = []
    for t in data:
        trade = {
            "price": float(t["price"]),
            "size": float(t["size"]),
            "side": t["side"],
            "time": t["time"]
        }
        trades.append(trade)
    return trades

# -----------------------------
# Store trades in DB
# -----------------------------
def store_trades(trades):
    conn = get_db_connection()
    if not conn:
        return
    cur = conn.cursor()
    for t in trades:
        cur.execute(
            "INSERT INTO trades_crypto (symbol, price, size, side, ts) VALUES (%s, %s, %s, %s, %s)",
            (SYMBOL, t["price"], t["size"], t["side"], datetime.fromisoformat(t["time"].replace("Z", "+00:00")))
        )
    conn.commit()
    cur.close()
    conn.close()

# -----------------------------
# Routes
# -----------------------------
@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "symbol": SYMBOL,
        "trades_cached": len(trade_cache)
    })

@app.route("/db-health")
def db_health():
    try:
        conn = get_db_connection()
        if conn:
            conn.close()
            return jsonify({"db": "ok"})
        else:
            return jsonify({"db": "not configured"})
    except Exception as e:
        return jsonify({"db": "error", "detail": str(e)})

@app.route("/signals")
def signals():
    global trade_cache
    trades = fetch_trades()
    trade_cache.extend(trades)
    trade_cache = trade_cache[-500:]  # limit cache size

    if DATABASE_URL:
        store_trades(trades)

    # Simple example signal
    cvd = sum(t["size"] if t["side"] == "buy" else -t["size"] for t in trade_cache)

    return jsonify({
        "symbol": SYMBOL,
        "cvd": round(cvd, 4),
        "trades_cached": len(trade_cache)
    })

@app.route("/export-csv")
def export_csv():
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "No database configured"}), 400
    cur = conn.cursor()
    cur.execute("SELECT symbol, price, size, side, ts FROM trades_crypto ORDER BY id DESC LIMIT 1000")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["symbol", "price", "size", "side", "ts"])
    for row in rows:
        writer.writerow(row)

    mem = io.BytesIO()
    mem.write(output.getvalue().encode("utf-8"))
    mem.seek(0)
    output.close()

    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="trades.csv")

# -----------------------------
# Init
# -----------------------------
if __name__ == "__main__":
    if DATABASE_URL:
        init_db()
    app.run(host="0.0.0.0", port=8080)
