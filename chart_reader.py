"""Chart Reader Agent — interactive candlestick-pattern screener for NSE.

Guides the user through a 4-step menu (universe / timeframe / pattern
group / confidence threshold), scans completed candles only, scores each
setup 0-100 with the weighted model (geometry 45%, trend+levels 35%,
volume 20%), and returns a structured markdown report.

Pattern rules are mathematical OHLC approximations of the Groww
candlestick-pattern reference — not exhaustive; zero matches are reported
honestly rather than forced.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("chart_reader")

# ---------------------------------------------------------------------------
# Phase 1 — selection menu
# ---------------------------------------------------------------------------

UNIVERSES = {
    "1A": ("Nifty 50", "nifty50"),
    "1B": ("Nifty ~100 (50 + active large/mid)", "active"),
    "1C": ("Broad multi-cap (~180)", "broad"),
    "1D": ("Multicap (symbols_multicap.json)", "multicap"),
    "1E": ("Smallcap (symbols_smallcap.json)", "smallcap"),
}

TIMEFRAMES = {
    "2A": ("5m", "5m", "1mo"),
    "2B": ("15m", "15m", "1mo"),
    "2C": ("1h", "1h", "3mo"),
    "2D": ("1d", "1d", "1y"),
}

PATTERN_GROUPS = {
    "3A": "all",
    "3B": "bullish",
    "3C": "bearish",
    "3D": "continuation",
}

THRESHOLDS = {"4A": 75.0, "4B": 80.0, "4C": 90.0}

BULLISH_PATTERNS = {"hammer", "inverted_hammer", "bullish_engulfing",
                    "piercing", "morning_star", "three_white_soldiers",
                    "bullish_harami"}
BEARISH_PATTERNS = {"shooting_star", "hanging_man", "bearish_engulfing",
                    "dark_cloud", "evening_star", "three_black_crows",
                    "bearish_harami"}
NEUTRAL_PATTERNS = {"marubozu", "doji", "spinning_top", "rising_three",
                    "falling_three"}

GROUP_MAP = {
    "all": BULLISH_PATTERNS | BEARISH_PATTERNS | NEUTRAL_PATTERNS,
    "bullish": BULLISH_PATTERNS,
    "bearish": BEARISH_PATTERNS,
    "continuation": NEUTRAL_PATTERNS,
}

MENU_TEXT = """**Chart Reader — setup menu** (reply e.g. `1B, 2B, 3B, 4B`)

1. Universe: [1A] Nifty 50 · [1B] ~Nifty 100 · [1C] Broad · [1D] Broad · [1E] All · [1F] Multicap file · [1G] Smallcap file
2. Timeframe: [2A] 5m · [2B] 15m · [2C] 1h · [2D] 1d
3. Patterns: [3A] All · [3B] Bullish reversal · [3C] Bearish reversal · [3D] Continuation/indecision
4. Min confidence: [4A] >=75% · [4B] >=80% · [4C] >=90%"""


# ---------------------------------------------------------------------------
# Pattern detection — mathematical OHLC rules on completed candles
# ---------------------------------------------------------------------------

def _rng(c):
    return max(c["high"] - c["low"], 1e-9)


def _body(c):
    return abs(c["close"] - c["open"])


def _upper_wick(c):
    return c["high"] - max(c["open"], c["close"])


def _lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]


def detect_patterns(candles: list[dict]) -> list[dict]:
    """Return pattern hits on the last completed candle(s)."""
    if len(candles) < 3:
        return []
    c, p1, p2 = candles[-1], candles[-2], candles[-3]
    hits = []
    rng, body = _rng(c), _body(c)
    # decisive-body gate: rejects doji/spinning-top-like bars for patterns
    # that require real directional conviction (engulfing, marubozu, stars,
    # soldiers/crows, three-methods). Wick patterns (hammer etc.) exempt.
    decisive = rng > 0 and (body / rng) >= 0.60
    uw, lw = _upper_wick(c), _lower_wick(c)
    bull = c["close"] > c["open"]
    bear = c["close"] < c["open"]

    # single-candle wick patterns — small body is the point, no decisive gate
    if body <= rng * 0.1:
        pass  # doji rejected — no edge
    if lw >= body * 2 and uw <= body * 0.5 and bull:
        hits.append(("hammer", "bullish", 0.85))
    if uw >= body * 2 and lw <= body * 0.5 and bear:
        hits.append(("shooting_star", "bearish", 0.85))
    if lw >= body * 2 and uw <= body * 0.5 and bear:
        hits.append(("hanging_man", "bearish", 0.7))
    if uw >= body * 2 and lw <= body * 0.5 and bull:
        hits.append(("inverted_hammer", "bullish", 0.7))
    if decisive and body >= rng * 0.9:
        hits.append(("marubozu", "bullish" if bull else "bearish", 0.8))
    # spinning_top rejected per spec (weak body, no direction)

    # two-candle — decisive gate on engulfing/piercing/dark-cloud signal candle
    p1_bull, p1_bear = p1["close"] > p1["open"], p1["close"] < p1["open"]
    if decisive and p1_bear and bull and c["open"] <= p1["close"] and c["close"] >= p1["open"]:
        hits.append(("bullish_engulfing", "bullish", 0.9))
    if decisive and p1_bull and bear and c["open"] >= p1["close"] and c["close"] <= p1["open"]:
        hits.append(("bearish_engulfing", "bearish", 0.9))
    if decisive and p1_bear and bull and p1["close"] < p1["open"] and c["open"] < p1["close"] \
            and c["close"] > (p1["open"] + p1["close"]) / 2 and c["close"] < p1["open"]:
        hits.append(("piercing", "bullish", 0.8))
    if decisive and p1_bull and bear and p1["close"] > p1["open"] and c["open"] > p1["close"] \
            and c["close"] < (p1["open"] + p1["close"]) / 2 and c["close"] > p1["open"]:
        hits.append(("dark_cloud", "bearish", 0.8))
    # harami: small body inside prior large body (exempt — small body is required)
    if body < _body(p1) * 0.5 and c["high"] <= p1["high"] and c["low"] >= p1["low"]:
        hits.append(("bullish_harami" if p1_bear else "bearish_harami",
                     "bullish" if p1_bear else "bearish", 0.65))

    # three-candle stars — signal candle should be decisive
    if decisive and bear and p1["close"] < p1["open"] and p2["close"] > p2["open"] \
            and p1["close"] < p2["open"] * 0.998:
        hits.append(("evening_star", "bearish", 0.75))
    if decisive and bull and p1["close"] > p1["open"] and p2["close"] < p2["open"] \
            and p1["close"] > p2["open"] * 1.002:
        hits.append(("morning_star", "bullish", 0.75))

    # three soldiers / crows — all three bodies must be decisive
    if all(x["close"] > x["open"] for x in (p2, p1, c)) and \
            all(_body(x) > _rng(x) * 0.6 for x in (p2, p1, c)):
        hits.append(("three_white_soldiers", "bullish", 0.85))
    if all(x["close"] < x["open"] for x in (p2, p1, c)) and \
            all(_body(x) > _rng(x) * 0.6 for x in (p2, p1, c)):
        hits.append(("three_black_crows", "bearish", 0.85))

    # rising / falling three methods — final candle decisive
    if decisive and p2["close"] > p2["open"] and c["close"] > c["open"] and \
            p1["close"] < p1["open"] and _body(p1) < _body(p2) * 0.5 \
            and p1["low"] > p2["open"]:
        hits.append(("rising_three", "bullish", 0.7))
    if decisive and p2["close"] < p2["open"] and c["close"] < c["open"] and \
            p1["close"] > p1["open"] and _body(p1) < _body(p2) * 0.5 \
            and p1["high"] < p2["open"]:
        hits.append(("falling_three", "bearish", 0.7))

    return [{"pattern": n, "bias": b, "geom": g} for n, b, g in hits]


# ---------------------------------------------------------------------------
# Confidence scoring — geometry 45% + trend/levels 35% + volume 20%
# ---------------------------------------------------------------------------

def _ema(vals: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def score_setup(candles: list[dict], hit: dict, timeframe: str) -> tuple[float, str]:
    """Return (confidence 0-100, justification string)."""
    c = candles[-1]
    closes = [x["close"] for x in candles]
    vols = [x.get("volume") or 0 for x in candles]

    # --- geometry (45%) ---
    geom = hit["geom"] * 100

    # --- trend & level alignment (35%) ---
    trend_score = 50.0
    just = []
    e20 = vwap = None
    if len(closes) >= 10:
        e20 = _ema(closes, 20)[-1]
    if timeframe in {"5m", "15m", "1h"}:
        pv = sum(((x["high"] + x["low"] + x["close"]) / 3) * (x.get("volume") or 0) for x in candles[-78:])
        vv = sum(x.get("volume") or 0 for x in candles[-78:])
        if vv > 0:
            vwap = pv / vv

    if e20 is not None or vwap is not None:
        above_ema = e20 is not None and c["close"] > e20
        above_vwap = vwap is not None and c["close"] > vwap
        # trend-alignment: bearish REVERSALS must reverse an upswing
        # (close above EMA20 or VWAP); bearish CONTINUATIONS are allowed below.
        BEAR_REV = {"shooting_star", "bearish_engulfing", "evening_star",
                    "dark_cloud", "bearish_harami", "hanging_man"}
        BEAR_CONT = {"three_black_crows", "falling_three"}
        pat = hit.get("pattern", "")
        if pat in BEAR_REV:
            upswing = above_ema or above_vwap
            if not upswing:
                trend_score = 15.0
                just.append("bearish rev but below EMA20/VWAP — no upswing to reverse")
            else:
                trend_score = 85.0
                just.append("reversing upswing (above EMA/VWAP)")
        elif pat in BEAR_CONT:
            trend_score = 70.0 if (not above_ema and not above_vwap) else 40.0
            just.append("bearish continuation" + (" below levels" if trend_score == 70.0 else ""))
        else:
            want_up = hit["bias"] == "bullish"
            aligned = (above_ema if e20 is not None else above_vwap)
            if want_up == aligned:
                trend_score = 85.0
            else:
                trend_score = 25.0
            ref = "EMA20" if e20 is not None else "VWAP"
            just.append(f"{'above' if aligned else 'below'} {ref}")
        # prior 5-candle momentum agrees?
        mom = (closes[-1] / closes[-6] - 1) * 100 if len(closes) >= 6 else 0
        if (hit["bias"] == "bullish" and mom > 0) or (hit["bias"] == "bearish" and mom < 0) \
                or hit["bias"] == "neutral":
            trend_score = min(100.0, trend_score + 10)
    else:
        just.append("insufficient history")

    # --- volume confirmation (20%) ---
    vol_score = 50.0
    if len(vols) >= 21:
        avg = sum(vols[-21:-1]) / 20
        if avg > 0:
            ratio = vols[-1] / avg
            vol_score = min(100.0, ratio * 50)  # 1x avg -> 50, 2x -> 100
            just.append(f"vol {ratio:.1f}x avg")

    conf = round(geom * 0.45 + trend_score * 0.35 + vol_score * 0.20, 1)

    # closing-auction guard: 15:00-15:30 IST volume spikes are square-off
    # noise — cap confidence so they can't push a setup over 80%
    if timeframe in {"5m", "15m"}:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        ist_min = datetime.fromtimestamp(c["time"], ZoneInfo("Asia/Kolkata")).hour * 60 \
            + datetime.fromtimestamp(c["time"], ZoneInfo("Asia/Kolkata")).minute
        if ist_min >= 15 * 60:
            if conf > 80.0:
                just.append("close-auction volume capped")
            conf = min(conf, 80.0)
        if vwap is not None:
            just.append(f"vs VWAP {vwap:.2f}")

    return conf, "; ".join(just)


def _atr(candles: list[dict], n: int = 14) -> float:
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / min(n, len(trs)) if trs else 0.0


def _levels(candles: list[dict], bias: str) -> dict:
    """SL + T1/T2/T3 in R-multiples of ATR(14) from signal close."""
    c = candles[-1]
    entry = c["close"]
    risk = max(_atr(candles), _rng(c))
    if risk <= 0 or bias == "neutral":
        return {}
    m = 1 if bias == "bullish" else -1
    return {
        "sl": round(entry - m * risk, 2),
        "t1": round(entry + m * risk * 1.0, 2),
        "t2": round(entry + m * risk * 2.0, 2),
        "t3": round(entry + m * risk * 3.0, 2),
    }


# ---------------------------------------------------------------------------
# Data fetch + scan
# ---------------------------------------------------------------------------

def _symbols_for(key: str) -> list[str]:
    """Resolve a universe key to symbol list.

    'multicap'/'smallcap' load symbols_<key>.json (a JSON array of NSE
    tickers) so real Nifty Midcap/Smallcap constituent lists can be
    dropped in without code changes. Falls back to the broad list.
    """
    import json
    import os
    from scanner import NIFTY50, ACTIVE_STOCKS, BROAD_STOCKS

    builtin = {"nifty50": NIFTY50, "active": ACTIVE_STOCKS, "broad": BROAD_STOCKS}
    if key in builtin:
        return builtin[key]
    path = f"symbols_{key}.json"
    if os.path.exists(path):
        try:
            syms = [s.strip().upper() for s in json.loads(open(path, encoding="utf-8").read()) if s.strip()]
            if syms:
                return syms
        except Exception as exc:
            logger.warning("Could not load %s: %s", path, exc)
    return BROAD_STOCKS


def _fetch(sym: str, interval: str, period: str) -> Optional[list[dict]]:
    try:
        import yfinance as yf
        df = yf.Ticker(f"{sym}.NS").history(period=period, interval=interval, auto_adjust=False)
        if df is None or df.empty or len(df) < 30:
            return None
        # drop the currently open/incomplete candle
        df = df.iloc[:-1] if interval != "1d" or True else df
        out = []
        for ts, r in df.iterrows():
            out.append({
                "time": int(ts.timestamp()), "open": float(r["Open"]),
                "high": float(r["High"]), "low": float(r["Low"]),
                "close": float(r["Close"]),
                "volume": float(r["Volume"]) if r.get("Volume") == r.get("Volume") else 0.0,
            })
        return out
    except Exception:
        return None


def run_scan(universe_key: str, tf_key: str, group: str,
             threshold: float, logger: Optional[logging.Logger] = None) -> str:
    """Execute a configured scan; returns markdown report."""
    log = logger or logging.getLogger("chart_reader")
    _, interval, period = TIMEFRAMES[tf_key]
    tf_label = TIMEFRAMES[tf_key][0]
    group_name = {"all": "All patterns", "bullish": "Bullish reversals",
                  "bearish": "Bearish reversals",
                  "continuation": "Continuation/indecision"}[group]
    allowed = GROUP_MAP[group]
    symbols = _symbols_for(UNIVERSES[universe_key][1])
    results = []

    for sym in symbols:
        candles = _fetch(sym, interval, period)
        if not candles:
            continue
        for hit in detect_patterns(candles):
            if hit["pattern"] not in allowed:
                continue
            conf, why = score_setup(candles, hit, interval)
            if conf >= threshold:
                c = candles[-1]
                results.append({
                    "symbol": sym, "pattern": hit["pattern"], "bias": hit["bias"],
                    "confidence": conf, "time": c["time"],
                    "ohlc": f"O{c['open']:.2f} H{c['high']:.2f} L{c['low']:.2f} C{c['close']:.2f}",
                    "vol": c.get("volume") or 0, "why": why,
                    **_levels(candles, hit["bias"]),
                })

    results.sort(key=lambda r: r["confidence"], reverse=True)
    uni_name = UNIVERSES[universe_key][0]

    if not results:
        return (f"### Chart Reader — no setups found\n\n"
                f"Universe **{uni_name}** · TF **{tf_label}** · Group **{group_name}** · "
                f"threshold >= {threshold:.0f}%\n\n"
                "No completed-candle pattern met the confidence bar. "
                "Try a lower threshold ([4A]) or a different timeframe.")

    lines = [
        f"### Chart Reader — {len(results)} setup(s)\n",
        f"*Universe {uni_name} · TF {tf_label} · {group_name} · >= {threshold:.0f}% · "
        f"{len(symbols)} symbols scanned, completed candles only*\n",
        "| Ticker | Pattern | Bias | Conf % | Time (IST) | SL | T1 | T2 | T3 | OHLC / Level / Volume |",
        "| :--- | :--- | :--- | ---: | :--- | ---: | ---: | ---: | ---: | :--- |",
    ]
    from datetime import datetime
    from zoneinfo import ZoneInfo
    for r in results[:25]:
        t = datetime.fromtimestamp(r["time"], ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M")
        lv = (f"{r.get('sl','—')} | {r.get('t1','—')} | {r.get('t2','—')} | {r.get('t3','—')}"
              if r.get("sl") is not None else "— | — | — | —")
        lines.append(
            f"| {r['symbol']} | {r['pattern']} | {r['bias']} | {r['confidence']}% "
            f"| {t} | {lv} | {r['ohlc']} · {r['why']} · vol {r['vol']:.0f} |"
        )
    lines.append("\n**Post-scan:** [R] re-run same settings · [F] filter further "
                 "(RSI/overbought) · [C] change options · [D] deep-dive a ticker")
    return "\n".join(lines)
