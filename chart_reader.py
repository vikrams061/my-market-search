"""Chart Reader Agent — interactive candlestick-pattern screener for NSE.

4-step menu (universe / timeframe / pattern group / confidence), scans
completed candles only, scores with geometry 65% + time-of-day-normalized
volume 35%, strict trend-location gating, and returns a markdown report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Optional

import pandas as pd

logger = logging.getLogger("chart_reader")

BiasType = Literal["bullish", "bearish", "neutral"]

# ---------------------------------------------------------------------------
# Phase 1 — selection menu
# ---------------------------------------------------------------------------

UNIVERSES = {
    "1A": ("Nifty 50", "nifty50"),
    "1B": ("Small + Midcap (~81)", "smallcap"),
    "1C": ("All Nifty (~100)", "active"),
}

TIMEFRAMES = {
    "2A": ("5m", "5m", "1mo"),
    "2B": ("15m", "15m", "1mo"),
    "2C": ("1h", "1h", "3mo"),
    "2D": ("1d", "1d", "1y"),
}

PATTERN_GROUPS = {"3A": "all", "3B": "bullish", "3C": "bearish", "3D": "continuation"}
THRESHOLDS = {"4A": 75.0, "4B": 80.0, "4C": 90.0}

BULLISH_PATTERNS = {"hammer", "inverted_hammer", "bullish_engulfing", "piercing_line",
                    "morning_star", "three_white_soldiers", "bullish_harami",
                    "tweezer_bottom", "rising_three"}
BEARISH_PATTERNS = {"shooting_star", "hanging_man", "bearish_engulfing", "dark_cloud_cover",
                    "evening_star", "three_black_crows", "bearish_harami",
                    "tweezer_top", "falling_three"}
NEUTRAL_PATTERNS = {"marubozu", "doji"}
GROUP_MAP = {
    "all": BULLISH_PATTERNS | BEARISH_PATTERNS | NEUTRAL_PATTERNS,
    "bullish": BULLISH_PATTERNS,
    "bearish": BEARISH_PATTERNS,
    "continuation": NEUTRAL_PATTERNS | {"rising_three", "falling_three"},
}

MENU_TEXT = """**Chart Reader — setup menu** (reply e.g. `1B, 2B, 3B, 4B`)

1. Universe: [1A] Nifty 50 · [1B] Small + Midcap · [1C] All Nifty (~100)
2. Timeframe: [2A] 5m · [2B] 15m · [2C] 1h · [2D] 1d
3. Patterns: [3A] All · [3B] Bullish reversal · [3C] Bearish reversal · [3D] Continuation/indecision
4. Min confidence: [4A] >=75% · [4B] >=80% · [4C] >=90%"""


# ---------------------------------------------------------------------------
# Core metrics (per spec)
# ---------------------------------------------------------------------------

@dataclass
class PatternSignal:
    ticker: str
    pattern: str
    bias: BiasType
    confidence: float
    timestamp: str
    body_ratio: float
    rel_vol_tod: float


def candle_metrics(row: pd.Series) -> dict:
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    rng = h - l
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    if rng <= 0:
        return {"range": 0.0, "body": 0.0, "body_ratio": 0.0, "upper_ratio": 0.0,
                "lower_ratio": 0.0, "is_bull": False, "is_bear": False}
    return {"range": rng, "body": body, "body_ratio": body / rng,
            "upper_ratio": upper_wick / rng, "lower_ratio": lower_wick / rng,
            "is_bull": c > o, "is_bear": c < o}


def get_trend_regime(close: float, ema20: float, vwap: float, prior_mean: float) -> str:
    """Structural trend location — reversal patterns only fire at extremes."""
    if close > ema20 and close >= vwap and close > prior_mean:
        return "uptrend"
    if close < ema20 and close <= vwap and close < prior_mean:
        return "downtrend"
    return "neutral"


def compute_tod_relative_volume(df: pd.DataFrame, lookback_days: int = 20) -> pd.Series:
    """Volume vs the historical average at that exact time-of-day bucket."""
    tod_bucket = pd.to_datetime(df["timestamp"]).dt.strftime("%H:%M")
    tod_avg = df.groupby(tod_bucket)["volume"].transform(
        lambda s: s.shift(1).rolling(lookback_days, min_periods=5).mean()
    )
    return (df["volume"] / tod_avg).fillna(1.0)


def _score_signal(ticker, pattern, bias, structural_quality, rel_vol_tod, timestamp) -> PatternSignal:
    """Geometry 65% + time-of-day volume 35% — weak bodies can't reach 80% on volume alone."""
    geom_score = min(1.0, max(0.0, (structural_quality - 0.45) / 0.45))
    vol_score = min(1.0, max(0.0, (rel_vol_tod - 0.70) / 1.80))
    raw_conf = (0.65 * geom_score + 0.35 * vol_score) * 100.0
    return PatternSignal(
        ticker=ticker, pattern=pattern, bias=bias,
        confidence=round(min(98.0, raw_conf), 1), timestamp=timestamp,
        body_ratio=round(structural_quality * 100.0, 1),
        rel_vol_tod=round(rel_vol_tod, 2),
    )


# ---------------------------------------------------------------------------
# Comprehensive classifier (completed candles only)
# ---------------------------------------------------------------------------

def classify_comprehensive_patterns(df: pd.DataFrame, ticker: str,
                                    timeframe: str = "15m",
                                    min_atr_mult: float = 0.50) -> list[PatternSignal]:
    if len(df) < 25:
        return []

    curr = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3] if len(df) >= 3 else prev
    m_curr, m_prev = candle_metrics(curr), candle_metrics(prev)
    m_prev2 = candle_metrics(prev2) if len(df) >= 3 else m_prev

    ts_str = str(curr["timestamp"])
    # reject incomplete stub bars on 1h NSE charts
    if timeframe == "1h" and ts_str.endswith("15:15:00"):
        return []

    atr14 = float(curr["atr14"]) if pd.notna(curr.get("atr14")) else 0.0
    if atr14 > 0 and m_curr["range"] < atr14 * min_atr_mult:
        return []

    prior_mean = float(df["close"].iloc[-6:-1].mean())
    trend = get_trend_regime(float(curr["close"]), float(curr["ema20"]),
                             float(curr.get("vwap") or curr["ema20"]), prior_mean)
    rel_vol = float(curr["rel_vol_tod"])
    signals: list[PatternSignal] = []

    # 1. Engulfing — current body >= 1.2x prior, >=60% body ratio
    if m_curr["body_ratio"] >= 0.60 and m_curr["body"] >= m_prev["body"] * 1.20:
        if m_prev["is_bear"] and m_curr["is_bull"]:
            if curr["open"] <= prev["close"] and curr["close"] >= prev["open"]:
                if trend == "downtrend" or (curr["close"] > curr["ema20"] and curr["close"] > curr.get("vwap", 0)):
                    signals.append(_score_signal(ticker, "bullish_engulfing", "bullish", m_curr["body_ratio"], rel_vol, ts_str))
        elif m_prev["is_bull"] and m_curr["is_bear"]:
            if curr["open"] >= prev["close"] and curr["close"] <= prev["open"]:
                if trend == "uptrend":
                    signals.append(_score_signal(ticker, "bearish_engulfing", "bearish", m_curr["body_ratio"], rel_vol, ts_str))

    # 2. Morning / Evening Star
    if len(df) >= 3:
        c1_mid = prev2["open"] - (m_prev2["body"] / 2.0)
        if (trend == "downtrend" and m_prev2["is_bear"] and m_prev2["body_ratio"] >= 0.65
                and m_prev["body_ratio"] <= 0.35 and m_curr["is_bull"]
                and m_curr["body_ratio"] >= 0.60 and curr["close"] >= c1_mid):
            signals.append(_score_signal(ticker, "morning_star", "bullish", m_curr["body_ratio"], rel_vol, ts_str))
        c1_mid_bull = prev2["open"] + (m_prev2["body"] / 2.0)
        if (trend == "uptrend" and m_prev2["is_bull"] and m_prev2["body_ratio"] >= 0.65
                and m_prev["body_ratio"] <= 0.35 and m_curr["is_bear"]
                and m_curr["body_ratio"] >= 0.60 and curr["close"] <= c1_mid_bull):
            signals.append(_score_signal(ticker, "evening_star", "bearish", m_curr["body_ratio"], rel_vol, ts_str))

    # 3. Piercing Line / Dark Cloud Cover
    if m_prev["body_ratio"] >= 0.65 and m_curr["body_ratio"] >= 0.60:
        if trend == "downtrend" and m_prev["is_bear"] and m_curr["is_bull"]:
            c1_mid = prev["open"] - (m_prev["body"] / 2.0)
            if curr["open"] < prev["low"] and curr["close"] >= c1_mid:
                signals.append(_score_signal(ticker, "piercing_line", "bullish", m_curr["body_ratio"], rel_vol, ts_str))
        if trend == "uptrend" and m_prev["is_bull"] and m_curr["is_bear"]:
            c1_mid_bull = prev["open"] + (m_prev["body"] / 2.0)
            if curr["open"] > prev["high"] and curr["close"] <= c1_mid_bull:
                signals.append(_score_signal(ticker, "dark_cloud_cover", "bearish", m_curr["body_ratio"], rel_vol, ts_str))

    # 4. Tweezer Bottom / Top (matching wick tips within 0.05%)
    tol = 0.0005
    low_diff = abs(curr["low"] - prev["low"]) / prev["low"]
    high_diff = abs(curr["high"] - prev["high"]) / prev["high"]
    if trend == "downtrend" and low_diff <= tol and m_prev["is_bear"] and m_curr["is_bull"]:
        signals.append(_score_signal(ticker, "tweezer_bottom", "bullish", 1.0 - low_diff, rel_vol, ts_str))
    elif trend == "uptrend" and high_diff <= tol and m_prev["is_bull"] and m_curr["is_bear"]:
        signals.append(_score_signal(ticker, "tweezer_top", "bearish", 1.0 - high_diff, rel_vol, ts_str))

    # 5. Three White Soldiers / Three Black Crows
    if len(df) >= 3:
        if (trend == "downtrend" and m_prev2["is_bull"] and m_prev2["body_ratio"] >= 0.60
                and m_prev["is_bull"] and m_prev["body_ratio"] >= 0.60 and prev["open"] >= prev2["open"]
                and m_curr["is_bull"] and m_curr["body_ratio"] >= 0.60 and curr["open"] >= prev["open"]):
            signals.append(_score_signal(ticker, "three_white_soldiers", "bullish", m_curr["body_ratio"], rel_vol, ts_str))
        if (trend == "uptrend" and m_prev2["is_bear"] and m_prev2["body_ratio"] >= 0.60
                and m_prev["is_bear"] and m_prev["body_ratio"] >= 0.60 and prev["open"] <= prev2["open"]
                and m_curr["is_bear"] and m_curr["body_ratio"] >= 0.60 and curr["open"] <= prev["open"]):
            signals.append(_score_signal(ticker, "three_black_crows", "bearish", m_curr["body_ratio"], rel_vol, ts_str))

    # 6. Pin bars — Hammer/Hanging Man, Shooting Star/Inverted Hammer
    if 0.05 <= m_curr["body_ratio"] <= 0.35 and m_curr["lower_ratio"] >= 0.60 and m_curr["upper_ratio"] <= 0.15:
        if trend == "downtrend":
            signals.append(_score_signal(ticker, "hammer", "bullish", m_curr["lower_ratio"], rel_vol, ts_str))
        elif trend == "uptrend":
            signals.append(_score_signal(ticker, "hanging_man", "bearish", m_curr["lower_ratio"], rel_vol, ts_str))
    if 0.05 <= m_curr["body_ratio"] <= 0.35 and m_curr["upper_ratio"] >= 0.60 and m_curr["lower_ratio"] <= 0.15:
        if trend == "uptrend":
            signals.append(_score_signal(ticker, "shooting_star", "bearish", m_curr["upper_ratio"], rel_vol, ts_str))
        elif trend == "downtrend":
            signals.append(_score_signal(ticker, "inverted_hammer", "bullish", m_curr["upper_ratio"], rel_vol, ts_str))

    # 7. Marubozu / Belt Hold — requires volume surge
    if m_curr["body_ratio"] >= 0.85 and rel_vol >= 1.50:
        if m_curr["is_bull"] and trend != "downtrend":
            signals.append(_score_signal(ticker, "marubozu", "bullish", m_curr["body_ratio"], rel_vol, ts_str))
        elif m_curr["is_bear"] and trend != "uptrend":
            signals.append(_score_signal(ticker, "marubozu", "bearish", m_curr["body_ratio"], rel_vol, ts_str))

    # 8. Rising Three Methods — 5-candle structure
    if len(df) >= 5:
        c1 = df.iloc[-5]
        m1 = candle_metrics(c1)
        mid = df.iloc[-4:-1]
        if (m1["is_bull"] and m1["body_ratio"] >= 0.65 and m_curr["is_bull"]
                and m_curr["body_ratio"] >= 0.65 and curr["close"] > c1["high"]
                and mid["low"].min() > c1["low"] and mid["high"].max() <= c1["high"]):
            signals.append(_score_signal(ticker, "rising_three", "bullish", m_curr["body_ratio"], rel_vol, ts_str))

    # 9. Doji — only if nothing else fired, capped at 72%
    if not signals and m_curr["body_ratio"] < 0.08:
        sig = _score_signal(ticker, "doji", "neutral", 1.0 - m_curr["body_ratio"], rel_vol, ts_str)
        sig.confidence = min(sig.confidence, 72.0)
        signals.append(sig)

    return signals


# ---------------------------------------------------------------------------
# Data fetch + indicators
# ---------------------------------------------------------------------------

def _symbols_for(key: str) -> list[str]:
    """Resolve universe key to symbol list.

    smallcap = BROAD_STOCKS minus ACTIVE_STOCKS (the ~80 extra names are the
    smaller/less-liquid tail); multicap = ACTIVE_STOCKS (~100 liquid names).
    A symbols_<key>.json file still overrides either if present.
    """
    import json, os
    from scanner import NIFTY50, ACTIVE_STOCKS, BROAD_STOCKS

    smallcap = [s for s in BROAD_STOCKS if s not in set(ACTIVE_STOCKS)]
    builtin = {
        "nifty50": NIFTY50,
        "active": ACTIVE_STOCKS,
        "broad": BROAD_STOCKS,
        "multicap": ACTIVE_STOCKS,
        "smallcap": smallcap,
    }
    path = f"symbols_{key}.json"
    if os.path.exists(path):
        try:
            syms = [s.strip().upper() for s in json.loads(open(path, encoding="utf-8").read()) if s.strip()]
            if syms:
                return syms
        except Exception as exc:
            logger.warning("Could not load %s: %s", path, exc)
    return builtin[key]


def _prepare_df(sym: str, interval: str, period: str) -> Optional[pd.DataFrame]:
    """Fetch OHLCV, drop the open candle, add ema20/atr14/vwap/rel_vol_tod."""
    try:
        import yfinance as yf
        df = yf.Ticker(f"{sym}.NS").history(period=period, interval=interval, auto_adjust=False)
        if df is None or df.empty or len(df) < 30:
            return None
        df = df.iloc[:-1].copy()  # drop currently-open candle
        df.columns = [c.lower() for c in df.columns]
        df = df.rename(columns={"datetime": "timestamp"})
        df["timestamp"] = df.index
        df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        df["atr14"] = tr.rolling(14).mean()
        if interval in {"5m", "15m", "1h"}:
            pv = (df["high"] + df["low"] + df["close"]) / 3 * df["volume"]
            df["vwap"] = pv.cumsum() / df["volume"].cumsum().replace(0, float("nan"))
        else:
            df["vwap"] = df["ema20"]
        df["rel_vol_tod"] = compute_tod_relative_volume(df)
        return df
    except Exception:
        return None


def _atr(candles_close: list, highs, lows, n: int = 14) -> float:
    trs = []
    for i in range(1, len(highs)):
        h, l, pc = highs[i], lows[i], candles_close[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / min(n, len(trs)) if trs else 0.0


def _levels(df: pd.DataFrame, bias: str) -> dict:
    """SL + T1/T2/T3 in R-multiples of ATR(14) from signal close."""
    entry = float(df.iloc[-1]["close"])
    highs = df["high"].tolist(); lows = df["low"].tolist(); closes = df["close"].tolist()
    risk = max(_atr(closes, highs, lows), float(df.iloc[-1]["high"] - df.iloc[-1]["low"]))
    if risk <= 0 or bias == "neutral":
        return {}
    m = 1 if bias == "bullish" else -1
    return {"sl": round(entry - m * risk, 2),
            "t1": round(entry + m * risk, 2),
            "t2": round(entry + m * risk * 2, 2),
            "t3": round(entry + m * risk * 3, 2)}


# ---------------------------------------------------------------------------
# Scan + report
# ---------------------------------------------------------------------------

def run_scan(universe_key: str, tf_key: str, group: str,
             threshold: float, log: Optional[logging.Logger] = None) -> str:
    log = log or logger
    _, interval, period = TIMEFRAMES[tf_key]
    tf_label = TIMEFRAMES[tf_key][0]
    group_name = {"all": "All patterns", "bullish": "Bullish reversals",
                  "bearish": "Bearish reversals",
                  "continuation": "Continuation/indecision"}[group]
    allowed = GROUP_MAP[group]
    symbols = _symbols_for(UNIVERSES[universe_key][1])
    results = []

    for sym in symbols:
        df = _prepare_df(sym, interval, period)
        if df is None:
            continue
        for sig in classify_comprehensive_patterns(df, sym, interval):
            if sig.pattern not in allowed or sig.bias == "neutral" and group != "all" and group != "continuation":
                continue
            if sig.confidence >= threshold:
                c = df.iloc[-1]
                results.append({
                    "symbol": sig.ticker, "pattern": sig.pattern, "bias": sig.bias,
                    "confidence": sig.confidence, "timestamp": sig.timestamp,
                    "ohlc": f"O{c['open']:.2f} H{c['high']:.2f} L{c['low']:.2f} C{c['close']:.2f}",
                    "vol": float(c["volume"] or 0),
                    "why": f"body {sig.body_ratio:.0f}% · relVol {sig.rel_vol_tod:.1f}x",
                    **_levels(df, sig.bias),
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
        try:
            t = datetime.fromtimestamp(pd.Timestamp(r["timestamp"]).timestamp(),
                                       ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M")
        except Exception:
            t = str(r["timestamp"])
        lv = (f"{r.get('sl','—')} | {r.get('t1','—')} | {r.get('t2','—')} | {r.get('t3','—')}"
              if r.get("sl") is not None else "— | — | — | —")
        lines.append(
            f"| {r['symbol']} | {r['pattern']} | {r['bias']} | {r['confidence']}% "
            f"| {t} | {lv} | {r['ohlc']} · {r['why']} · vol {r['vol']:.0f} |"
        )
    lines.append("\n**Post-scan:** [R] re-run same settings · [F] filter further "
                 "(RSI/overbought) · [C] change options · [D] deep-dive a ticker")
    return "\n".join(lines)