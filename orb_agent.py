"""Intraday ORB + VWAP Momentum Agent.

Frameworks implemented:
1. INTRADAY (primary): Session VWAP trend filter + 15-minute Opening Range
   Breakout (ORH/ORL) with Relative Volume > 1.5x -> ATM/ITM option spread.
2. SWING (fallback): RSI(14) + EMA20/50 trend filter -> monthly vertical spread.

Option premiums are ESTIMATES — verify on NSE option chain.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from agent import _ema, _rsi, _sma
from options_agent import _next_monthly_expiry, _round_strike, _strike_step, bullish_side


def _intraday_frame(tk: Any) -> dict:
    """Compute VWAP, opening range, RVOL from 15m bars."""
    df = tk.history(period="5d", interval="15m", auto_adjust=False)
    if df is None or df.empty or len(df) < 30:
        return {}

    # latest trading day only
    today = df.index[-1].date()
    day = df[df.index.date == today] if hasattr(df.index, "date") else df.tail(30)
    if day.empty:
        day = df.tail(30)

    closes = day["Close"].dropna()
    if closes.empty:
        return {}

    # session VWAP
    if {"High", "Low", "Volume"}.issubset(day.columns):
        typical = (day["High"] + day["Low"] + day["Close"]) / 3
        vol = day["Volume"].fillna(0)
        vwap = float((typical * vol).sum() / vol.sum()) if vol.sum() else None
    else:
        vwap = None

    # opening range = first 15m bar of the session
    or_bar = day.iloc[0]
    orh = float(or_bar["High"])
    orl = float(or_bar["Low"])

    # RVOL: today's avg bar volume vs 20-bar avg volume of prior days
    prior = df[df.index.date < today] if hasattr(df.index, "date") else df.iloc[:-len(day)]
    avg_prior = float(prior["Volume"].tail(80).mean()) if len(prior) else None
    rvol = None
    if avg_prior:
        today_vol = float(day["Volume"].sum())
        bars_so_far = max(len(day), 1)
        # project vs average per-bar volume
        rvol = round(float(day["Volume"].mean()) / avg_prior, 2) if avg_prior else None

    last = float(closes.iloc[-1])
    return {
        "vwap": round(vwap, 2) if vwap else None,
        "orh": round(orh, 2),
        "orl": round(orl, 2),
        "last": round(last, 2),
        "rvol": rvol,
        "above_vwap": (last > vwap) if vwap else None,
        "orb_breakout": "BULL" if last > orh else ("BEAR" if last < orl else "NONE"),
        "bars": len(day),
    }


def analyze_orb(symbol: str, kind: str, logger: logging.Logger) -> str:
    """ORB/VWAP intraday analysis with swing fallback (markdown)."""
    import yfinance as yf

    from config import Instrument

    inst = Instrument(symbol=symbol.upper(), kind=kind.upper())
    idx_map = {
        "NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "^CNXFIN",
        "MIDCPNIFT": "^CNXMID",
    }
    alias = {"HDFC": "HDFCBANK.NS"}.get(inst.symbol)
    ys = alias or (
        idx_map.get(inst.symbol, f"^{inst.symbol}")
        if inst.kind == "INDEX" else f"{inst.symbol}.NS"
    )

    tk = yf.Ticker(ys)

    # --- intraday frame -------------------------------------------------------
    intra = {}
    try:
        intra = _intraday_frame(tk)
    except Exception as exc:
        logger.debug("Intraday frame failed for %s: %s", symbol, exc)

    # --- daily frame ----------------------------------------------------------
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

    # --- decision: intraday ORB first, swing fallback --------------------------
    orb = intra.get("orb_breakout", "NONE")
    rvol = intra.get("rvol")
    above_vwap = intra.get("above_vwap")
    vwap = intra.get("vwap")
    orh, orl = intra.get("orh"), intra.get("orl")

    intraday_valid = (
        orb in {"BULL", "BEAR"}
        and (rvol is None or rvol > 1.5)
        and (above_vwap is not None and ((orb == "BULL") == above_vwap))
    )

    # swing RSI+EMA check (reuse simplified logic)
    above_trend = bool(cmp_price and ema20 and ema50 and cmp_price > ema20 and cmp_price > ema50)
    below_trend = bool(cmp_price and ema20 and ema50 and cmp_price < ema20 and cmp_price < ema50)
    swing_bull = above_trend and rsi_now is not None and rsi_now < 60
    swing_bear = below_trend and rsi_now is not None and rsi_now > 40

    # ---- levels ----------------------------------------------------------------
    def pct(v, p):
        return round(v * (1 + p), 2) if v else None

    expiry = _next_monthly_expiry()
    step = _strike_step(price)
    atm = _round_strike(price)
    otm_offset = max(step, int(round(price * 0.02 / step) * step))
    est_debit = round(price * 0.009, 2)
    est_max = round(otm_offset - est_debit, 2)
    rr = round(est_max / est_debit, 2) if est_debit else None

    header = f"""### {inst.symbol} ({ys}) — ORB/VWAP Momentum Analysis
*Generated {date.today().isoformat()} · Expiry ~{expiry}*

## 1. Market Catalyst & Structural Summary
- **Price:** ₹{price:,.2f} | **VWAP:** {('₹' + format(vwap, ',.2f')) if vwap else 'N/A'} ({'above' if above_vwap else 'below' if above_vwap is False else 'N/A'})
- **Trend:** {'above' if cmp_price and ema20 and cmp_price > ema20 else 'below' if cmp_price and ema20 else 'N/A'} EMA20, {'above' if cmp_price and ema50 and cmp_price > ema50 else 'below' if cmp_price and ema50 else 'N/A'} EMA50, {'above' if cmp_price and sma200 and cmp_price > sma200 else 'below' if cmp_price and sma200 else 'N/A'} 200SMA
- **RSI(14):** {('%.1f' % rsi_now) if rsi_now is not None else 'N/A'} | **Volume:** {vol_ratio if vol_ratio else 'N/A'}x 20d avg (daily), RVOL {rvol if rvol else 'N/A'}x (intraday)
- **Catalyst/Sentiment:** No news feed in this agent — check NSE announcements manually.

## 2. Technical Execution Bounds
- **15m Opening Range:** ORH ₹{orh:,.2f} / ORL ₹{orl:,.2f} | **ORB status:** {orb}
"""

    if intraday_valid:
        bullish = orb == "BULL"
        bias = "Bullish Long (ORB breakout)" if bullish else "Bearish Short (ORB breakdown)"
        long_k, short_k, opt = (atm, atm + otm_offset, "CE") if bullish else (atm, atm - otm_offset, "PE")
        t1 = pct(price, 0.008) if bullish else pct(price, -0.008)
        t2 = pct(price, 0.015) if bullish else pct(price, -0.015)
        sl = orl if bullish else orh
        strat = "Bull Call Spread" if bullish else "Bear Put Spread"
        return header + f"""
- **Directional Bias:** {bias}
- **Entry Trigger Zone:** {'Breakout above ORH ₹' + format(orh, ',.2f') if bullish else 'Breakdown below ORL ₹' + format(orl, ',.2f')} with RVOL {rvol}x > 1.5
- **T1 (15–60 min):** ₹{t1:,.2f} | **T2 (1–3 sessions):** ₹{t2:,.2f}
- **Strict Stop-Loss:** ₹{sl:,.2f} (opposite side of opening range)

## 3. Actionable Derivatives Strategy (Intraday)
- **Strategy Type:** {strat} — ATM{'/ITM delta 0.50–0.65' if bullish else ' (ITM put, delta -0.50 to -0.65)'}
- **Strikes:** Buy {long_k} {opt} / Sell {short_k} {opt} ({expiry})
- **Net Debit / Max Risk:** ~₹{est_debit:,.2f} *ESTIMATE* — verify on chain
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
        trig = "RSI+EMA pullback/reclaim setup" if bullish else "RSI+EMA rejection/breakdown setup"
        return header + f"""
- **Directional Bias:** {bias} — {'no intraday ORB trigger; swing setup active' if intraday_valid is False else ''}
- **Entry Trigger Zone:** {trig} — enter on next session or VWAP reclaim
- **T1 (2–4 sessions):** ₹{t1:,.2f} | **T2 (5–10 sessions):** ₹{t2:,.2f}
- **Strict Stop-Loss:** ₹{sl:,.2f} (swing {'low' if bullish else 'high'})

## 3. Actionable Derivatives Strategy (Monthly Expiry)
- **Strategy Type:** {strat}
- **Strikes:** Buy {long_k} {opt} / Sell {short_k} {opt} ({expiry})
- **Net Debit / Max Risk:** ~₹{est_debit:,.2f} *ESTIMATE*
- **Max Reward:** ~₹{est_max:,.2f} | **R:R:** ~1:{rr}
- **Exit Rules:** 50% at T1, rest at T2/expiry; exit if underlying closes beyond SL
"""

    # neutral / wait
    up = swing_high or orh or pct(price, 0.01)
    dn = swing_low or orl or pct(price, -0.01)
    return header + f"""
- **Directional Bias:** NEUTRAL — Wait for Trigger
- **ORB status:** {orb} — {'price above VWAP but RVOL ' + str(rvol) + 'x below 1.5x threshold' if orb == 'BULL' and above_vwap else ('price below VWAP' if orb == 'BEAR' else 'no opening-range break yet')}; needs VWAP agreement + RVOL > 1.5x to trigger
- **Entry Trigger Zone:** Breakout above ₹{up:,.2f} or breakdown below ₹{dn:,.2f}
- **T1/T2/SL:** Recompute on trigger confirmation

## 3. Actionable Derivatives Strategy
**NO TRADE** — no valid ORB+VWAP or RSI+EMA trigger. Wait for the levels above.
"""
