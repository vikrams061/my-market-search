"""Intraday Credit-Spread / Iron-Condor Strategist.

Regime-filtered defined-risk option SELLING strategies:
- Time-of-day windows (IST), Session VWAP + 15m/60m Opening Range,
  RVOL, India VIX trend -> regime classification.
- Bull Put Spread / Bear Call Spread / Intraday Iron Condor.
- Every recommendation is recorded into pick_history.json (source="credit")
  so the Performance agent tracks daily wins/losses.

All premiums are ESTIMATES — verify on the NSE option chain.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from agent import _ema, _rsi, _sma

IST = ZoneInfo("Asia/Kolkata")
PICK_HISTORY_FILE = "pick_history.json"


def _ist_now() -> tuple[int, int]:
    now = datetime.now(IST)
    return now.hour, now.minute


def _time_window() -> str:
    h, m = _ist_now()
    t = h * 60 + m
    if t < 10 * 60 + 15:
        return "OPENING (9:15–10:15) — observe only"
    if t < 13 * 60 + 30:
        return "MEAN-REVERSION (10:15–13:30) — credit spreads prime"
    if t < 15 * 60 + 15:
        return "TREND-CONTINUATION (13:30–15:15) — VWAP breakouts"
    return "CLOSED (post 15:20 IST hard square-off)"


def _intraday_frame(tk: Any) -> dict:
    df = tk.history(period="5d", interval="15m", auto_adjust=False)
    if df is None or df.empty or len(df) < 30:
        return {}
    today = df.index[-1].date()
    day = df[df.index.date == today] if hasattr(df.index, "date") else df.tail(30)
    if day.empty:
        day = df.tail(30)
    closes = day["Close"].dropna()
    if closes.empty:
        return {}
    if {"High", "Low", "Volume"}.issubset(day.columns):
        typical = (day["High"] + day["Low"] + day["Close"]) / 3
        vol = day["Volume"].fillna(0)
        vwap = float((typical * vol).sum() / vol.sum()) if vol.sum() else None
    else:
        vwap = None
    or_bar = day.iloc[0]
    prior = df[df.index.date < today] if hasattr(df.index, "date") else df.iloc[:-len(day)]
    avg_prior = float(prior["Volume"].tail(80).mean()) if len(prior) else None
    rvol = round(float(day["Volume"].mean()) / avg_prior, 2) if avg_prior else None
    last = float(closes.iloc[-1])
    # 60m opening range = first 4 bars
    or60 = day.head(4)
    return {
        "vwap": round(vwap, 2) if vwap else None,
        "orh": round(float(or_bar["High"]), 2),
        "orl": round(float(or_bar["Low"]), 2),
        "or60h": round(float(or60["High"].max()), 2),
        "or60l": round(float(or60["Low"].min()), 2),
        "last": round(last, 2),
        "rvol": rvol,
        "above_vwap": (last > vwap) if vwap else None,
    }


def _india_vix() -> dict:
    try:
        import yfinance as yf
        fi = yf.Ticker("^INDIAVIX").fast_info
        last = float(fi.last_price)
        prev = float(fi.previous_close) if fi.previous_close else None
        return {
            "value": round(last, 2),
            "chg_pct": round((last / prev - 1) * 100, 2) if prev else None,
        }
    except Exception:
        return {"value": None, "chg_pct": None}


def _record_credit_pick(source_symbol: str, side: str, entry: float) -> None:
    """Append this recommendation to shared pick history for perf tracking."""
    try:
        try:
            history = json.loads(open(PICK_HISTORY_FILE, encoding="utf-8").read())
        except Exception:
            history = []
        key = (date.today().isoformat(), "credit", side)
        if any((h.get("date"), h.get("source"), h.get("side")) == key for h in history):
            return
        history.append({
            "date": date.today().isoformat(),
            "source": "credit",
            "side": side,
            "symbol": source_symbol,
            "entry_price": entry,
            "score": None,
        })
        with open(PICK_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[-500:], f, indent=2)
    except Exception:
        pass


def analyze_credit(symbol: str, kind: str, logger: logging.Logger) -> str:
    """Regime-filtered intraday credit strategy (markdown)."""
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
    intra = {}
    try:
        intra = _intraday_frame(tk)
    except Exception as exc:
        logger.debug("Credit intraday frame failed for %s: %s", symbol, exc)

    hist = tk.history(period="6mo", interval="1d", auto_adjust=False)
    cmp_price = ema20 = ema50 = rsi_now = vol_ratio = None
    swing_high = swing_low = None
    if hist is not None and not hist.empty and len(hist) >= 60:
        closes = [float(c) for c in hist["Close"].dropna()]
        volumes = [float(v) for v in hist["Volume"].dropna()] if "Volume" in hist.columns else []
        cmp_price = closes[-1]
        ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
        rsi_now = _rsi(closes, 14)
        try:
            w = hist.tail(20)
            swing_high, swing_low = float(w["High"].max()), float(w["Low"].min())
        except Exception:
            pass
        if volumes:
            avg20 = sum(volumes[-20:]) / min(20, len(volumes))
            vol_ratio = round(volumes[-1] / avg20, 2) if avg20 else None

    price = intra.get("last") or cmp_price
    if not price:
        raise RuntimeError(f"No price data for {symbol}")

    vwap = intra.get("vwap")
    orh, orl = intra.get("orh"), intra.get("orl")
    or60h, or60l = intra.get("or60h"), intra.get("or60l")
    rvol = intra.get("rvol")
    above_vwap = intra.get("above_vwap")
    vix = _india_vix()
    window = _time_window()
    h_now, m_now = _ist_now()

    # ---------------- regime classification ---------------------------------
    vix_spike = (vix.get("chg_pct") or 0) > 8
    chop = (above_vwap is not None and abs(price - (vwap or price)) / (vwap or price) < 0.003
            and (rvol or 1) < 1.0)

    if "CLOSED" in window or "OPENING" in window:
        regime = "NO-TRADE — time window"
        strat = None
    elif vix_spike:
        regime = "NO-TRADE — Elevated VIX/Event risk"
        strat = None
    elif chop:
        regime = "Range-Bound Mean Reversion"
        strat = "Iron Condor"
    elif above_vwap:
        regime = "Trending Bullish / Neutral-Bullish"
        strat = "Bull Put Spread"
    else:
        regime = "Trending Bearish / Neutral-Bearish"
        strat = "Bear Call Spread"

    # ---------------- strikes ---------------------------------------------------
    def _k(p):
        step = 50 if p < 1000 else 100
        return int(round(p / step) * step)

    if strat == "Bull Put Spread":
        short_k = _k((orl or vwap or price) * 0.997)   # below ORL/support
        long_k = _k(short_k - price * 0.03)           # ~3% tail hedge
        side = "bullish"
    elif strat == "Bear Call Spread":
        short_k = _k((orh or vwap or price) * 1.003)   # above ORH/resistance
        long_k = _k(short_k + price * 0.03)
        side = "bearish"
    elif strat == "Iron Condor":
        put_short = _k((orl or vwap or price) * 0.995)
        put_long = _k(put_short - price * 0.03)
        call_short = _k((orh or vwap or price) * 1.005)
        call_long = _k(call_short + price * 0.03)
        side = "neutral"
    else:
        put_short = put_long = call_short = call_long = None
        side = "none"

    # estimated credit ~0.35% of underlying for 1-lot spread unit (ESTIMATE)
    est_credit = round(price * 0.0035, 2) if price else None
    width = abs((long_k or 0) - (short_k or 0)) or (abs((call_long or 0) - (call_short or 0)) or None)
    est_max_risk = round((width or 0) - (est_credit or 0), 2) if width and est_credit else None
    pop = "~65–72% (range-bound mean reversion)" if strat == "Iron Condor" else "~60–68% (defined-risk credit)"

    entry_ok = "10:15–15:15 IST" if strat else "—"

    # record pick for performance tracking (directional only)
    if side in {"bullish", "bearish"}:
        _record_credit_pick(inst.symbol, side, price)

    # ---------------- build track payloads ------------------------------------
    def _spread_track(strat_name: str, s_k: int, l_k: int, ot: str, sl_level: Optional[float]) -> dict:
        return {
            "asset_type": "SPREAD",
            "symbol": inst.symbol,
            "kind": inst.kind,
            "underlying": inst.symbol,
            "strategy": strat_name,
            "opt_type": ot,
            "short_strike": s_k,
            "long_strike": l_k,
            "expiry": _next_monthly_expiry(),
            "entry_credit": est_credit,
            "target": round((est_credit or 0) * 0.30, 2),   # 70% credit decay
            "stop_loss": sl_level,                          # underlying invalidation
        }

    tracks: list[dict] = []
    if strat == "Iron Condor":
        tracks = [
            _spread_track("Iron Condor (Put)", put_short, put_long, "PE", or60l),
            _spread_track("Iron Condor (Call)", call_short, call_long, "CE", or60h),
        ]
    elif strat:
        tracks = [_spread_track(
            strat, short_k, long_k,
            "PE" if side == "bullish" else "CE",
            or60l if side == "bullish" else or60h,
        )]

    header = f"""### {inst.symbol} ({ys}) — Intraday Credit Strategy
*Generated {date.today().isoformat()} {h_now:02d}:{m_now:02d} IST · Window: {window}*

## 1. Intraday Market Regime & Volatility Check

| Parameter | Value |
| :--- | :--- |
| Spot vs Session VWAP | ₹{price:,.2f} vs {('₹' + format(vwap, ',.2f')) if vwap else 'N/A'} → **{'Above' if above_vwap else 'Below' if above_vwap is False else 'At/N-A'}** |
| Opening Range (15m) | ORH ₹{orh:,.2f} \| ORL ₹{orl:,.2f} |
| Opening Range (60m) | ₹{or60l:,.2f} – ₹{or60h:,.2f} |
| India VIX | {('₹' + format(vix['value'], '.2f')) if vix.get('value') else 'N/A'} ({(('%+.2f' % vix['chg_pct']) + '%') if vix.get('chg_pct') is not None else 'N/A'}){' ⚠️ SPIKE' if vix_spike else ''} |
| Relative Volume | {rvol if rvol else 'N/A'}x |
| Daily RSI(14) | {('%.0f' % rsi_now) if rsi_now is not None else 'N/A'} |
| Trend vs EMA20 | {'above' if cmp_price and ema20 and cmp_price > ema20 else 'below' if cmp_price and ema20 else 'N/A'} |
| **Regime** | **{regime}** |
"""

    if not strat:
        return {
            "markdown": header + f"""
## 2. Strategy

| Check | Result |
| :--- | :--- |
| Decision | **NO TRADE — {'Elevated Event/Gamma Risk' if vix_spike or 'CLOSED' in window or 'OPENING' in window else 'No regime edge'}** |
| Required to re-enter | VWAP structure formed + RVOL normalized (>1.0) + valid regime window |
""",
            "track": [],
        }

    if strat == "Iron Condor":
        body = f"""
## 2. High-Probability Options Strategy ({_next_monthly_expiry()})

| Leg | Action | Strike | Type | Role |
| :--- | :--- | :--- | :--- | :--- |
| Short Put | Sell | {put_short} | PE | Theta engine — below ORL |
| Long Put | Buy | {put_long} | PE | Tail hedge ~3% |
| Short Call | Sell | {call_short} | CE | Theta engine — above ORH |
| Long Call | Buy | {call_long} | CE | Tail hedge ~3% |

| Metric | Value |
| :--- | :--- |
| Strategy | Intraday Iron Condor |
| Est. Net Credit | ~₹{est_credit:,.2f} *ESTIMATE* |
| Max Defined Risk | ~₹{est_max_risk:,.2f} / unit |
| POP estimate | {pop} |

## 3. Execution, Stop-Loss & Time-Stop

| Rule | Level |
| :--- | :--- |
| Entry Window | {entry_ok} — after 10:15 AM once VWAP bands stabilize |
| Profit Target | Exit at 65–75% credit decay (~₹{round((est_credit or 0) * 0.3, 2):,.2f} remaining) |
| Underlying Stop | Exit all legs if 5m candle closes beyond ₹{or60h:,.2f} / ₹{or60l:,.2f} |
| Hard Square-Off | 3:15 PM IST mandatory — no overnight risk |
"""
    else:
        opt = "PE" if side == "bullish" else "CE"
        body = f"""
## 2. High-Probability Options Strategy ({_next_monthly_expiry()})

| Leg | Action | Strike | Type | Placement |
| :--- | :--- | :--- | :--- | :--- |
| Short | Sell | {short_k} | {opt} | {'Below ORL/support' if side == 'bullish' else 'Above ORH/resistance'} |
| Long | Buy | {long_k} | {opt} | ~3% tail hedge |

| Metric | Value |
| :--- | :--- |
| Strategy | {strat} |
| Est. Net Credit | ~₹{est_credit:,.2f} *ESTIMATE* |
| Max Defined Risk | ~₹{est_max_risk:,.2f} / unit |
| POP estimate | {pop} |

## 3. Execution, Stop-Loss & Time-Stop

| Rule | Level |
| :--- | :--- |
| Entry Window | {entry_ok} |
| Profit Target | Exit at 65–75% credit decay |
| Underlying Stop | Exit if 5m candle closes beyond {'₹' + format(or60l or 0, ',.2f') if side == 'bullish' else '₹' + format(or60h or 0, ',.2f')} |
| Hard Square-Off | 3:15 PM IST mandatory |
"""

    return {
        "markdown": header + body + """
> ⚠️ Credits/premiums are estimates — confirm on NSE option chain. Position sizing: max 1–2% of capital per setup. Never 100% win rate — expectancy + discipline only.
""",
        "track": tracks,
    }


def _next_monthly_expiry() -> str:
    import calendar
    from datetime import date as _d

    today = _d.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    for d in range(last_day, 0, -1):
        if _d(today.year, today.month, d).weekday() == 3:
            return _d(today.year, today.month, d).isoformat()
    return today.isoformat()
