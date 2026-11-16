"""Pick History Logger — shared agent for recording trade picks.

All agents (scanners, credit desk, etc.) append recommendations to
``pick_history.json`` through this module so the performance agent can
score them. Guards against recording outside market hours, where stale
entry prices would corrupt stats (entry == close -> flat 0% returns).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
PICK_HISTORY_FILE = "pick_history.json"
MAX_ROWS = 500

_logger = logging.getLogger("pick_history")


def in_record_window() -> bool:
    """True only Mon–Fri 09:30–15:15 IST."""
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= mins <= 15 * 60 + 15


def load_history() -> list[dict]:
    try:
        return json.loads(open(PICK_HISTORY_FILE, encoding="utf-8").read())
    except Exception:
        return []


def save_history(rows: list[dict]) -> None:
    try:
        with open(PICK_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(rows[-MAX_ROWS:], f, indent=2)
    except Exception as exc:
        _logger.warning("Could not save pick history: %s", exc)


def record_pick(
    *,
    source: str,
    side: str,
    symbol: str,
    entry_price: Optional[float],
    score: Optional[float] = None,
    short_strike: Optional[float] = None,
    long_strike: Optional[float] = None,
    pick_date: Optional[str] = None,
) -> bool:
    """Append a pick to history. Dedupes on (date, source, side, symbol).

    Returns True if a row was written. No-op outside market hours.
    """
    if not in_record_window():
        _logger.debug("Skipping pick record outside market hours (09:30-15:15 IST).")
        return False
    d = pick_date or date.today().isoformat()
    history = load_history()
    key = (d, source, side, symbol)
    if any(
        (h.get("date"), h.get("source"), h.get("side"), h.get("symbol")) == key
        for h in history
    ):
        return False
    row: dict[str, Any] = {
        "date": d,
        "time": datetime.now(IST).strftime("%H:%M"),
        "source": source,
        "side": side,
        "symbol": symbol,
        "entry_price": entry_price,
        "score": score,
    }
    if short_strike is not None:
        row["short_strike"] = short_strike
    if long_strike is not None:
        row["long_strike"] = long_strike
    history.append(row)
    save_history(history)
    return True


def upsert_daily_pick(
    *,
    source: str,
    side: str,
    symbol: str,
    entry_price: Optional[float],
    score: Optional[float] = None,
    pick_date: Optional[str] = None,
) -> bool:
    """One row per (date, source, side) — used by scanners.

    If a rescan changes the symbol, the existing row is updated in place so
    history always matches the currently displayed pick.
    """
    if not in_record_window():
        _logger.debug("Skipping pick record outside market hours (09:30-15:15 IST).")
        return False
    d = pick_date or date.today().isoformat()
    history = load_history()
    key = (d, source, side)
    for h in history:
        if (h.get("date"), h.get("source"), h.get("side")) == key:
            if h.get("symbol") != symbol:
                h.update({
                    "symbol": symbol,
                    "entry_price": entry_price,
                    "score": score,
                    "time": datetime.now(IST).strftime("%H:%M"),
                })
                save_history(history)
                return True
            return False
    history.append({
        "date": d,
        "time": datetime.now(IST).strftime("%H:%M"),
        "source": source,
        "side": side,
        "symbol": symbol,
        "entry_price": entry_price,
        "score": score,
    })
    save_history(history)
    return True
