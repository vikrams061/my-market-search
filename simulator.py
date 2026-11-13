"""Virtual options paper-trading engine for the F&O simulation dashboard."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MAX_TRADES_PER_SESSION = 3
RR_TARGET = 2.5  # 1:2.5 risk-reward on option premium
HARD_SQUAREOFF_MIN = 15 * 60 + 15  # 3:15 PM IST


class PaperTrader:
    """One active virtual option position at a time, max 3 per session."""

    def __init__(self) -> None:
        self.position: Optional[dict] = None
        self.ledger: list[dict] = []
        self.trade_count = 0
        self.session_date = datetime.now(IST).date().isoformat()

    # ------------------------------------------------------------------
    def _reset_if_new_day(self) -> None:
        today = datetime.now(IST).date().isoformat()
        if today != self.session_date:
            self.session_date = today
            self.trade_count = 0
            self.ledger = []
            if self.position:
                self.position["status"] = "CARRYOVER"

    # ------------------------------------------------------------------
    def _ist_minutes(self) -> int:
        n = datetime.now(IST)
        return n.hour * 60 + n.minute

    # ------------------------------------------------------------------
    def try_enter(self, state: str, kind: str, premium: float, strike: int) -> Optional[dict]:
        """Enter a virtual long option when a valid state fires."""
        self._reset_if_new_day()
        if self.position or self.trade_count >= MAX_TRADES_PER_SESSION:
            return None
        if state not in {"BUY_CALL", "BUY_PUT"}:
            return None
        side = "CE" if state == "BUY_CALL" else "PE"
        if side != kind:
            return None
        # premium sanity: don't chase absurd prints
        if premium <= 0 or premium > 2000:
            return None
        risk = premium * 0.20          # assume ~20% premium stop
        target = premium + risk * RR_TARGET
        sl = premium - risk
        self.position = {
            "side": side, "strike": strike, "entry": premium,
            "sl": round(sl, 2), "target": round(target, 2),
            "qty": 75, "status": "OPEN", "entry_time": time.time(),
        }
        self.trade_count += 1
        return self.position

    # ------------------------------------------------------------------
    def try_exit(self, kind: str, premium: float,
                 fut_invalid: bool = False, opt_vwap_break: bool = False) -> Optional[dict]:
        """Check SL / TP / time-stop / invalidation exits."""
        self._reset_if_new_day()
        p = self.position
        if not p or p.get("status") != "OPEN" or kind != p["side"]:
            return None

        reason = None
        if premium <= p["sl"] or opt_vwap_break or fut_invalid:
            reason = "SL/INVALIDATION"
        elif premium >= p["target"]:
            reason = "TARGET 1:2.5"
        elif self._ist_minutes() >= HARD_SQUAREOFF_MIN:
            reason = "TIME STOP 15:15"

        if not reason:
            return None
        p["exit"] = premium
        p["exit_reason"] = reason
        p["pnl_points"] = round(premium - p["entry"], 2)
        p["pnl_inr"] = round(p["pnl_points"] * p["qty"], 2)
        p["status"] = "CLOSED"
        self.ledger.append(dict(p))
        self.position = None
        return p

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        self._reset_if_new_day()
        realized = sum(t.get("pnl_inr", 0) for t in self.ledger)
        wins = sum(1 for t in self.ledger if t.get("pnl_points", 0) > 0)
        unreal = 0.0
        if self.position and self.position.get("status") == "OPEN":
            unreal = self.position.get("unreal_pnl", 0.0)
        return {
            "active": self.position,
            "trades_today": self.trade_count,
            "ledger": self.ledger[-10:],
            "realized_inr": round(realized, 2),
            "wins": wins,
            "losses": len(self.ledger) - wins,
            "unrealized_inr": round(unreal, 2),
        }
