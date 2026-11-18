"""Trade-outcome evaluator — replays each recorded pick against its own
target/stop-loss on real 15-minute bars.

For every ``pick_history`` row that carries explicit ``target`` and
``stop_loss`` levels, this module walks the pick day's 15m candles forward
from the entry bar and records the first level touched:

- ``win``     — target touched before stop-loss
- ``loss``    — stop-loss touched first
- ``timeout`` — neither level touched by session close; marked at close

Rules:
- Entry bar = first 15m bar starting at/after the recorded pick time
  (avoids look-ahead inside the entry candle). Picks without a recorded
  time are evaluated from the session open.
- A candle whose range contains BOTH levels resolves pessimistically to
  ``loss`` — intrabar ordering is unknowable.
- Rows without explicit levels (credit spreads, legacy picks) are skipped;
  only trades with defined exits enter expectancy stats.

Results persist to ``trade_outcomes.json`` keyed by
``(date, source, side, symbol)`` so each pick is evaluated exactly once.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

TRADE_OUTCOMES_FILE = "trade_outcomes.json"
BAR_INTERVAL = "15m"
IST = ZoneInfo("Asia/Kolkata")

_logger = logging.getLogger("trade_eval")

IDX_MAP = {
    "NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "^CNXFIN", "MIDCPNIFT": "^CNXMID",
}


def yf_symbol(symbol: str) -> str:
    sym = (symbol or "").upper()
    return IDX_MAP.get(sym, f"{sym}.NS")


def outcome_key(row: dict) -> tuple:
    return (row.get("date"), row.get("source"), row.get("side"), row.get("symbol"))


def load_outcomes() -> list[dict]:
    try:
        return json.loads(open(TRADE_OUTCOMES_FILE, encoding="utf-8").read())
    except Exception:
        return []


def save_outcomes(rows: list[dict]) -> None:
    try:
        with open(TRADE_OUTCOMES_FILE, "w", encoding="utf-8") as f:
            json.dump(rows[-2000:], f, indent=2)
    except Exception as exc:
        _logger.warning("Could not save trade outcomes: %s", exc)


def fetch_bars(symbol: str, start: date, end: date) -> Optional[pd.DataFrame]:
    """15m OHLCV bars for ``symbol`` over [start, end], IST-aware index."""
    import yfinance as yf

    try:
        df = yf.Ticker(yf_symbol(symbol)).history(
            start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
            interval=BAR_INTERVAL, auto_adjust=False,
        )
    except Exception as exc:
        _logger.debug("Bar fetch failed for %s: %s", symbol, exc)
        return None
    if df is None or df.empty:
        return None
    if df.index.tz is not None:
        df.index = df.index.tz_convert(IST)
    else:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    return df


def _entry_bar_index(day: pd.DataFrame, pick_time: Optional[str]) -> int:
    """Index of the first bar starting at/after the recorded pick time."""
    if not pick_time:
        return 0
    try:
        hh, mm = (int(x) for x in str(pick_time).split(":")[:2])
        cutoff = dtime(hh, mm)
    except Exception:
        return 0
    times = [ts.time() for ts in day.index]
    for i, t in enumerate(times):
        if t >= cutoff:
            return i
    return len(day) - 1


def evaluate_pick(row: dict, day: pd.DataFrame) -> Optional[dict]:
    """Replay one pick's day bars; return outcome dict or None if unevaluable."""
    try:
        entry = float(row["entry_price"])
        target = float(row["target"])
        sl = float(row["stop_loss"])
    except (KeyError, TypeError, ValueError):
        return None
    if entry <= 0 or target <= 0 or sl <= 0:
        return None

    side = row.get("side")
    direction = 1 if side == "bullish" else (-1 if side == "bearish" else 0)
    if direction == 0:
        return None

    day = day.dropna(subset=["High", "Low", "Close"])
    if day.empty:
        return None

    i0 = _entry_bar_index(day, row.get("time"))
    seg = day.iloc[i0:]
    if seg.empty:
        return None

    tgt_hit = sl_hit = False
    exit_price = exit_time = None
    result = "timeout"
    mfe = mae = 0.0

    for ts, bar in seg.iterrows():
        hi, lo, cl = float(bar["High"]), float(bar["Low"]), float(bar["Close"])
        fav = (hi - entry) if direction == 1 else (entry - lo)
        adv = (entry - lo) if direction == 1 else (hi - entry)
        mfe = max(mfe, fav / entry * 100)
        mae = max(mae, adv / entry * 100)

        hit_t = (hi >= target) if direction == 1 else (lo <= target)
        hit_s = (lo <= sl) if direction == 1 else (hi >= sl)
        if hit_t and hit_s:
            result, exit_price = "loss", sl
        elif hit_t:
            result, exit_price = "win", target
        elif hit_s:
            result, exit_price = "loss", sl
        if result != "timeout":
            exit_time = ts
            break
    else:
        exit_price = float(seg.iloc[-1]["Close"])
        exit_time = seg.index[-1]

    ret = round((exit_price / entry - 1) * 100 * direction, 2)
    return {
        "date": row.get("date"), "time": row.get("time"),
        "source": row.get("source"), "side": side,
        "symbol": row.get("symbol"),
        "entry": round(entry, 2), "target": target, "stop_loss": sl,
        "result": result,
        "exit_time": exit_time.strftime("%H:%M") if exit_time is not None else None,
        "exit_price": round(exit_price, 2),
        "ret_pct": ret,
        "mfe_pct": round(mfe, 2), "mae_pct": round(mae, 2),
    }


def evaluate_pending(history: list[dict], done_keys: set[tuple],
                     market_closed_today: bool, today: date,
                     logger: Optional[logging.Logger] = None) -> list[dict]:
    """Evaluate pick_history rows that have levels and aren't scored yet.

    Only sessions that have fully closed are evaluated (past dates, or
    today after market close). Groups bar fetches per symbol.
    Returns newly evaluated outcome rows.
    """
    log = logger or _logger

    def session_closed(d_str: str) -> bool:
        try:
            d = date.fromisoformat(d_str)
        except Exception:
            return False
        return d < today or (d == today and market_closed_today)

    pending = [
        h for h in history
        if h.get("entry_price") and h.get("date") and h.get("target") and h.get("stop_loss")
        and outcome_key(h) not in done_keys and session_closed(h["date"])
    ]
    if not pending:
        return []

    by_symbol: dict[str, list[dict]] = {}
    for h in pending:
        by_symbol.setdefault(h["symbol"], []).append(h)

    start = min(date.fromisoformat(h["date"]) for h in pending) - timedelta(days=2)
    end = today + timedelta(days=1)

    frames: dict[str, Optional[pd.DataFrame]] = {}
    for sym in by_symbol:
        frames[sym] = fetch_bars(sym, start, end)

    new_rows: list[dict] = []
    for sym, picks in by_symbol.items():
        df = frames.get(sym)
        if df is None or df.empty:
            continue
        df["_d"] = df.index.date
        for h in picks:
            try:
                d = date.fromisoformat(h["date"])
            except Exception:
                continue
            day = df[df["_d"] == d]
            res = evaluate_pick(h, day)
            if res is not None:
                new_rows.append(res)
                done_keys.add(outcome_key(h))

    if new_rows:
        log.info("Evaluated %d trade outcomes.", len(new_rows))
    return new_rows
