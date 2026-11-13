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

from agent import (
    _ema, _intraday_metrics, _macd, _momentum_score, _news_sentiment, _rsi, _sma,
    confidence_pct, trend_bias,
)

PICKS_FILE = "picks.json"
PICK_HISTORY_FILE = "pick_history.json"
TUNING_FILE = "agent_tuning.json"
SCAN_WORKERS = 10


def _load_tuning() -> dict:
    """Load performance-agent tuning params, if present."""
    try:
        return json.loads(open(TUNING_FILE, encoding="utf-8").read())
    except Exception:
        return {}

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

# Broad multi-cap universe for the momentum radar (large + mid + small caps)
BROAD_STOCKS = ACTIVE_STOCKS + [
    # PSU banks & financials
    "BANKBARODA", "PNB", "CANBK", "UNIONBANK", "INDIANB", "BOB", "SYNDIBANK",
    "UJJIVANSFB", "RBLBANK", "IDFCFIRSTB", "M&MFIN", "CHOLAFIN", "MUTHOOTFIN",
    "MANAPPURAM", "PFC", "RECLTD", "IRFC", "HUDCO", "SBICARD", "HDFCAMC",
    # Industrials & capital goods
    "BHEL", "THERMAX", "CUMMINSIND", "BEML", "BDL", "RVNL", "IRCTC", "HAL",
    "BEL", "MAZDOCK", "NBCC", "CONCOR", "GMRAIRPORT", "TITAGARH", "ASHOKLEY",
    "BALKRISIND", "MOTHERSON", "BOSCHLTD", "SIEMENS", "ABB", "CGPOWER",
    "HONAUT", "TRENT", "PAGEIND", "RELAXO", "BATAINDIA", "KPRMILL",
    # Consumption & retail
    "JUBLFOOD", "WESTLIFE", "VBL", "TRENT", "DMART", "PIDILITIND", "MRF",
    "BATAINDIA", "RELAXO", "PAGEIND", "KPRMILL", "TATACONSUM", "BRITANNIA",
    "HINDUNILVR", "ITC", "NESTLEIND", "COLPAL", "MARICO", "DABUR",
    "GODREJCP", "EMAMILTD", "AWL", "PCJEWELLER", "VAIBHAVGLOBAL",
    # IT & tech midcaps
    "TATAELXSI", "KPITTECH", "MPHASIS", "HAPPSTMNDS", "COFORGE", "PERSISTENT",
    "LTIM", "OFSS", "ZENSAR", "COCHINSHIP", "GESHIP", "MAZAGON", "BEL",
    "ASTRAMICRO", "TANLA", "INTELLECT", "YATHARTH", "MTARTECH", "AXITA",
    # Energy & commodities
    "SUZLON", "WAAREEENER", "ADANIGREEN", "ADANIPOWER", "TORNTPHARM",
    "DEEPAKNITR", "KALYANKJIL", "JINDALSAW", "WELCORP", "UJJIVANSFB",
    "IDEA", "YESBANK", "RCOM", "TATASTEEL", "JSWSTEEL", "HINDALCO",
    "VEDL", "COALINDIA", "NMDC", "SAIL", "HINDZINC", "MOIL",
    # Pharma & healthcare
    "LUPIN", "BIOCON", "MAXHEALTH", "FORTIS", "LALPATHLAB", "METROPOLIS",
    "THYROCARE", "KRISHANA", "SMSPHARMA", "APEX", "AAREYDRUG",
    # Realty & others
    "DLF", "GODREJPROP", "PRESTIGE", "OBEROIRLTY", "PHOENIXLTD", "MGL",
    "BRIGADE", "SOBHA", "NIGHTINTERM", "HUDCO", "INDIAMART",
    "TANLA", "ONE97", "PAYTM", "ZOMATO", "NYKAA", "IXIGO", "DELHIVERY",
    "BLUESTONE", "CARTRADE", "POLICYBZR", "MCX", "BSE",
]
BROAD_STOCKS = list(dict.fromkeys(BROAD_STOCKS))


def _trade_targets(entry: dict, bullish: bool) -> Optional[dict]:
    """Rule-based T1/T2/SL with holding timeframes, matching agent.py logic."""
    cmp_price = (entry.get("quote") or {}).get("ltp") or (entry.get("technicals") or {}).get("cmp")
    if not cmp_price:
        return None
    if bullish:
        return {
            "t1": round(cmp_price * 1.008, 2), "t1_tf": "15–30 min",
            "t2": round(cmp_price * 1.015, 2), "t2_tf": "1–2 hrs",
            "sl": round(cmp_price * 0.995, 2),
        }
    return {
        "t1": round(cmp_price * 0.992, 2), "t1_tf": "15–30 min",
        "t2": round(cmp_price * 0.985, 2), "t2_tf": "1–2 hrs",
        "sl": round(cmp_price * 1.005, 2),
    }


class NiftyScanner:
    """Scans a symbol universe and tracks today's picks."""

    def __init__(
        self,
        logger: logging.Logger,
        symbols: Optional[list[str]] = None,
        picks_file: str = PICKS_FILE,
        source: str = "nifty",
    ) -> None:
        self._logger = logger
        self._symbols = symbols or NIFTY50
        self._picks_file = picks_file
        self._source = source
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
        elif self._picks:
            # ensure today's already-made picks are in the performance history
            self._record_history(self._picks)

    def backfill_history(self) -> None:
        """Record today's loaded picks into history (idempotent)."""
        if self._picks:
            self._record_history(self._picks)

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
                try:
                    entry["intraday"] = _intraday_metrics(tk)
                except Exception:
                    entry["intraday"] = {}
                entry["momentum_score"] = _momentum_score(entry)
                entry["trend_bias"] = trend_bias(entry["momentum_score"])
                entry["confidence"] = confidence_pct(entry)
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

        # Apply performance-agent tuning thresholds (agent_tuning.json)
        tuning = _load_tuning()
        min_conf = tuning.get("min_confidence", 0)
        min_score = tuning.get("min_abs_score", 0)
        min_vol = tuning.get("min_volume_ratio")
        src_liquidity = tuning.get(f"{self._source}_liquidity_filter")

        def eligible(e: dict) -> bool:
            tech = e.get("technicals") or {}
            if min_conf and (e.get("confidence") or 0) < min_conf:
                return False
            if min_score and abs(e.get("momentum_score") or 0) < min_score:
                return False
            if min_vol and (tech.get("volume_vs_20d_avg") or 0) < min_vol:
                return False
            if src_liquidity and (tech.get("latest_volume") or 0) < 500_000:
                return False
            return True

        pool = [e for e in results if eligible(e)] or results

        bull_min = tuning.get("bullish_min_confidence", 0)
        bear_min = tuning.get("bearish_min_abs_score", 0)

        bullish_candidates = [e for e in pool if e["momentum_score"] > 0]
        if bull_min:
            bullish_candidates = [
                e for e in bullish_candidates if (e.get("confidence") or 0) >= bull_min
            ] or bullish_candidates

        bearish_candidates = [e for e in pool if e["momentum_score"] < 0]
        if bear_min:
            bearish_candidates = [
                e for e in bearish_candidates if e["momentum_score"] <= -bear_min
            ]

        picks = {
            "date": date.today().isoformat(),
            "scanned": len(results),
            "bullish": max(bullish_candidates, key=lambda e: e["momentum_score"], default=None) or (results[0] if results else None),
            "bearish": min(bearish_candidates, key=lambda e: e["momentum_score"], default=None) or (results[-1] if results else None),
            "top5": [
                {
                    "symbol": e["symbol"], "score": e["momentum_score"],
                    "bias": e.get("trend_bias"), "confidence": e.get("confidence"),
                    "cmp": e["technicals"]["cmp"],
                }
                for e in results[:5]
            ],
            "bottom5": [
                {
                    "symbol": e["symbol"], "score": e["momentum_score"],
                    "bias": e.get("trend_bias"), "confidence": e.get("confidence"),
                    "cmp": e["technicals"]["cmp"],
                }
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
        self._record_history(picks)
        self._logger.info(
            "NIFTY scan complete: %d symbols. Bullish=%s Bearish=%s",
            len(results),
            picks["bullish"]["symbol"] if picks["bullish"] else "-",
            picks["bearish"]["symbol"] if picks["bearish"] else "-",
        )

    # ------------------------------------------------------------------
    # Pick history (for the performance agent)
    # ------------------------------------------------------------------
    def _record_history(self, picks: dict) -> None:
        """Append today's bullish/bearish picks to pick_history.json."""
        try:
            try:
                history = json.loads(open(PICK_HISTORY_FILE, encoding="utf-8").read())
            except Exception:
                history = []

            existing = {
                (h.get("date"), h.get("source"), h.get("side")) for h in history
            }
            for side in ("bullish", "bearish"):
                p = picks.get(side)
                if not p:
                    continue
                key = (picks["date"], self._source, side)
                if key in existing:
                    continue
                entry_price = (p.get("quote") or {}).get("ltp") or (p.get("technicals") or {}).get("cmp")
                history.append({
                    "date": picks["date"],
                    "source": self._source,
                    "side": side,
                    "symbol": p["symbol"],
                    "entry_price": entry_price,
                    "score": p.get("momentum_score"),
                })

            with open(PICK_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history[-500:], f, indent=2)
        except Exception as exc:
            self._logger.warning("Could not record pick history: %s", exc)

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
                # backfill bias/confidence for picks recorded before this feature
                if "trend_bias" not in p:
                    p["trend_bias"] = trend_bias(p.get("momentum_score"))
                if "confidence" not in p:
                    p["confidence"] = confidence_pct(p)
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
