# Opportunity Radar (Alpha, Free Data)

This is a **non-technical, click-to-run** starter that shows live crypto order-flow signals from **Coinbase** using **free, public data**. It runs a small website with two pages:

- `/` — a simple dashboard (CVD, 5-min volume + RVOL, best bid/ask)
- `/signals` — a JSON API for the data
- `/health` — quick status check

> Default symbol: **BTC-USD**. You can change it to **ETH-USD** later without code.

---

## Option A — Easiest (Deploy on DigitalOcean App Platform)

**You need:** A DigitalOcean account and this project in a GitHub repo.

1. **Create a GitHub account** (if you don't have one).
2. **Upload these files** to a new GitHub repository (click "Add file" → "Upload files").
3. In DigitalOcean, go to **App Platform → Create App → GitHub** and select your repository.
4. When it asks, set:
   - **Environment Variable** `SYMBOL` = `BTC-USD` (or `ETH-USD`)
   - Build from `Dockerfile`
   - Exposed **HTTP Port** `8080`
5. Click **Deploy**. In ~1–3 minutes, your app will have a public URL.
6. Open the URL. You should see live data updating every 1–2 seconds.

**Stopping / deleting**: In App Platform, click your app → **Destroy** or **Suspend** to stop charges.

> App Platform has a small monthly cost for running apps. You can deploy, test, then pause the app.

---

## Option B — Run on your Mac (local)

**You need:** Docker Desktop installed (free), and this folder on your Mac.

1. Install Docker Desktop for Mac (from docker.com).
2. In Terminal, go to this folder (drag it into the Terminal window).
3. Run: `docker build -t radar .`
4. Then: `docker run -p 8080:8080 -e SYMBOL=BTC-USD radar`
5. Open `http://localhost:8080` in your browser.

Stop with `Ctrl+C` (or stop the container in Docker Desktop).

---

## Change the symbol

- In DigitalOcean: Settings → Environment Variables → change `SYMBOL` to `ETH-USD` → **Redeploy**.
- Locally: change `-e SYMBOL=ETH-USD` in the `docker run` command.

---

## What you're seeing

- **CVD (Cumulative Volume Delta):** estimates whether aggressive buyers or sellers dominate.
- **5-min Volume & RVOL:** shows current activity vs. recent 5-min medians.
- **Best Bid/Ask:** top-of-book quotes from the ticker channel.

This is enough to test the **Micro Scanner**, **Order Flow Sentinel**, and **Manipulation Hunter** ideas on crypto. When you're ready, we’ll plug in paid feeds for equities/options/futures using the same structure.

---

## Next steps (when you're ready)

- Add the **Alpha Optimizer Agent (AOA)** to log outcomes and suggest small tweaks.
- Turn on the **Macro Watcher** using FRED (free).
- Plug in paid feeds (Polygon/SpotGamma/Rithmic) using the same Docker app.

---

## Troubleshooting

- If the dashboard is blank, give it ~10–20 seconds to warm up after deploy.
- If `/health` returns `{ "status": "ok" }` but `/signals` shows `null`, keep it open; it fills as new trades arrive.
- Some corporate networks block WebSockets. Try on home Wi‑Fi or mobile hotspot.

---

*Generated 2025-08-09T18:32:02.531615 UTC*
