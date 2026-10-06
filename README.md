# Otroligt Bot v4

Multi-timeframe crypto signal scanner using public Kraken OHLC + Ticker data.

**Daily regime → 12H setup → 4H confirmation** with Ichimoku (Tenkan/Kijun), MACD, RSI and ATR-based stops.

## Features

- OHLC caching (default 8 min)
- Expanded regimes: `BULLISH` / `BULLISH_BIAS` / `NEUTRAL` / `BEARISH_BIAS` / `BEARISH`
- Configurable RSI bands, ATR multipliers, reward:risk, equity & risk %
- Automatic position sizing from equity × risk %
- Persistent JSONL scan history (`data/scans.jsonl`)
- Clean single-page UI (mobile-friendly)
- Optional HTTP Basic Auth (`AUTH_USER` + `AUTH_PASS`)
- Cloud-ready (`PORT` env var)

## Quick start (local)

```bash
cd otroligt-bot
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

Open http://127.0.0.1:8765

### Enable password protection

```bash
export AUTH_USER=myuser
export AUTH_PASS=supersecret
python server.py
```

## Docker (local)

```bash
docker build -t otroligt-bot .
docker run --rm -p 8765:8765 \
  -e AUTH_USER=myuser \
  -e AUTH_PASS=supersecret \
  -v $(pwd)/data:/app/data \
  otroligt-bot
```

## Deploy to the cloud (use from anywhere)

### Option A – Railway (easiest)

1. Push this folder to a GitHub repo (or use Railway’s CLI).
2. Create a new project on [railway.app](https://railway.app) → Deploy from GitHub.
3. Add environment variables:
   - `AUTH_USER` = your username
   - `AUTH_PASS` = a strong password
4. Railway automatically sets `PORT`. Done.

### Option B – Render

1. New → Web Service → connect the repo.
2. Build command: `pip install -r requirements.txt`
3. Start command: `python server.py`
4. Add `AUTH_USER` and `AUTH_PASS` in Environment.
5. Free tier sleeps after inactivity (first request may be slow).

### Option C – Fly.io

```bash
fly launch          # follow prompts, choose a region
fly secrets set AUTH_USER=myuser AUTH_PASS=supersecret
fly deploy
```

### Option D – Any VPS (DigitalOcean, Hetzner, etc.)

```bash
# on the server
git clone <your-repo> otroligt-bot
cd otroligt-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# protect it
export AUTH_USER=myuser
export AUTH_PASS=supersecret

# run in background (or use systemd / docker)
nohup python server.py > bot.log 2>&1 &
```

Point a domain + Cloudflare / Caddy / nginx if you want HTTPS.

## API

| Endpoint | Description |
|----------|-------------|
| `GET /` | Web UI |
| `GET /api/scan` | Run full scan (accepts config query params) |
| `GET /api/health` | Kraken reachability + latency |
| `GET /api/info` | Version, defaults, auth status |
| `GET /api/history?limit=20` | Recent scan logs |
| `GET /api/clear-cache` | Flush OHLC cache |

### Example with custom risk

```
/api/scan?equity=25000&risk_pct=0.75&rr=3.5
```

## Signal logic (summary)

1. **Daily regime** – close vs Kijun, Tenkan vs Kijun, MACD vs signal  
   - 3/3 → full BULLISH/BEARISH  
   - 2/3 → BULLISH_BIAS / BEARISH_BIAS
2. **12H setup** – same alignment + RSI in band
3. **4H confirmation** – alignment + MACD histogram direction + RSI band
4. **Stops** – recent 20-bar structure ± 0.25 ATR, or 1.5 ATR fallback
5. **Target** – entry ± RR × risk
6. **Size** – (equity × risk%) / distance-to-stop

Only **closed** candles are used.

## Disclaimer

Research / discretionary signal tool only. **Not financial advice.**
