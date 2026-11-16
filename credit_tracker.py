"""Credit Tracker Agent — strike-aware performance recording for credit strategies.

Standalone agent that logs defined-risk credit recommendations (spreads and
Iron Condors) into ``pick_history.json`` via the shared ``pick_history``
module, including short/long strikes so outcomes can later be scored against
the actual spread structure rather than raw spot direction.

Source label: ``credit_tracker`` — appears as its own agent in the
Performance tab.
"""

from __future__ import annotations

import logging
from typing import Optional

from pick_history import in_record_window, record_pick

logger = logging.getLogger("credit_tracker")

SOURCE = "credit_tracker"


def record_credit_pick(
    source_symbol: str,
    side: str,
    entry: float,
    short_strike: Optional[float] = None,
    long_strike: Optional[float] = None,
    strategy: Optional[str] = None,
) -> bool:
    """Record a credit recommendation for performance tracking.

    side: "bullish" (bull put spread), "bearish" (bear call spread),
          or "neutral" (iron condor / credit spread).
    Returns True if the pick was written (False outside market hours or dup).
    """
    if not in_record_window():
        logger.debug("Skipping credit pick outside market hours (09:30-15:15 IST).")
        return False
    ok = record_pick(
        source=SOURCE, side=side, symbol=source_symbol,
        entry_price=entry, short_strike=short_strike, long_strike=long_strike,
    )
    if ok and strategy:
        # tag the row with the strategy name for later analysis
        try:
            import json
            from pick_history import PICK_HISTORY_FILE
            history = json.loads(open(PICK_HISTORY_FILE, encoding="utf-8").read())
            for h in reversed(history):
                if (h.get("source"), h.get("side"), h.get("symbol")) == (SOURCE, side, source_symbol):
                    h["strategy"] = strategy
                    break
            with open(PICK_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history[-500:], f, indent=2)
        except Exception:
            pass
    return ok


def record_spread(
    symbol: str,
    direction: str,
    underlying_price: float,
    short_strike: float,
    long_strike: float,
    est_credit: Optional[float] = None,
) -> bool:
    """Record a directional credit spread (Bull Put / Bear Call)."""
    side = "bullish" if direction == "bull_put" else "bearish"
    return record_credit_pick(
        symbol, side, underlying_price,
        short_strike=short_strike, long_strike=long_strike,
        strategy=direction,
    )


def record_iron_condor(
    symbol: str,
    underlying_price: float,
    put_short: float,
    put_long: float,
    call_short: Optional[float] = None,
    call_long: Optional[float] = None,
    est_credit: Optional[float] = None,
) -> bool:
    """Record an Iron Condor as a neutral pick.

    Put-side strikes are stored as the primary range boundary; call strikes
    are appended to the strategy tag for reference.
    """
    strategy = "iron_condor"
    if call_short is not None:
        strategy += f"|call {call_short}/{call_long}"
    return record_credit_pick(
        symbol, "neutral", underlying_price,
        short_strike=put_short, long_strike=put_long,
        strategy=strategy,
    )
