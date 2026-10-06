#!/usr/bin/env python3
"""
Otroligt Bot v4 – Multi-timeframe crypto signal scanner (Kraken)
Improvements over v3:
  - OHLC caching (default 8 min)
  - Expanded regime (BULLISH / BULLISH_BIAS / NEUTRAL / BEARISH_BIAS / BEARISH)
  - Configurable RSI bands, ATR multipliers, RR
  - Position sizing from equity + risk %
  - Persistent JSONL scan log
  - Clean JSON API + modern single-page frontend
"""

import json
import math
import time
import socket
import threading
import os
import base64
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timezone, timedelta
from collections import OrderedDict
import pandas as pd

# ─── Configuration ───────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "static"
LOG_FILE = DATA_DIR / "scans.jsonl"

# Optional HTTP Basic Auth (set both env vars to enable)
#   export AUTH_USER=myuser
#   export AUTH_PASS=mypassword
AUTH_USER = os.environ.get("AUTH_USER", "").strip()
AUTH_PASS = os.environ.get("AUTH_PASS", "").strip()
AUTH_ENABLED = bool(AUTH_USER and AUTH_PASS)

PAIRS = [
    "AAVEUSD", "MORPHOUSD", "HYPEUSD", "SOLUSD", "LINKUSD",
    "NEARUSD", "SUIUSD", "ONDOUSD", "TAOUSD", "XRPUSD",
]

API = "https://api.kraken.com/0/public/OHLC"
TICKER = "https://api.kraken.com/0/public/Ticker"
USER_AGENT = "OtroligtBot/4.0"

# Defaults (can be overridden via /api/scan?… query params)
DEFAULTS = {
    "cache_minutes": 8,
    "rsi_long_low": 50,
    "rsi_long_high": 70,
    "rsi_short_low": 30,
    "rsi_short_high": 50,
    "atr_structure": 0.25,   # structure ± this * ATR
    "atr_fallback": 1.5,     # price ± this * ATR
    "rr": 4.0,
    "equity": 10000.0,       # USD
    "risk_pct": 1.0,         # % of equity risked per trade
}

# ─── Simple in-memory OHLC cache ─────────────────────────────────────────────
_cache_lock = threading.Lock()
_ohlc_cache: dict[tuple[str, int], tuple[float, pd.DataFrame]] = {}  # (pair, interval) → (ts, df)


def get_json(url: str, params: dict, attempts: int = 3):
    q = urllib.parse.urlencode(params)
    last_error = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(
                url + "?" + q, headers={"User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode("utf-8")
            data = json.loads(raw)
            if data.get("error"):
                raise RuntimeError("Kraken API: " + "; ".join(map(str, data["error"])))
            return data["result"]
        except urllib.error.HTTPError as e:
            last_error = RuntimeError(f"Kraken HTTP {e.code}")
            if e.code not in (429, 500, 502, 503, 504):
                raise last_error
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            last_error = RuntimeError(f"Network error: {reason}")
        except json.JSONDecodeError as e:
            last_error = RuntimeError(f"Invalid response from Kraken: {e}")
        if attempt < attempts - 1:
            time.sleep(1.0 * (attempt + 1))
    raise last_error or RuntimeError("Unknown Kraken connection error")


def ohlc(pair: str, interval: int, cache_minutes: float = 8.0) -> pd.DataFrame:
    key = (pair, interval)
    now = time.time()
    with _cache_lock:
        if key in _ohlc_cache:
            ts, df = _ohlc_cache[key]
            if now - ts < cache_minutes * 60:
                return df.copy()

    result = get_json(API, {"pair": pair, "interval": interval})
    keys = [k for k in result if k != "last"]
    if not keys:
        raise RuntimeError(f"Kraken returned no OHLC data for {pair}")
    rows = result[keys[0]]
    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "vwap", "volume", "trades"],
    )
    if df.empty:
        raise RuntimeError(f"No OHLC candles returned for {pair}")
    df["time"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    for c in ["open", "high", "low", "close", "volume", "trades"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # Drop currently forming candle
    if len(df) > 1:
        df = df.iloc[:-1].copy()
    df = df[["time", "open", "high", "low", "close", "volume", "trades"]].reset_index(drop=True)

    with _cache_lock:
        _ohlc_cache[key] = (now, df)
        # Keep cache from growing forever
        if len(_ohlc_cache) > 40:
            oldest = min(_ohlc_cache.items(), key=lambda x: x[1][0])
            del _ohlc_cache[oldest[0]]
    return df.copy()


def batch_ticker(pairs: list[str]) -> dict[str, float]:
    try:
        result = get_json(TICKER, {"pair": ",".join(pairs)})
    except Exception:
        return {}
    prices = {}
    for requested in pairs:
        if requested in result:
            try:
                prices[requested] = float(result[requested]["c"][0])
            except Exception:
                pass
            continue
        norm_req = requested.replace("X", "").replace("Z", "")
        for key, val in result.items():
            if key == "last":
                continue
            norm_key = str(key).replace("X", "").replace("Z", "")
            if (
                norm_key == norm_req
                or norm_key.endswith(norm_req)
                or norm_req.endswith(norm_key)
            ):
                try:
                    prices[requested] = float(val["c"][0])
                except Exception:
                    pass
                break
    return prices


# ─── Indicators ──────────────────────────────────────────────────────────────
def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    pc = df.close.shift(1)
    tr = pd.concat(
        [(df.high - df.low), (df.high - pc).abs(), (df.low - pc).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def macd(df: pd.DataFrame):
    m = ema(df.close, 12) - ema(df.close, 26)
    sig = ema(m, 9)
    return m, sig, m - sig


def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    d = df.close.diff()
    g = d.clip(lower=0)
    l = -d.clip(upper=0)
    ag = g.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    al = l.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = ag / al
    return 100 - (100 / (1 + rs))


def add_ind(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["Tenkan"] = (df.high.rolling(9).max() + df.low.rolling(9).min()) / 2
    df["Kijun"] = (df.high.rolling(26).max() + df.low.rolling(26).min()) / 2
    df["MACD"], df["MACD_signal"], df["MACD_hist"] = macd(df)
    df["RSI"] = rsi(df)
    df["ATR"] = atr(df)
    return df


def resample12(df: pd.DataFrame) -> pd.DataFrame:
    x = df.set_index("time")
    out = x.resample("12h").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "trades": "sum",
        }
    )
    n = x.close.resample("12h").count()
    out = out[n == 3].dropna(subset=["open", "high", "low", "close"]).reset_index()
    return out


# ─── Regime (expanded) ───────────────────────────────────────────────────────
def regime(row) -> str:
    """
    Full alignment  → BULLISH / BEARISH
    2-of-3 alignment → BULLISH_BIAS / BEARISH_BIAS
    else             → NEUTRAL
    """
    vals = [row.get(c) for c in ["Kijun", "Tenkan", "MACD", "MACD_signal"]]
    if any(pd.isna(v) for v in vals):
        return "UNKNOWN"

    close_above = row.close > row.Kijun
    tenkan_above = row.Tenkan > row.Kijun
    macd_bull = row.MACD > row.MACD_signal

    bull_count = sum([close_above, tenkan_above, macd_bull])
    bear_count = sum([not close_above, not tenkan_above, not macd_bull])

    if bull_count == 3:
        return "BULLISH"
    if bear_count == 3:
        return "BEARISH"
    if bull_count == 2:
        return "BULLISH_BIAS"
    if bear_count == 2:
        return "BEARISH_BIAS"
    return "NEUTRAL"


def scan_pair(pair: str, live_price: float | None, cfg: dict) -> dict:
    d4 = add_ind(ohlc(pair, 240, cfg["cache_minutes"]))
    d1 = add_ind(ohlc(pair, 1440, cfg["cache_minutes"]))
    d12 = add_ind(resample12(d4))

    if len(d4) < 60 or len(d12) < 30 or len(d1) < 60:
        raise RuntimeError(f"Not enough candle history returned for {pair}")

    r1 = d1.iloc[-1]
    r12 = d12.iloc[-1]
    r4 = d4.iloc[-1]

    reference = float(r4.close)
    price = (
        float(live_price)
        if live_price is not None and math.isfinite(live_price)
        else reference
    )
    price_source = "Kraken live ticker" if live_price is not None else "last closed 4H"

    reg = regime(r1)

    # Setup conditions (12H)
    long_setup = (
        r12.close > r12.Kijun
        and r12.Tenkan > r12.Kijun
        and r12.MACD > r12.MACD_signal
        and cfg["rsi_long_low"] <= r12.RSI <= cfg["rsi_long_high"]
    )
    short_setup = (
        r12.close < r12.Kijun
        and r12.Tenkan < r12.Kijun
        and r12.MACD < r12.MACD_signal
        and cfg["rsi_short_low"] <= r12.RSI <= cfg["rsi_short_high"]
    )

    # Entry confirmation (4H)
    long_entry = (
        r4.close > r4.Kijun
        and r4.Tenkan > r4.Kijun
        and r4.MACD > r4.MACD_signal
        and r4.MACD_hist > 0
        and cfg["rsi_long_low"] <= r4.RSI <= cfg["rsi_long_high"]
    )
    short_entry = (
        r4.close < r4.Kijun
        and r4.Tenkan < r4.Kijun
        and r4.MACD < r4.MACD_signal
        and r4.MACD_hist < 0
        and cfg["rsi_short_low"] <= r4.RSI <= cfg["rsi_short_high"]
    )

    confirmations = []
    if reg in ("BULLISH", "BULLISH_BIAS"):
        confirmations.append(f"Daily regime {reg.lower()}")
    if reg in ("BEARISH", "BEARISH_BIAS"):
        confirmations.append(f"Daily regime {reg.lower()}")
    if long_setup:
        confirmations.append("12H long setup")
    if short_setup:
        confirmations.append("12H short setup")
    if long_entry:
        confirmations.append("4H long confirmation")
    if short_entry:
        confirmations.append("4H short confirmation")

    # Direction requires full alignment + bias allowance
    direction = None
    if reg in ("BULLISH", "BULLISH_BIAS") and long_setup and long_entry:
        direction = "LONG"
    elif reg in ("BEARISH", "BEARISH_BIAS") and short_setup and short_entry:
        direction = "SHORT"

    atrv = float(r4.ATR) if pd.notna(r4.ATR) else None
    stop = target = risk = position_size = None

    if direction and atrv and atrv > 0:
        look = d4.tail(20)
        if direction == "LONG":
            structure = float(look.low.min())
            stop = min(structure - cfg["atr_structure"] * atrv, price - cfg["atr_fallback"] * atrv)
            risk = price - stop
            target = price + cfg["rr"] * risk
        else:
            structure = float(look.high.max())
            stop = max(structure + cfg["atr_structure"] * atrv, price + cfg["atr_fallback"] * atrv)
            risk = stop - price
            target = price - cfg["rr"] * risk

        if risk <= 0 or not math.isfinite(risk):
            direction = None
            stop = target = risk = None
        else:
            # Position size: risk a fixed % of equity
            risk_amount = cfg["equity"] * (cfg["risk_pct"] / 100.0)
            position_size = risk_amount / risk  # units of the asset

    # Scoring (bias gets partial credit)
    score = 0
    if reg in ("BULLISH", "BEARISH"):
        score += 2
    elif reg in ("BULLISH_BIAS", "BEARISH_BIAS"):
        score += 1
    if (long_setup and reg.startswith("BULL")) or (short_setup and reg.startswith("BEAR")):
        score += 2
    if (long_entry and reg.startswith("BULL")) or (short_entry and reg.startswith("BEAR")):
        score += 2

    decision = "BUY" if direction == "LONG" else "SELL" if direction == "SHORT" else "WAIT"

    return {
        "pair": pair.replace("USD", ""),
        "decision": decision,
        "score": score,
        "price": round(price, 6) if price else None,
        "entry": round(price, 6) if direction else None,
        "stop": round(stop, 6) if stop else None,
        "target": round(target, 6) if target else None,
        "rr": cfg["rr"] if direction else None,
        "risk": round(risk, 6) if risk else None,
        "position_size": round(position_size, 4) if position_size else None,
        "position_value": round(position_size * price, 2) if position_size else None,
        "regime": reg,
        "confirmations": confirmations,
        "rsi": round(float(r4.RSI), 1) if pd.notna(r4.RSI) else None,
        "atr": round(atrv, 6) if atrv else None,
        "price_source": price_source,
        "last_closed_4h": r4.time.isoformat(),
        "last_closed_1d": r1.time.isoformat(),
    }


def parse_cfg(qs: dict) -> dict:
    cfg = DEFAULTS.copy()
    for k, v in qs.items():
        if k in cfg:
            try:
                cfg[k] = type(DEFAULTS[k])(v[0] if isinstance(v, list) else v)
            except (ValueError, TypeError):
                pass
    return cfg


def append_log(payload: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str) + "\n")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body, ctype: str = "text/html; charset=utf-8", extra_headers: dict | None = None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _check_auth(self) -> bool:
        """Return True if request is allowed. Sends 401 and returns False if not."""
        if not AUTH_ENABLED:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                user, _, pw = decoded.partition(":")
                if user == AUTH_USER and pw == AUTH_PASS:
                    return True
            except Exception:
                pass
        self._send(
            401,
            "Authentication required",
            "text/plain",
            extra_headers={"WWW-Authenticate": 'Basic realm="Otroligt Bot"'},
        )
        return False

    def do_GET(self):
        if not self._check_auth():
            return

        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path.startswith("/api/health"):
            try:
                started = time.time()
                get_json(API, {"pair": "XBTUSD", "interval": 60}, attempts=1)
                self._send(
                    200,
                    json.dumps(
                        {
                            "ok": True,
                            "kraken": "reachable",
                            "latency_ms": round((time.time() - started) * 1000),
                            "cache_entries": len(_ohlc_cache),
                        }
                    ),
                    "application/json",
                )
            except Exception as e:
                self._send(
                    200,
                    json.dumps({"ok": False, "kraken": "unreachable", "error": str(e)}),
                    "application/json",
                )
            return

        if path.startswith("/api/info"):
            lan_ip = "127.0.0.1"
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("1.1.1.1", 80))
                lan_ip = s.getsockname()[0]
                s.close()
            except Exception:
                pass
            port = int(os.environ.get("PORT", "8765"))
            self._send(
                200,
                json.dumps(
                    {
                        "lan_url": f"http://{lan_ip}:{port}",
                        "local_url": f"http://127.0.0.1:{port}",
                        "version": "4.0",
                        "auth_enabled": AUTH_ENABLED,
                        "defaults": DEFAULTS,
                        "pairs": [p.replace("USD", "") for p in PAIRS],
                    }
                ),
                "application/json",
            )
            return

        if path.startswith("/api/scan"):
            cfg = parse_cfg(qs)
            started = time.time()
            out = []
            prices = batch_ticker(PAIRS)
            for idx, p in enumerate(PAIRS):
                try:
                    out.append(scan_pair(p, prices.get(p), cfg))
                except Exception as e:
                    out.append(
                        {
                            "pair": p.replace("USD", ""),
                            "decision": "ERROR",
                            "score": 0,
                            "regime": "ERROR",
                            "error": str(e),
                        }
                    )
                if idx < len(PAIRS) - 1:
                    time.sleep(0.25)

            errors = sum(1 for x in out if x.get("decision") == "ERROR")
            payload = {
                "updated": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.time() - started, 1),
                "errors": errors,
                "ticker_prices": len(prices),
                "config": cfg,
                "results": out,
            }
            # Persist
            try:
                append_log(payload)
            except Exception:
                pass

            self._send(200, json.dumps(payload), "application/json")
            return

        if path.startswith("/api/history"):
            limit = 20
            try:
                limit = min(100, max(1, int(qs.get("limit", ["20"])[0])))
            except Exception:
                pass
            rows = []
            if LOG_FILE.exists():
                with open(LOG_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                for line in reversed(lines[-limit:]):
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
            self._send(200, json.dumps({"count": len(rows), "scans": rows}), "application/json")
            return

        if path.startswith("/api/clear-cache"):
            with _cache_lock:
                _ohlc_cache.clear()
            self._send(200, json.dumps({"ok": True, "cleared": True}), "application/json")
            return

        # Static files
        if path == "/" or path == "/index.html":
            html_path = STATIC_DIR / "index.html"
            if html_path.exists():
                self._send(200, html_path.read_text(encoding="utf-8"))
            else:
                self._send(500, "Frontend missing – static/index.html not found")
            return

        # Simple static file server for any other assets
        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            fp = STATIC_DIR / rel
            if fp.is_file() and fp.resolve().is_relative_to(STATIC_DIR.resolve()):
                ctype = "text/plain"
                if rel.endswith(".css"):
                    ctype = "text/css"
                elif rel.endswith(".js"):
                    ctype = "application/javascript"
                elif rel.endswith(".png"):
                    ctype = "image/png"
                self._send(200, fp.read_bytes(), ctype)
                return

        self._send(404, "Not found")

    def log_message(self, fmt, *args):
        pass  # quiet


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    host = "0.0.0.0"
    # Cloud platforms (Railway, Render, Fly, Heroku…) set PORT
    port = int(os.environ.get("PORT", "8765"))
    lan_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    print(f"Otroligt Bot v4 running")
    print(f"  Local : http://127.0.0.1:{port}")
    print(f"  LAN   : http://{lan_ip}:{port}")
    print(f"  Data  : {DATA_DIR}")
    if AUTH_ENABLED:
        print(f"  Auth  : ENABLED (user={AUTH_USER})")
    else:
        print(f"  Auth  : disabled  (set AUTH_USER + AUTH_PASS to protect)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
