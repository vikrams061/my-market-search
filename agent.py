"""Equity Analyst Agent.

Gathers real market data for the current watchlist (prices, technical
indicators, news headlines, option-chain availability via yfinance), then
sends a structured analyst brief to Groq's LLM and returns a markdown report.

The agent is provider-agnostic at the prompt level — only the Groq call is
implemented (OpenAI-compatible chat completions API).
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Optional

import requests

from config import Settings
from market_watch import Instrument, MarketWatch

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.3-70b-versatile"
MAX_WATCHLIST_FOR_ANALYSIS = 8  # bound enrichment latency
REQUEST_TIMEOUT = 120


# ---------------------------------------------------------------------------
# System prompt — the analyst persona/instructions
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """Act as a Senior SEBI-Registered Equity Analyst, Quantitative Intraday Momentum Trader, and Derivatives Specialist. Your goal is to scan liquid NSE F&O equities and identify TWO high-conviction INTRADAY momentum setups targeting the NEXT 15 MINUTES TO 2 HOURS of price movement:
1. STOCK #1: Strongest Bullish Momentum (Long Candidate)
2. STOCK #2: Strongest Bearish Momentum (Short / Bearish Strategy Candidate)

CRITICAL INSTRUCTIONS:
- You will be given a DATA PACKET containing real collected market data (quotes, daily technicals, 15-minute intraday metrics: VWAP, 15m RSI, 1h momentum, recent news) for the user's watchlist, plus today's date. Base your analysis ONLY on this data plus well-established technical principles.
- Do NOT hallucinate option prices, strike premiums, or exact tick data that is not in the data packet. If option chain data is absent, say so explicitly and give the strategy structure with strikes relative to CMP instead of fabricated premiums.
- If a metric is unavailable (e.g., delivery %, option chain), state the data constraint explicitly rather than guessing.

PHASES TO COVER IN YOUR REPORT:
1. Market Catalyst & News Scan — classify sentiment +10 (highly bullish) to -10 (highly bearish) using the provided news; note missing data (FII/DII flows, brokerage notes) as constraints.
2. Cash Market Structure & Volume — volume vs 20-day average, delivery % if available, CMP vs 20EMA/50SMA/200SMA.
3. Multi-Timeframe Technicals — intraday (15m VWAP, 15m RSI, 1h momentum) as PRIMARY trigger, daily structure (RSI14, MACD, EMAs) as trend filter, supports/resistances, expected duration 15 min – 2 hours.
4. Pattern & Setup ID — intraday flags, VWAP bounces/rejections, opening-range breaks, near key levels (infer from provided OHLC data where possible; state when unverifiable).
5. Trade Execution Parameters — INTRADAY scalps for BOTH stocks: T1 & T2 targets achievable within 15–120 minutes, tight intraday stop-loss, risk:reward >= 1:2, directional probability %.
6. Derivatives Strategy — current weekly/monthly expiry defined-risk intraday strategy (Bull Call Spread / Bear Put Spread or equivalent), strike selection logic vs CMP and VWAP, max risk/reward structure. If no option chain data, give strikes relative to CMP and label premiums as "to be checked on NSE option chain" — do NOT invent premium numbers.
7. Executive Summary — end with a markdown comparison table of both picks covering: ticker, CMP, catalyst, pattern, T1/T2, stop-loss, volume/delivery, option strategy, premium structure.

FORMAT: GitHub-flavored markdown with clear section headers. Be concise but complete — this renders in a web dashboard."""


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------
def _yahoo_symbol(watch: MarketWatch, inst: Instrument) -> str:
    return watch._yahoo_symbol(inst)


def _rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) <= period:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _macd(closes: list[float]) -> Optional[dict]:
    """Return MACD line, signal, histogram from daily closes."""
    if len(closes) < 35:
        return None
    # EMA12 - EMA26 series
    def ema_series(vals, period):
        k = 2.0 / (period + 1)
        out = []
        e = sum(vals[:period]) / period
        out.extend([None] * (period - 1) + [e])
        for v in vals[period:]:
            e = v * k + e * (1 - k)
            out.append(e)
        # align: out has len(vals)-period+1 entries; pad front
        return [None] * (len(vals) - len(out)) + out

    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    macd_line = [
        (a - b) if (a is not None and b is not None) else None
        for a, b in zip(e12, e26)
    ]
    valid = [m for m in macd_line if m is not None]
    if len(valid) < 9:
        return None
    signal = _ema(valid, 9)
    hist_vals = [m - signal for m in valid[-9:]] if signal is not None else []
    return {
        "macd": round(valid[-1], 2),
        "signal": round(signal, 2) if signal is not None else None,
        "histogram": round(hist_vals[-1], 2) if hist_vals else None,
    }


def collect_market_data(watch: MarketWatch, logger: logging.Logger) -> dict:
    """Build the data packet for the analyst from the watchlist."""
    import yfinance as yf

    quotes = {q["symbol"]: q for q in watch.snapshot()}
    symbols = [
        inst for inst in watch._instruments.values()
        if inst.symbol in quotes
    ][:MAX_WATCHLIST_FOR_ANALYSIS]

    packet: dict[str, Any] = {
        "as_of": date.today().isoformat(),
        "data_source": watch._settings.data_source,
        "data_delay_note": (
            "Yahoo Finance data is ~15 minutes delayed" if watch._settings.data_source == "yahoo" else "Groww market data"
        ),
        "instruments": [],
    }

    for inst in symbols:
        entry: dict[str, Any] = {
            "symbol": inst.symbol,
            "kind": inst.kind,
            "exchange": inst.exchange,
            "yahoo_ticker": _yahoo_symbol(watch, inst),
        }
        q = quotes.get(inst.symbol, {})
        entry["quote"] = {
            "ltp": q.get("ltp"),
            "change_pct": q.get("change_pct"),
            "volume": q.get("volume"),
            "last_trade_time": q.get("last_trade_time"),
        }

        try:
            tk = yf.Ticker(entry["yahoo_ticker"])
            hist = tk.history(period="6mo", interval="1d", auto_adjust=False)
            if hist is not None and not hist.empty:
                closes = [float(c) for c in hist["Close"].dropna()]
                volumes = [float(v) for v in hist["Volume"].dropna()] if "Volume" in hist else []
                entry["technicals"] = {
                    "cmp": closes[-1],
                    "rsi_14": _rsi(closes),
                    "ema_20": _ema(closes, 20),
                    "ema_50": _ema(closes, 50),
                    "sma_200": _sma(closes, 200),
                    "macd": _macd(closes),
                    "avg_volume_20d": round(sum(volumes[-20:]) / min(20, len(volumes))) if volumes else None,
                    "latest_volume": int(volumes[-1]) if volumes else None,
                    "volume_vs_20d_avg": (
                        round(volumes[-1] / (sum(volumes[-20:]) / min(20, len(volumes))), 2)
                        if volumes and len(volumes) >= 20
                        else None
                    ),
                    "high_52w": max(float(h) for h in hist["High"].dropna()) if "High" in hist else None,
                    "low_52w": min(float(l) for l in hist["Low"].dropna()) if "Low" in hist else None,
                }
                # recent daily candles for pattern inference (last 10)
                entry["recent_daily_candles"] = [
                    {
                        "date": str(idx.date()),
                        "open": round(float(row["Open"]), 2),
                        "high": round(float(row["High"]), 2),
                        "low": round(float(row["Low"]), 2),
                        "close": round(float(row["Close"]), 2),
                        "volume": int(row["Volume"]) if "Volume" in hist.columns else None,
                    }
                    for idx, row in hist.tail(10).iterrows()
                ]

            try:
                entry["intraday"] = _intraday_metrics(tk)
            except Exception:
                entry["intraday"] = {}

            # news headlines
            try:
                news = tk.news or []
                entry["news"] = [
                    {
                        "title": n.get("title"),
                        "publisher": n.get("publisher"),
                        "link": n.get("link"),
                        "provider_publish_time": n.get("providerPublishTime"),
                    }
                    for n in news[:8]
                ]
            except Exception:
                entry["news"] = []

            # option chain availability (NSE via Yahoo is often unavailable)
            try:
                opts = tk.options
                entry["option_chain"] = {
                    "available": bool(opts),
                    "expiries": list(opts)[:3] if opts else [],
                }
            except Exception:
                entry["option_chain"] = {"available": False, "expiries": []}

        except Exception as exc:
            logger.warning("Data enrichment failed for %s: %s", inst.symbol, exc)
            entry["enrichment_error"] = str(exc)

        packet["instruments"].append(entry)

    return packet


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------
def _call_groq(prompt: str, api_key: str, model: str, logger: logging.Logger) -> str:
    """Call Groq's OpenAI-compatible chat completions endpoint."""
    resp = requests.post(
        GROQ_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
            "max_tokens": 4096,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# Trend bias & confidence (shared by scanner picks and quick checks)
# ---------------------------------------------------------------------------
def trend_bias(score: Optional[float]) -> str:
    """Classify momentum score into a trend bias label."""
    if score is None:
        return "UNKNOWN"
    if score >= 15:
        return "BULLISH"
    if score <= -15:
        return "BEARISH"
    return "NEUTRAL"


def confidence_pct(entry: dict) -> int:
    """Estimate pick confidence (0-100) from signal agreement.

    Counts how many independent signals (trend vs EMAs/200SMA, MACD, RSI
    band, volume confirmation, day move, news sentiment) agree with the
    momentum direction, then blends with the magnitude of the score.
    """
    score = entry.get("momentum_score")
    if score is None:
        return 0
    direction = 1 if score > 0 else -1

    tech = entry.get("technicals") or {}
    q = entry.get("quote") or {}
    cmp_price = tech.get("cmp") or q.get("ltp")

    agree = 0
    total = 0

    def vote(bullish: bool, weight: int = 1) -> None:
        nonlocal agree, total
        total += weight
        if bullish == (direction > 0):
            agree += weight

    if cmp_price and tech.get("ema_20"):
        vote(cmp_price > tech["ema_20"])
    if cmp_price and tech.get("ema_50"):
        vote(cmp_price > tech["ema_50"])
    if cmp_price and tech.get("sma_200"):
        vote(cmp_price > tech["sma_200"])

    hist = (tech.get("macd") or {}).get("histogram")
    if hist is not None:
        vote(hist > 0)

    rsi = tech.get("rsi_14")
    if rsi is not None:
        # RSI > 50 leans bullish; extremes count as weaker conviction
        if rsi > 80 or rsi < 20:
            vote(direction > 0)  # extreme momentum in pick direction counts once
        else:
            vote(rsi > 50)

    vol_ratio = tech.get("volume_vs_20d_avg")
    chg = q.get("change_pct")
    if vol_ratio and vol_ratio > 1.2 and chg is not None:
        vote(chg > 0)

    sent, _ = _news_sentiment(entry.get("news") or [])
    if sent != 0:
        vote(sent > 0)

    # intraday agreement (15m VWAP + 1h momentum)
    intra = entry.get("intraday") or {}
    vs_vwap = intra.get("vs_vwap_pct")
    if vs_vwap is not None:
        vote(vs_vwap > 0)
    mom_1h = intra.get("mom_1h_pct")
    if mom_1h is not None and abs(mom_1h) > 0.1:
        vote(mom_1h > 0)

    agreement = agree / total if total else 0.5
    # blend: magnitude contributes up to ~60 pts, agreement up to ~40
    magnitude = min(abs(score) / 100.0, 1.0) * 60
    return int(round(10 + magnitude + agreement * 40))


def analyze_symbol(symbol: str, kind: str, logger: logging.Logger) -> dict:
    """Quick trend/momentum check for ANY stock or index symbol."""
    import yfinance as yf

    from config import Instrument

    inst = Instrument(symbol=symbol.upper(), kind=kind.upper())
    idx_map = {
        "NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "^CNXFIN",
        "MIDCPNIFT": "^CNXMID", "NIFTYNXT50": "^NIFTYNXT50",
    }
    alias = {"HDFC": "HDFCBANK.NS"}.get(inst.symbol)
    ys = alias or (
        idx_map.get(inst.symbol, f"^{inst.symbol}")
        if inst.kind == "INDEX" else f"{inst.symbol}.NS"
    )

    tk = yf.Ticker(ys)
    hist = tk.history(period="6mo", interval="1d", auto_adjust=False)
    if hist is None or hist.empty or len(hist) < 30:
        raise RuntimeError(f"No price data found for {symbol} ({ys})")

    closes = [float(c) for c in hist["Close"].dropna()]
    volumes = [float(v) for v in hist["Volume"].dropna()] if "Volume" in hist.columns else []
    entry = {
        "symbol": inst.symbol,
        "kind": inst.kind,
        "quote": {"ltp": closes[-1], "change_pct": None},
        "technicals": {
            "cmp": closes[-1],
            "rsi_14": _rsi(closes),
            "ema_20": _ema(closes, 20),
            "ema_50": _ema(closes, 50),
            "sma_200": _sma(closes, 200),
            "macd": _macd(closes),
            "avg_volume_20d": round(sum(volumes[-20:]) / min(20, len(volumes))) if volumes else None,
            "latest_volume": int(volumes[-1]) if volumes else None,
            "volume_vs_20d_avg": (
                round(volumes[-1] / (sum(volumes[-20:]) / min(20, len(volumes))), 2)
                if volumes and len(volumes) >= 20 else None
            ),
        },
        "news": [],
    }
    try:
        fi = tk.fast_info
        if getattr(fi, "previous_close", None):
            entry["quote"]["change_pct"] = round((closes[-1] / float(fi.previous_close) - 1) * 100, 2)
    except Exception:
        pass

    try:
        entry["intraday"] = _intraday_metrics(tk)
    except Exception:
        entry["intraday"] = {}

    entry["momentum_score"] = _momentum_score(entry)
    entry["trend_bias"] = trend_bias(entry["momentum_score"])
    entry["confidence"] = confidence_pct(entry)
    return entry


# ---------------------------------------------------------------------------
# Local deterministic analyst (no LLM required)
# ---------------------------------------------------------------------------
_BULL_WORDS = {
    "surges", "soars", "rallies", "jumps", "beats", "upgrade", "outperform",
    "buy", "bullish", "record", "profit", "growth", "misses estimates" ,
    "raises", "strong", "approval", "wins", "partnership", "boost",
}
_BEAR_WORDS = {
    "plunges", "slumps", "falls", "drops", "misses", "downgrade", "sell",
    "bearish", "loss", "cut", "weak", "probe", "penalty", "fine", "lawsuit",
    "concern", "decline", "crash", "warns",
}


def _news_sentiment(news: list[dict]) -> tuple[float, list[str]]:
    """Simple keyword sentiment over headlines -> score in [-1, 1]."""
    score, hits = 0.0, []
    for item in news or []:
        title = (item.get("title") or "").lower()
        b = sum(1 for w in _BULL_WORDS if w in title)
        s = sum(1 for w in _BEAR_WORDS if w in title)
        if b or s:
            d = b - s
            score += d
            hits.append(item.get("title"))
    if not news:
        return 0.0, []
    return max(-1.0, min(1.0, score / max(1, len(news)))), hits[:5]


def _intraday_metrics(tk: Any) -> dict:
    """Fetch 15-minute bars and derive intraday trend metrics."""
    try:
        df = tk.history(period="5d", interval="15m", auto_adjust=False)
        if df is None or df.empty or len(df) < 20:
            return {}
        closes = df["Close"].dropna()
        if len(closes) < 15:
            return {}
        last = float(closes.iloc[-1])

        # session VWAP approximation
        if {"High", "Low", "Volume"}.issubset(df.columns):
            typical = (df["High"] + df["Low"] + df["Close"]) / 3
            vol = df["Volume"].fillna(0)
            vwap = float((typical * vol).sum() / vol.sum()) if vol.sum() else None
        else:
            vwap = None

        closes_list = [float(c) for c in closes]
        mom_1h = None
        if len(closes_list) >= 5:
            mom_1h = round((last / closes_list[-5] - 1) * 100, 3)

        return {
            "rsi_15m": _rsi(closes_list),
            "vwap": round(vwap, 2) if vwap else None,
            "vs_vwap_pct": round((last / vwap - 1) * 100, 3) if vwap else None,
            "mom_1h_pct": mom_1h,
            "price": round(last, 2),
        }
    except Exception:
        return {}


def _momentum_score(entry: dict) -> float:
    """Composite momentum score in roughly [-100, +100] (incl. intraday)."""
    tech = entry.get("technicals") or {}
    q = entry.get("quote") or {}
    intra = entry.get("intraday") or {}
    score = 0.0

    cmp_price = tech.get("cmp") or q.get("ltp")
    ema20, ema50, sma200 = tech.get("ema_20"), tech.get("ema_50"), tech.get("sma_200")
    if cmp_price and ema20:
        score += 15 if cmp_price > ema20 else -15
    if cmp_price and ema50:
        score += 15 if cmp_price > ema50 else -15
    if cmp_price and sma200:
        score += 10 if cmp_price > sma200 else -10

    rsi = tech.get("rsi_14")
    if rsi is not None:
        # momentum sweet spot vs extremes
        if 55 <= rsi <= 75:
            score += 10
        elif rsi > 80 or rsi < 30:
            score -= 8

    macd = tech.get("macd") or {}
    hist = macd.get("histogram")
    if hist is not None:
        score += 10 if hist > 0 else -10

    vol_ratio = tech.get("volume_vs_20d_avg")
    chg = q.get("change_pct")
    if vol_ratio and chg is not None:
        # volume-confirmed directional move
        if vol_ratio > 1.3 and chg > 0:
            score += 10
        elif vol_ratio > 1.3 and chg < 0:
            score -= 10

    sent, _ = _news_sentiment(entry.get("news") or [])
    score += sent * 20

    # --- intraday (15m) momentum component -------------------------------
    vs_vwap = intra.get("vs_vwap_pct")
    if vs_vwap is not None:
        score += 10 if vs_vwap > 0 else -10
    mom_1h = intra.get("mom_1h_pct")
    if mom_1h is not None:
        score += 10 if mom_1h > 0.1 else (-10 if mom_1h < -0.1 else 0)
    rsi_15m = intra.get("rsi_15m")
    if rsi_15m is not None:
        if 55 <= rsi_15m <= 75:
            score += 8
        elif rsi_15m > 80 or rsi_15m < 25:
            score -= 6

    return round(score, 1)


def _next_monthly_expiry(today: date) -> date:
    """Approximate NSE monthly expiry: last Thursday of the month."""
    import calendar

    last_day = calendar.monthrange(today.year, today.month)[1]
    for d in range(last_day, 0, -1):
        candidate = date(today.year, today.month, d)
        if candidate.weekday() == 3:  # Thursday
            return candidate
    return date(today.year, today.month, last_day)


def _fmt_price(v) -> str:
    return f"₹{v:,.2f}" if isinstance(v, (int, float)) else "N/A"


def _stock_section(entry: dict, score: float, bullish: bool, expiry: date) -> str:
    """Render one stock's analysis block as markdown."""
    sym = entry["symbol"]
    tech = entry.get("technicals") or {}
    q = entry.get("quote") or {}
    direction = (
        "BULLISH (Long)" if bullish
        else "BEARISH (Short / Bearish strategy) — *weakest momentum in scan; no confirmed bearish setup today*"
        if score > 0
        else "BEARISH (Short / Bearish strategy)"
    )
    cmp_price = tech.get("cmp") or q.get("ltp")
    rsi = tech.get("rsi_14")
    macd = tech.get("macd") or {}
    vol_ratio = tech.get("volume_vs_20d_avg")
    sent, headlines = _news_sentiment(entry.get("news") or [])
    sentiment_label = "Bullish" if sent > 0.15 else "Bearish" if sent < -0.15 else "Neutral"

    # derived trade params (rule-based)
    if cmp_price:
        if bullish:
            t1 = round(cmp_price * 1.008, 2)
            t2 = round(cmp_price * 1.015, 2)
            sl = round(cmp_price * 0.995, 2)
            strategy = (
                f"Bull Call Spread — buy {_fmt_strike(cmp_price, 'ce')} CE, "
                f"sell 1 strike OTM; expiry {expiry.isoformat()} (intraday hold)"
            )
        else:
            t1 = round(cmp_price * 0.992, 2)
            t2 = round(cmp_price * 0.985, 2)
            sl = round(cmp_price * 1.005, 2)
            strategy = (
                f"Bear Put Spread — buy {_fmt_strike(cmp_price, 'pe')} PE, "
                f"sell 1 strike OTM; expiry {expiry.isoformat()} (intraday hold)"
            )
        rr = abs((t1 - cmp_price) / (cmp_price - sl)) if cmp_price != sl else 0
    else:
        t1 = t2 = sl = rr = None
        strategy = "Awaiting price data"

    news_md = "\n".join(f"- {h}" for h in headlines) if headlines else "- No recent headlines retrieved"
    pattern = "Momentum continuation (rule-based inference from MAs/RSI/MACD)" if score != 0 else "Insufficient data"
    bias = trend_bias(score)
    conf = confidence_pct(entry)

    return f"""### {sym} — {direction}

**Trend Bias:** {bias} | **Confidence:** {conf}% | **Momentum Score:** {score}/100 | **Sentiment:** {sentiment_label} ({sent:+.2f})

- **CMP:** {_fmt_price(cmp_price)} | **Day Chg:** {q.get('change_pct') if q.get('change_pct') is not None else 'N/A'}%
- **RSI(14):** {round(rsi, 1) if rsi is not None else 'N/A'} | **MACD hist:** {macd.get('histogram') if macd.get('histogram') is not None else 'N/A'}
- **EMAs:** 20={_fmt_price(tech.get('ema_20'))}, 50={_fmt_price(tech.get('ema_50'))}, 200SMA={_fmt_price(tech.get('sma_200'))}
- **Volume:** {tech.get('volume_vs_20d_avg', 'N/A')}x 20-day avg (latest {tech.get('latest_volume') or 'N/A'})
- **52w range:** {_fmt_price(tech.get('low_52w'))} – {_fmt_price(tech.get('high_52w'))}

**News scan (auto-sentiment, limited):**
{news_md}

**Trade parameters (rule-based):**
- T1: {_fmt_price(t1)} (15–30 min) | T2: {_fmt_price(t2)} (1–2 hrs)
- Stop-loss: {_fmt_price(sl)} | Indicative R:R ≈ {rr:.1f}:1 | Probability: ~{min(70, max(35, int(50 + abs(score))))}%
- **Option strategy:** {strategy} — *net premium & exact strikes must be verified on the NSE option chain; not available via current data source.*
"""


def _fmt_strike(cmp_price: float, opt: str) -> str:
    """Round CMP to nearest 50 for strike suggestion."""
    step = 50 if cmp_price < 1000 else 100
    strike = round(cmp_price / step) * step
    return f"{strike}{opt.upper()}"


def run_local_analysis(watch: MarketWatch, settings: Settings, logger: logging.Logger) -> str:
    """Deterministic analyst report — no external LLM required."""
    logger.info("Analyst agent: running LOCAL deterministic analysis (no LLM key).")
    packet = collect_market_data(watch, logger)

    scored = []
    for entry in packet["instruments"]:
        s = _momentum_score(entry)
        scored.append((entry, s))
        entry["momentum_score"] = s

    scored.sort(key=lambda x: x[1], reverse=True)
    expiry = _next_monthly_expiry(date.today())

    if len(scored) >= 2:
        bull_entry, bull_score = scored[0]
        bear_entry, bear_score = scored[-1]
        if bear_score > 0 and len(scored) > 1:
            bear_entry, bear_score = scored[1]
    elif scored:
        bull_entry, bull_score = scored[0]
        bear_entry, bear_score = scored[0]
    else:
        raise RuntimeError("No instrument data collected — check symbols and data source.")

    # comparison matrix
    def row(label, b, s_):
        tech_b = (b.get("technicals") or {})
        tech_s = (s_.get("technicals") or {})
        return (
            f"| {label} | {b['symbol']} | {s_['symbol']} |\n"
            f"| CMP | {_fmt_price(tech_b.get('cmp'))} | {_fmt_price(tech_s.get('cmp'))} |\n"
            f"| Momentum score | {b.get('momentum_score')} | {s_.get('momentum_score')} |\n"
            f"| RSI(14) | {round(tech_b['rsi_14'],1) if tech_b.get('rsi_14') is not None else 'N/A'} | {round(tech_s['rsi_14'],1) if tech_s.get('rsi_14') is not None else 'N/A'} |\n"
            f"| Vol vs 20d avg | {tech_b.get('volume_vs_20d_avg', 'N/A')}x | {tech_s.get('volume_vs_20d_avg', 'N/A')}x |\n"
        )

    report = f"""# NSE Momentum Analyst Report — {packet['as_of']}

*Generated by local deterministic engine (no LLM). Data source: {packet['data_source']} ({packet['data_delay_note']}).*

> **Data constraints:** Delivery %, FII/DII flows, brokerage notes, and live option-chain premiums are NOT available from this data source. Option strategies give strike structure only — verify premiums on the NSE option chain before execution. All technicals are computed from daily candles.

## Executive Summary

{row("Parameter", bull_entry, bear_entry)}

## Stock #1 — Strongest Bullish Momentum

{_stock_section(bull_entry, bull_entry.get('momentum_score', 0), True, expiry)}

## Stock #2 — Strongest Bearish Momentum

{_stock_section(bear_entry, bear_entry.get('momentum_score', 0), False, expiry)}

## Methodology & Limitations

- Momentum score = weighted composite of trend vs EMAs/200SMA, RSI band, MACD histogram, volume confirmation, and headline sentiment.
- This is a screening tool, not investment advice. SEBI-registered analyst judgment, option-chain verification, and risk management are essential before any trade.
"""
    return report


def run_analysis(watch: MarketWatch, settings: Settings, logger: logging.Logger) -> str:
    """Collect data and produce the analyst report (markdown)."""
    if not watch.snapshot():
        raise RuntimeError("Watchlist is empty — add at least one instrument first.")

    if not settings.groq_api_key:
        return run_local_analysis(watch, settings, logger)

    logger.info("Analyst agent: collecting market data for %d instruments...", len(watch.snapshot()))
    packet = collect_market_data(watch, logger)

    prompt = (
        "Today is {date}. Analyze the following watchlist and produce the full "
        "report per your instructions. DATA PACKET (JSON):\n\n{data}\n\n"
        "If fewer than 2 instruments are provided, pick the best available and "
        "note the constraint. Prefer F&O-liquid large caps when choosing momentum setups."
    ).format(date=date.today().isoformat(), data=json.dumps(packet, indent=2, default=str))

    logger.info("Analyst agent: calling Groq model %s...", settings.groq_model)
    report = _call_groq(prompt, settings.groq_api_key, settings.groq_model, logger)
    logger.info("Analyst agent: report generated (%d chars).", len(report))
    return report
