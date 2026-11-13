"""Quantitative engine: VWAP, Opening Range, ATR, RVOL + state classifier."""

from __future__ import annotations

from typing import Optional


def session_vwap(bars: list[dict]) -> Optional[float]:
    """VWAP from typical price * volume over session bars."""
    pv = vv = 0.0
    for b in bars:
        tp = (b["high"] + b["low"] + b["close"]) / 3
        pv += tp * b.get("volume", 1)
        vv += b.get("volume", 1)
    return pv / vv if vv else None


def opening_range(bars: list[dict], n: int = 3) -> tuple[Optional[float], Optional[float]]:
    """First `n` 5m bars -> (ORH, ORL)."""
    if len(bars) < n:
        return None, None
    seg = bars[:n]
    return max(b["high"] for b in seg), min(b["low"] for b in seg)


def atr(bars: list[dict], period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period if trs else None


def rvol(bars: list[dict], lookback: int = 20) -> Optional[float]:
    """Latest bar volume vs average of prior `lookback` bars."""
    if len(bars) < lookback + 1:
        return None
    avg = sum(b["volume"] for b in bars[-lookback - 1:-1]) / lookback
    return bars[-1]["volume"] / avg if avg else None


def candle_body(b: dict) -> float:
    return abs(b["close"] - b["open"])


def upper_wick(b: dict) -> float:
    return b["high"] - max(b["open"], b["close"])


def classify(fut: list[dict], ce: list[dict], pe: list[dict]) -> dict:
    """Classify the latest closed 5m state across all three charts."""
    if not fut or not ce or not pe:
        return {"state": "WARMUP", "detail": "insufficient bars"}

    fb, cb, pb = fut[-1], ce[-1], pe[-1]
    fvwap, cvwap, pvwap = session_vwap(fut), session_vwap(ce), session_vwap(pe)
    orh, orl = opening_range(fut)
    fatr, catr, patr = atr(fut), atr(ce), atr(pe)
    frvol, crvol, prvol = rvol(fut), rvol(ce), rvol(pe)

    # --- exhaustion check first -------------------------------------------
    big_fut_move = abs(fb["close"] - fut[0]["open"]) > 120
    wick_spike = (
        fatr and candle_body(cb) > 2.5 * (catr or 1)
        and upper_wick(cb) > candle_body(cb) * 1.5
        and (crvol or 0) > 2.0 and big_fut_move
    )
    if wick_spike:
        return {"state": "EXHAUSTION", "detail": "Option spike + long upper wick after extended move"}

    # --- ignition checks ----------------------------------------------------
    bull_ignition = (
        fatr and candle_body(fb) > 1.5 * fatr
        and (frvol or 0) >= 2.0
        and ((fvwap and fb["close"] > fvwap) or (orh and fb["close"] > orh))
        and cb["close"] > (cvwap or 0) and (crvol or 0) >= 1.5
    )
    bear_ignition = (
        fatr and candle_body(fb) > 1.5 * fatr
        and (frvol or 0) >= 2.0
        and ((fvwap and fb["close"] < fvwap) or (orl and fb["close"] < orl))
        and pb["close"] > (pvwap or 0) and (prvol or 0) >= 1.5
    )

    if bull_ignition:
        return {"state": "BUY_CALL", "detail": "Futures bullish ignition + CE above option VWAP"}
    if bear_ignition:
        return {"state": "BUY_PUT", "detail": "Futures bearish ignition + PE above option VWAP"}

    # --- theta / range regime ----------------------------------------------
    fut_range = (max(b["high"] for b in fut[-6:]) - min(b["low"] for b in fut[-6:])) if len(fut) >= 6 else None
    tight = fut_range is not None and fatr and fut_range < 4 * fatr
    ce_below = cvwap and cb["close"] < cvwap
    pe_below = pvwap and pb["close"] < pvwap
    oi_rising = cb.get("oi", 0) > 0 and pb.get("oi", 0) > 0
    if tight and ce_below and pe_below and oi_rising:
        return {"state": "THETA_DECAY", "detail": "Range-bound chop, both options below VWAP, OI rising"}

    return {"state": "NEUTRAL", "detail": "no aligned setup"}
