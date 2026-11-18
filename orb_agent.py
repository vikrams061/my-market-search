"""Intraday ORB + VWAP Momentum Agent (Multi-Index Support: Nifty, Bank Nifty, Finnifty, Midcap Nifty).

Frameworks implemented:
1. INTRADAY (primary): Session VWAP trend filter + 15-minute Opening Range
   Breakout (ORH/ORL) + Relative Volume > 1.5x + 15m RSI Confirmation -> ATM/ITM option spread.
2. SWING (fallback): RSI(14) + EMA20/50 trend filter -> monthly vertical spread.

Option premiums are ESTIMATES — verify on NSE option chain.
"""

from __future__ import annotations

import logging
from datetime import datetime, date
from typing import Any, Optional
import pytz
import pandas as pd
import yfinance as yf

from agent import _ema, _rsi, _sma
from options_agent import _next_monthly_expiry, _round_strike
from config import Instrument


def _calc_intraday_rsi(closes: pd.Series, period: int = 14) -> float:
    """Compute RSI on intraday price series."""
    if len(closes) < period + 1:
        return 50.0
    delta = closes.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return float(val) if pd.notna(val) else 50.0


def _get_dynamic_strike_step(symbol: str, price: float) -> int:
    """Return correct NSE option strike step based on instrument type."""
    sym = symbol.upper()
    if "BANKNIFTY" in sym or sym == "^NSEBANK":
        return 100
    elif "FINNIFTY" in sym or sym == "^CNXFIN":
        return 50
    elif "MIDCP" in sym or sym == "^CNXMID":
        return 25
    else:  # NIFTY / Equities
        return 50 if price > 2000 else 10


def _intraday_frame(tk: Any) -> dict:
    """Compute VWAP, opening range, RVOL, and 15m RSI from 15m bars."""
    df = tk.history(period="5d", interval="15m", auto_adjust=False)
    if df is None or df.empty or len(df) < 10:
        return {}

    # Convert to IST timezone for reliable daily slicing
    ist = pytz.timezone("Asia/Kolkata")
    if df.index.tz is not None:
        df.index = df.index.tz_convert(ist)
    else:
        df.index = df.index.tz_localize("UTC").tz_convert(ist)

    today = datetime.now(ist).date()
    day = df[df.index.date == today]
    if day.empty:
        latest_date = df.index.date[-1]
        day = df[df.index.date == latest_date]

    if day.empty or len(day) < 1:
        return {}

    closes = day["Close"].dropna()
    if closes.empty:
        return {}

    # Session VWAP calculation
    vwap = None
    if {"High", "Low", "Close"}.issubset(day.columns):
        typical = (day["High"] + day["Low"] + day["Close"]) / 3
        if "Volume" in day.columns and day["Volume"].sum() > 0:
            vol = day["Volume"].fillna(0)
            vwap = float((typical * vol).sum() / vol.sum())
        else:
            vwap = float(typical.mean())

    # Opening range = first 15m bar
    or_bar = day.iloc[0]
    orh = float(or_bar["High"])
    orl = float(or_bar["Low"])

    # RVOL: today's cumulative volume vs expected cumulative at this point
    # in the session (prior days' avg full-day volume x bars elapsed / 25).
    # Returns None when volume data is absent/zero (e.g. indices on yfinance)
    # so the RVOL gate is skipped rather than falsely failing.
    prior = df[df.index.date < day.index[0].date()]
    rvol = None
    if "Volume" in day.columns and "Volume" in prior.columns:
        day_vol = float(day["Volume"].fillna(0).sum())
        prior_daily = prior.groupby(prior.index.date)["Volume"].sum()
        avg_day_vol = float(prior_daily.mean()) if len(prior_daily) else 0.0
        if day_vol > 0 and avg_day_vol > 0:
            session_bars = 25  # 09:15–15:30 in 15m bars
            frac = min(len(day) / session_bars, 1.0)
            expected = avg_day_vol * frac
            rvol = round(day_vol / expected, 2) if expected > 0 else None

    last = float(closes.iloc[-1])
    rsi_15m = _calc_intraday_rsi(closes, 14)

    # ORB Breakout status with strict boundary checks
    breakout_status = "NONE"
    if last > orh:
        breakout_status = "BULL"
    elif last < orl:
        breakout_status = "BEAR"

    return {
        "vwap": round(vwap, 2) if vwap else round(last, 2),
        "orh": round(orh, 2),
        "orl": round(orl, 2),
        "last": round(last, 2),
        "rvol": rvol,
        "rsi_15m": round(rsi_15m, 1),
        "above_vwap": (last >= vwap) if vwap else True,
        "orb_breakout": breakout_status,
        "bars": len(day),
    }


def analyze_orb(symbol: str, kind: str, logger: logging.Logger) -> str:
    """ORB/VWAP intraday analysis with multi-index `idx_map` support and RSI confirmation."""
    inst = Instrument(symbol=symbol.upper(), kind=kind.upper())
    
    idx_map = {
        "NIFTY": "^NSEI", 
        "BANKNIFTY": "^NSEBANK", 
        "FINNIFTY": "^CNXFIN",
        "MIDCPNIFT": "^CNXMID",
    }
    
    alias = {"HDFC": "HDFCBANK.NS"}.get(inst.symbol)
    ys = alias or (
        idx_map.get(inst.symbol, f"^{inst.symbol}")
        if inst.kind == "INDEX" else f"{inst.symbol}.NS"
    )

    tk = yf.Ticker(ys)

    # --- Intraday Frame ---
    intra = {}
    try:
        intra = _intraday_frame(tk)
    except Exception as exc:
        logger.debug("Intraday frame failed for %s: %s", symbol, exc)

    # --- Daily Frame ---
    hist = tk.history(period="6mo", interval="1d", auto_adjust=False)
    closes, volumes = [], []
    cmp_price = ema20 = ema50 = sma200 = rsi_now = None
    swing_high = swing_low = None
    vol_ratio = None
    if hist is not None and not hist.empty and len(hist) >= 60:
        closes = [float(c) for c in hist["Close"].dropna()]
        volumes = [float(v) for v in hist["Volume"].dropna()] if "Volume" in hist.columns else []
        cmp_price = closes[-1]
        ema20, ema50, sma200 = _ema(closes, 20), _ema(closes, 50), _sma(closes, 200)
        rsi_now = _rsi(closes, 14)
        try:
            w = hist.tail(20)
            swing_high, swing_low = float(w["High"].max()), float(w["Low"].min())
        except Exception:
            pass
        if volumes:
            avg20 = sum(volumes[-20:]) / min(20, len(volumes))
            vol_ratio = round(volumes[-1] / avg20, 2) if avg20 else None

    price = (intra.get("last") or cmp_price)
    if not price:
        raise RuntimeError(f"No price data for {symbol}")

    # --- Decision Engine: ORB + VWAP + 15m RSI Filter ---
    orb = intra.get("orb_breakout", "NONE")
    rvol = intra.get("rvol", 1.0)
    above_vwap = intra.get("above_vwap", True)
    vwap = intra.get("vwap")
    orh, orl = intra.get("orh"), intra.get("orl")
    orh_txt = f"₹{orh:,.2f}" if orh is not None else "N/A"
    orl_txt = f"₹{orl:,.2f}" if orl is not None else "N/A"
    rsi_15m = intra.get("rsi_15m", 50.0)
    rvol_txt = f"{rvol}x" if rvol is not None else "N/A"

    # Intraday requires: Breakout + RVOL > 1.5 + VWAP alignment + RSI momentum confirmation (>55 for bull, <45 for bear)
    intraday_valid = (
        orb in {"BULL", "BEAR"}
        and (rvol is None or rvol > 1.5)
        and (above_vwap is not None and ((orb == "BULL") == above_vwap))
        and ((orb == "BULL" and rsi_15m > 55.0) or (orb == "BEAR" and rsi_15m < 45.0))
    )

    # Swing fallback check
    above_trend = bool(cmp_price and ema20 and ema50 and cmp_price > ema20 and cmp_price > ema50)
    below_trend = bool(cmp_price and ema20 and ema50 and cmp_price < ema20 and cmp_price < ema50)
    swing_bull = above_trend and rsi_now is not None and rsi_now < 60
    swing_bear = below_trend and rsi_now is not None and rsi_now > 40

    # --- Strike & Options Calculations (Dynamic Strike Step, 1.5% OTM Offset) ---
    def pct(v, p):
        return round(v * (1 + p), 2) if v else None

    expiry = _next_monthly_expiry()
    step = _get_dynamic_strike_step(inst.symbol, price)
    atm = _round_strike(price)
    
    otm_offset = max(step, int(round(price * 0.015 / step) * step))
    est_debit = round(price * 0.008, 2)
    est_max = round(otm_offset - est_debit, 2)
    rr = round(est_max / est_debit, 2) if est_debit else None

    header = f"""### {inst.symbol} ({ys}) — ORB/VWAP Momentum Analysis
*Generated {date.today().isoformat()} · Expiry ~{expiry}*

## 1. Market Catalyst & Structural Summary
- **Price:** ₹{price:,.2f} | **VWAP:** {('₹' + format(vwap, ',.2f')) if vwap else 'N/A'} ({'above' if above_vwap else 'below' if above_vwap is False else 'N/A'})
- **Trend:** {'above' if cmp_price and ema20 and cmp_price > ema20 else 'below' if cmp_price and ema20 else 'N/A'} EMA20, {'above' if cmp_price and ema50 and cmp_price > ema50 else 'below' if cmp_price and ema50 else 'N/A'} EMA50
- **RSI(14):** Daily {('%.1f' % rsi_now) if rsi_now is not None else 'N/A'} | **15m RSI:** {rsi_15m} | **RVOL:** {rvol_txt}
- **Catalyst/Sentiment:** No news feed in this agent — check NSE announcements manually.

## 2. Technical Execution Bounds
- **15m Opening Range:** ORH {orh_txt} / ORL {orl_txt} | **ORB status:** {orb}
"""

    if intraday_valid:
        bullish = orb == "BULL"
        bias = "Bullish Long (ORB breakout + RSI confirmation)" if bullish else "Bearish Short (ORB breakdown + RSI confirmation)"
        long_k, short_k, opt = (atm, atm + otm_offset, "CE") if bullish else (atm, atm - otm_offset, "PE")
        t1 = pct(price, 0.008) if bullish else pct(price, -0.008)
        t2 = pct(price, 0.015) if bullish else pct(price, -0.015)
        sl = orl if bullish else orh
        strat = "Bull Call Spread" if bullish else "Bear Put Spread"
        try:
            from pick_history import record_pick
            record_pick(source="orb", side="bullish" if bullish else "bearish",
                        symbol=inst.symbol, entry_price=price, target=t1, stop_loss=sl)
        except Exception:
            pass
        return header + f"""
- **Directional Bias:** {bias}
- **Entry Trigger Zone:** {'Breakout above ORH ₹' + format(orh, ',.2f') if bullish else 'Breakdown below ORL ₹' + format(orl, ',.2f')} with RVOL {rvol_txt} (>1.5) and 15m RSI {rsi_15m}
- **T1 (15–60 min):** ₹{t1:,.2f} | **T2 (1–3 sessions):** ₹{t2:,.2f}
- **Strict Stop-Loss:** ₹{sl:,.2f} (opposite side of opening range)

## 3. Actionable Derivatives Strategy (Intraday)
- **Strategy Type:** {strat} (1.5% OTM Width)
- **Strikes:** Buy {long_k} {opt} / Sell {short_k} {opt} ({expiry})
- **Net Debit / Max Risk:** ~₹{est_debit:,.2f} *ESTIMATE*
- **Max Reward:** ~₹{est_max:,.2f} | **R:R:** ~1:{rr}
- **Exit Rules:** Square off 50% at T1 or VWAP failure; hard exit if price closes back inside opening range
"""

    if swing_bull or swing_bear:
        bullish = swing_bull and not swing_bear
        bias = "Bullish (swing)" if bullish else "Bearish (swing)"
        long_k, short_k, opt = (atm, atm + otm_offset, "CE") if bullish else (atm, atm - otm_offset, "PE")
        t1 = pct(price, 0.04) if bullish else pct(price, -0.04)
        t2 = pct(price, 0.09) if bullish else pct(price, -0.09)
        sl = swing_low or pct(price, -0.02) if bullish else swing_high or pct(price, 0.02)
        strat = "Bull Call Spread" if bullish else "Bear Put Spread"
        try:
            from pick_history import record_pick
            record_pick(source="orb", side="bullish" if bullish else "bearish",
                        symbol=inst.symbol, entry_price=price, target=t1, stop_loss=sl)
        except Exception:
            pass
        return header + f"""
- **Directional Bias:** {bias} — intraday ORB filter not met; swing setup active
- **Entry Trigger Zone:** RSI+EMA pullback/reclaim setup
- **T1 (2–4 sessions):** ₹{t1:,.2f} | **T2 (5–10 sessions):** ₹{t2:,.2f}
- **Strict Stop-Loss:** ₹{sl:,.2f}

## 3. Actionable Derivatives Strategy (Monthly Expiry)
- **Strategy Type:** {strat} (1.5% OTM Width)
- **Strikes:** Buy {long_k} {opt} / Sell {short_k} {opt} ({expiry})
- **Net Debit / Max Risk:** ~₹{est_debit:,.2f} *ESTIMATE*
- **Max Reward:** ~₹{est_max:,.2f} | **R:R:** ~1:{rr}
"""

    return header + f"""
- **Directional Bias:** NEUTRAL — Wait for Trigger
- **ORB status:** {orb} — price {'above' if above_vwap else 'below'} VWAP, RVOL {rvol_txt}, 15m RSI {rsi_15m}; needs RVOL > 1.5x + 15m RSI confirmation (>55 / <45)
- **Entry Trigger Zone:** Breakout above {orh_txt} or breakdown below {orl_txt}
- **T1/T2/SL:** Recompute on trigger confirmation

## 3. Actionable Derivatives Strategy
**NO TRADE** — insufficient momentum or conflicting VWAP/RSI signals.
"""