"""NIFTY 50 daily momentum scanner.

Scans all NIFTY 50 constituents, computes technical momentum scores from
Yahoo Finance daily data, and picks the day's top bullish and bearish
candidates. The pick is persisted to ``picks.json`` so it stays stable for
the trading day; a rescan can be triggered manually.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from typing import Any, Optional

from agent import _ema, _macd, _momentum_score, _news_sentiment, _rsi, _sma

PICKS_FILE = "picks.json"
SCAN_WORKERS = 10

# NIFTY 50 constituents (NSE tickers)
NIFTY50 = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "ITC", "SBIN",
    "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK", "ASIANPAINT", "MARUTI",
    "SUNPHARMA", "TITAN", "ULTRACEMCO", "NESTLEIND", "WIPRO", "NTPC",
    "POWERGRID", "HCLTECH", "TMPV", "BAJFINANCE", "ADANIENT",
    "ADANIPORTS", "COALINDIA", "BPCL", "EICHERMOT", "GRASIM", "CIPLA",
    "DRREDDY", "HINDUNILVR", "ITC", "JSWSTEEL", "TATASTEEL", "BAJAJFINSV",
    "BAJAJ-AUTO", "HEROMOTOCO", "BRITANNIA", "TECHM", "HINDALCO",
    "APOLLOHOSP", "SHRIRAMFIN", "INDUSINDBK", "SBILIFE", "HDFCLIFE",
    "BAJAJHLDNG", "M&M", "TATACONSUM", "DIVISLAB", "ETERNAL",
]

# dedupe while preserving order
NIFTY50 = list(dict.fromkeys(NIFTY50))

# Broader liquid NSE universe for the "active stocks" scan (~NIFTY 100)
ACTIVE_STOCKS = NIFTY50 + [
    "DMART", "PIDILITIND", "CHOLAFIN", "MUTHOOTFIN", "TRENT", "OFSS",
    "PERSISTENT", "COFORGE", "POLICYBZR", "IRCTC", "HAL", "BEL",
    "MAZDOCK", "BDL", "NBCC", "RVNL", "IRFC", "PFC", "RECLTD", "HUDCO",
    "CONCOR", "INDIGO", "NYKAA", "PAYTM", "IXIGO", "DELHIVERY", "TVSMOTOR",
    "MOTHERSON", "BOSCHLTD", "SIEMENS", "ABB", "CGPOWER", "TORNTPHARM",
    "AUROPHARMA", "LUPIN", "BIOCON", "MAXHEALTH", "FORTIS", "DLF", "GODREJPROP",
    "PRESTIGE", "OBEROIRLTY", "PHOENIXLTD", "MGL", "VBL", "PAGEIND",
    "CUMMINSIND", "THERMAX", "BHEL", "SIEMENS", "HONAUT",
]
ACTIVE_STOCKS = list(dict.fromkeys(ACTIVE_STOCKS))


def _trade_targets(entry: dict, bullish: bool) -> Optional[dict]:
    """Rule-based T1/T2/SL with holding timeframes, matching agent.py logic."""
    cmp_price = (entry.get("quote") or {}).get("ltp") or (entry.get("technicals") or {}).get("cmp")
    if not cmp_price:
        return None
    if bullish:
        return {
            "t1": round(cmp_price * 1.04, 2), "t1_tf": "2–4 sessions",
            "t2": round(cmp_price * 1.09, 2), "t2_tf": "5–10 sessions",
            "sl": round(cmp_price * 0.98, 2),
        }
    return {
        "t1": round(cmp_price * 0.96, 2), "t1_tf": "2–4 sessions",
        "t2": round(cmp_price * 0.91, 2), "t2_tf": "5–10 sessions",
        "sl": round(cmp_price * 1.02, 2),
    }


class NiftyScanner:
    """Scans a symbol universe and tracks today's picks."""

    def __init__(
        self,
        logger: logging.Logger,
        symbols: Optional[list[str]] = None,
        picks_file: str = PICKS_FILE,
    ) -> None:
        self._logger = logger
        self._symbols = symbols or NIFTY50
        self._picks_file = picks_file
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"status": "idle"}
        self._picks: Optional[dict] = self._load_picks()
        self._prev_prices: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load_picks(self) -> Optional[dict]:
        try:
            data = json.loads(open(self._picks_file, encoding="utf-8").read())
            if data.get("date") == date.today().isoformat():
                return data
        except Exception:
            pass
        return None

    def _save_picks(self, picks: dict) -> None:
        try:
            with open(self._picks_file, "w", encoding="utf-8") as f:
                json.dump(picks, f, indent=2)
        except Exception as exc:
            self._logger.warning("Could not save picks: %s", exc)

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------
    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start_scan(self) -> bool:
        """Kick off a background scan. Returns False if already running."""
        with self._lock:
            if self._state.get("status") == "running":
                return False
            self._state = {"status": "running", "progress": "0/50", "error": ""}
        threading.Thread(target=self._scan_worker, name="nifty-scanner", daemon=True).start()
        return True

    def ensure_today(self) -> None:
        """Auto-scan on startup if no pick exists for today."""
        if self._picks is None:
            self._logger.info("No pick for today — starting NIFTY 50 scan.")
            self.start_scan()

    def _scan_worker(self) -> None:
        import yfinance as yf

        results: list[dict] = []
        done = 0
        total = len(self._symbols)

        def fetch(sym: str) -> Optional[dict]:
            try:
                tk = yf.Ticker(f"{sym}.NS")
                hist = tk.history(period="6mo", interval="1d", auto_adjust=False)
                if hist is None or hist.empty or len(hist) < 60:
                    return None
                closes = [float(c) for c in hist["Close"].dropna()]
                volumes = (
                    [float(v) for v in hist["Volume"].dropna()]
                    if "Volume" in hist.columns else []
                )
                tech = {
                    "cmp": closes[-1],
                    "rsi_14": _rsi(closes),
                    "ema_20": _ema(closes, 20),
                    "ema_50": _ema(closes, 50),
                    "sma_200": _sma(closes, 200),
                    "macd": _macd(closes),
                    "avg_volume_20d": (
                        round(sum(volumes[-20:]) / min(20, len(volumes)))
                        if volumes else None
                    ),
                    "latest_volume": int(volumes[-1]) if volumes else None,
                    "volume_vs_20d_avg": (
                        round(volumes[-1] / (sum(volumes[-20:]) / min(20, len(volumes))), 2)
                        if volumes and len(volumes) >= 20 else None
                    ),
                }
                # today's quote for day change
                quote = {"ltp": closes[-1], "change_pct": None}
                try:
                    fi = tk.fast_info
                    if getattr(fi, "previous_close", None):
                        quote["change_pct"] = round(
                            (closes[-1] / float(fi.previous_close) - 1) * 100, 2
                        )
                except Exception:
                    pass
                entry = {
                    "symbol": sym,
                    "kind": "EQ",
                    "quote": quote,
                    "technicals": tech,
                    "news": [],
                }
                entry["momentum_score"] = _momentum_score(entry)
                return entry
            except Exception as exc:
                self._logger.debug("Scan fetch failed for %s: %S", sym, exc)
                return None

        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            futures = {pool.submit(fetch, sym): sym for sym in self._symbols}
            for fut in as_completed(futures):
                r = fut.result()
                if r:
                    results.append(r)
                done += 1
                with self._lock:
                    self._state["progress"] = f"{done}/{total}"

        results.sort(key=lambda e: e["momentum_score"], reverse=True)

        picks = {
            "date": date.today().isoformat(),
            "scanned": len(results),
            "bullish": results[0] if results else None,
            "bearish": results[-1] if results else None,
            "top5": [
                {"symbol": e["symbol"], "score": e["momentum_score"], "cmp": e["technicals"]["cmp"]}
                for e in results[:5]
            ],
            "bottom5": [
                {"symbol": e["symbol"], "score": e["momentum_score"], "cmp": e["technicals"]["cmp"]}
                for e in results[-5:]
            ],
        }
        # flag bearish honestly
        if picks["bearish"] and picks["bearish"]["momentum_score"] > 0:
            picks["bearish_note"] = "No confirmed bearish setup — weakest momentum shown."

        with self._lock:
            self._picks = picks
            self._state = {"status": "done", "progress": f"{len(results)}/{total}", "error": ""}
        self._save_picks(picks)
        self._logger.info(
            "NIFTY scan complete: %d symbols. Bullish=%s Bearish=%s",
            len(results),
            picks["bullish"]["symbol"] if picks["bullish"] else "-",
            picks["bearish"]["symbol"] if picks["bearish"] else "-",
        )

    # ------------------------------------------------------------------
    # Live pick view (with fresh quote for movement)
    # ------------------------------------------------------------------
    def today_picks(self) -> dict:
        """Return today's picks with refreshed live prices."""
        if self._picks is None or self._picks.get("date") != date.today().isoformat():
            return {"date": date.today().isoformat(), "status": "none", "picks": None}

        picks = json.loads(json.dumps(self._picks))  # deep copy
        for key, bullish in (("bullish", True), ("bearish", False)):
            p = picks.get(key)
            if p:
                self._refresh_quote(p)
                p["targets"] = _trade_targets(p, bullish)
        return {"date": picks["date"], "status": "ready", "picks": picks}

    def _refresh_quote(self, entry: dict) -> None:
        """Refresh LTP/change for a pick and compute movement vs last poll."""
        import yfinance as yf

        sym = entry["symbol"]
        try:
            fi = yf.Ticker(f"{sym}.NS").fast_info
            ltp = float(fi.last_price)
            prev = float(fi.previous_close) if fi.previous_close else None
            entry["quote"]["ltp"] = round(ltp, 2)
            if prev:
                entry["quote"]["change_pct"] = round((ltp / prev - 1) * 100, 2)
            # intraday movement vs previous poll
            prev_ltp = self._prev_prices.get(sym)
            entry["movement"] = (
                round(ltp - prev_ltp, 2) if prev_ltp else 0.0
            )
            self._prev_prices[sym] = ltp
        except Exception as exc:
            self._logger.debug("Pick quote refresh failed for %s: %s", sym, exc)
