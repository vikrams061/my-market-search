"""Defined-Risk Options Strategy Agent.

Implements the RSI(14) + EMA trend-filter methodology:
- Bullish: price > EMA20 & EMA50; RSI pullback 40-45 reclaiming 50, or
  oversold (<35) bounce with volume confirmation -> Bull Call Spread.
- Bearish: price < EMA20 & EMA50; RSI rejection 50-60 turning down, or
  break below 40 -> Bear Put Spread.
- Swing horizon 5-10 sessions, min 1:2 R:R, cash-anchored stops,
  "NO TRADE" output when no valid setup exists.

Option premiums are ESTIMATES (Yahoo has no NSE option chain) — the report
labels them clearly and tells the user to verify on the NSE chain.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from agent import _ema, _rsi, _sma


def _rsi_at(closes: list[float], days_ago: int = 0, period: int = 14) -> Optional[float]:
    """RSI computed on closes truncated `days_ago` bars back."""
    if days_ago:
        closes = closes[:-days_ago]
    return _rsi(closes, period)


def _swing_levels(hist) -> tuple[Optional[float], Optional[float]]:
    """Recent swing high/low over last ~20 sessions."""
    try:
        window = hist.tail(20)
        return float(window["High"].max()), float(window["Low"].min())
    except Exception:
        return None, None


def _strike_step(price: float) -> int:
    return 50 if price < 1000 else 100


def _round_strike(price: float) -> int:
    step = _strike_step(price)
    return int(round(price / step) * step)


def analyze_options(symbol: str, kind: str, logger: logging.Logger) -> str:
    """Produce the defined-risk options analysis (markdown)."""
    import yfinance as yf

    from config import Instrument
    from market_watch import MarketWatch

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
    hist = tk.history(period="6mo", interval="1d", auto_adjust=False)
    if hist is None or hist.empty or len(hist) < 60:
        raise RuntimeError(f"Insufficient price history for {symbol} ({ys})")

    closes = [float(c) for c in hist["Close"].dropna()]
    volumes = [float(v) for v in hist["Volume"].dropna()] if "Volume" in hist.columns else []

    cmp_price = closes[-1]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    sma200 = _sma(closes, 200)
    rsi_now = _rsi(closes, 14)
    rsi_3d = _rsi_at(closes, 3)
    rsi_5d = _rsi_at(closes, 5)
    swing_high, swing_low = _swing_levels(hist)
    avg_vol20 = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else None
    vol_ratio = (volumes[-1] / avg_vol20) if (volumes and avg_vol20) else None

    # ---- trend alignment ----------------------------------------------------
    above_trend = bool(
        cmp_price and ema20 and ema50 and cmp_price > ema20 and cmp_price > ema50
    )
    below_trend = bool(
        cmp_price and ema20 and ema50 and cmp_price < ema20 and cmp_price < ema50
    )

    # ---- momentum triggers ---------------------------------------------------
    rsi_rising = (rsi_now is not None and rsi_5d is not None and rsi_now > rsi_5d)
    rsi_falling = (rsi_now is not None and rsi_5d is not None and rsi_now < rsi_5d)
    vol_confirm = (vol_ratio or 0) >= 1.2

    bull_trigger = (
        above_trend and rsi_now is not None and (
            (40 <= rsi_now <= 55 and rsi_rising)          # pullback reclaim
            or (rsi_now is not None and rsi_3d is not None and rsi_3d < 35 and rsi_now > rsi_3d and vol_confirm)  # oversold bounce
        )
    )
    bear_trigger = (
        below_trend and rsi_now is not None and (
            (45 <= rsi_now <= 60 and rsi_falling)          # rejection turn down
            or (rsi_now is not None and rsi_now < 40 and rsi_falling)  # breakdown momentum
        )
    )

    # chop-zone guard
    chop = rsi_now is not None and 45 <= rsi_now <= 55 and not rsi_rising and not rsi_falling

    # ---- levels ---------------------------------------------------------------
    def pct(v, p):
        return round(v * (1 + p), 2) if v else None

    bull_t1, bull_t2 = pct(cmp_price, 0.04), pct(cmp_price, 0.09)
    bull_sl = swing_low or pct(cmp_price, -0.02)
    bear_t1, bear_t2 = pct(cmp_price, -0.04), pct(cmp_price, -0.09)
    bear_sl = swing_high or pct(cmp_price, 0.02)

    # ---- option structure -----------------------------------------------------
    step = _strike_step(cmp_price)
    atm = _round_strike(cmp_price)
    # sell strike ~2% OTM (one or two steps)
    otm_offset = max(step, int(round(cmp_price * 0.02 / step) * step))

    # estimated debit ~0.9% of underlying for ATM option (labelled estimate)
    est_leg_premium = cmp_price * 0.009
    est_debit = round(est_leg_premium, 2)
    width = otm_offset
    est_max_profit = round(width - est_debit, 2)
    rr = round(est_max_profit / est_debit, 2) if est_debit else None

    # ---- decision --------------------------------------------------------------
    if bull_trigger and not bear_trigger:
        bias, setup = "BULLISH", "Bull Call Spread"
        long_strike, short_strike, opt = atm, atm + otm_offset, "CE"
        t1, t2, sl = bull_t1, bull_t2, bull_sl
        trigger_txt = (
            f"RSI {rsi_now:.1f} reclaimed from pullback zone with price above EMA20/EMA50"
        )
    elif bear_trigger and not bull_trigger:
        bias, setup = "BEARISH", "Bear Put Spread"
        long_strike, short_strike, opt = atm, atm - otm_offset, "PE"
        t1, t2, sl = bear_t1, bear_t2, bear_sl
        trigger_txt = (
            f"RSI {rsi_now:.1f} rejected 50-60 zone with price below EMA20/EMA50"
        )
    else:
        bias = "NEUTRAL"
        # nearest breakout/breakdown levels
        up_level = swing_high or (ema20 or cmp_price * 1.01)
        dn_level = swing_low or (ema20 or cmp_price * 0.99)
        return f"""### {inst.symbol} ({ys}) — Options Strategy Analysis
*Generated {date.today().isoformat()} · Swing horizon 5–10 sessions*

## 1. Market Structure & Catalyst Check
- **CMP:** ₹{cmp_price:,.2f} | **vs EMA20:** {'above' if cmp_price > (ema20 or 0) else 'below'} | **vs EMA50:** {'above' if cmp_price > (ema50 or 0) else 'below'} | **vs 200SMA:** {'above' if cmp_price > (sma200 or 0) else 'below' if sma200 else 'N/A'}
- **RSI(14):** {rsi_now:.1f} (3d ago: {('%.1f' % rsi_3d) if rsi_3d is not None else 'N/A'}, slope: {'rising' if rsi_rising else 'falling' if rsi_falling else 'flat'}) | **Volume:** {vol_ratio:.2f}x 20d avg
- **Catalysts/Seasonality:** No event calendar in data feed — check NSE announcements & quarterly cycle manually.

## 2. Underlying Cash Chart Levels
- **Directional Bias:** NO TRADE — Wait for Confirmation
- **Entry Trigger Zone:** Breakout above ₹{up_level:,.2f} (swing high) for longs, or breakdown below ₹{dn_level:,.2f} (swing low) for shorts
- **T1 / T2 / SL:** Await trigger — levels recompute on confirmation

## 3. Defined-Risk Options Strategy
**NO TRADE** — {"chop zone (RSI 45–55 flat, no EMA support)" if chop else "no valid RSI+EMA trigger"}. Wait for the breakout/breakdown levels above before constructing a spread.
"""

    monthly_expiry = _next_monthly_expiry()
    rr_ok = "✅" if (rr and rr >= 2) else "⚠️ below 1:2 mandate"

    return f"""### {inst.symbol} ({ys}) — Options Strategy Analysis
*Generated {date.today().isoformat()} · Swing horizon 5–10 sessions · Monthly expiry ~{monthly_expiry}*

## 1. Market Structure & Catalyst Check
- **CMP:** ₹{cmp_price:,.2f} | **Trend:** {'above' if cmp_price > (ema20 or 0) else 'below'} EMA20 (₹{ema20:,.2f}), {'above' if cmp_price > (ema50 or 0) else 'below'} EMA50 (₹{ema50:,.2f}), {'above' if cmp_price > (sma200 or 0) else 'below' if sma200 else 'N/A'} 200SMA
- **RSI(14):** {rsi_now:.1f} (5d ago {rsi_5d:.1f}, slope {'rising' if rsi_rising else 'falling'}) | **Volume:** {vol_ratio:.2f}x 20d avg
- **Catalysts/Seasonality:** No event calendar in data feed — verify earnings/macro on NSE before entry.

## 2. Underlying Cash Chart Levels (Swing: 5–10 sessions)
- **Directional Bias:** {bias}
- **Entry Trigger Zone:** {trigger_txt} — enter on next session open or 15m VWAP reclaim
- **Target 1 (2–4 sessions):** ₹{t1:,.2f}
- **Target 2 (5–10 sessions):** ₹{t2:,.2f}
- **Strict Cash Stop-Loss:** ₹{sl:,.2f} (recent {'swing low' if bullish_side(bias) else 'swing high'})

## 3. Defined-Risk Options Strategy (Monthly Expiry)
- **Strategy:** {setup} — {"Buy ATM Call + Sell OTM Call" if bias == "BULLISH" else "Buy ATM Put + Sell OTM Put"}
- **Leg 1 (Long):** Buy {long_strike} {opt}
- **Leg 2 (Short):** Sell {short_strike} {opt}
- **Spread Width:** ₹{width:,.2f}
- **Estimated Net Debit (Max Risk):** ~₹{est_debit:,.2f} *per unit* — ESTIMATE (~0.9% of spot); verify actual premiums on NSE option chain
- **Maximum Profit Potential:** ~₹{est_max_profit:,.2f} (width − debit)
- **Risk-to-Reward:** ~1:{rr} {rr_ok}
- **Exit Rules:** Book ~50% at T1, rest at T2 or expiry; hard exit if underlying closes beyond ₹{sl:,.2f}

> ⚠️ **Data constraint:** premiums are model estimates, not live quotes. Confirm net debit & Greeks on the NSE option chain before placing the order. Defined-risk structure caps max loss at net debit paid.
"""


def bullish_side(bias: str) -> bool:
    return bias == "BULLISH"


def _next_monthly_expiry() -> str:
    import calendar
    from datetime import date as _d

    today = _d.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    for d in range(last_day, 0, -1):
        if _d(today.year, today.month, d).weekday() == 3:
            return _d(today.year, today.month, d).isoformat()
    return today.isoformat()
