"""Simulated multi-instrument data feed (MOCK_REPLAY_MODE).

Generates realistic NIFTY futures + ATM CE/PE ticks via random walk seeded
from the real NIFTY spot (Yahoo). Aggregates into 1m/5m OHLCV+OI bars.
Replace with Kite Connect WebSocket when credentials are available.
"""

from __future__ import annotations

import math
import random
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MOCK_REPLAY_MODE = True
REPLAY_SPEED = 60.0  # demo speed: ~1 sim-minute per real second


class SimFeed:
    """Synthetic tick engine for NIFTY futures + ATM CE/PE."""

    def __init__(self, spot_seed: float = 25000.0, strike_step: int = 50) -> None:
        self.spot = spot_seed
        self.strike_step = strike_step
        self.atm_strike = self._nearest_strike(spot_seed)
        # per-instrument state: last price, day open, bars
        self._state = {
            "FUT": {"price": spot_seed, "open": spot_seed, "bars": {}},
            "CE": {"price": 120.0, "open": 120.0, "bars": {}},
            "PE": {"price": 115.0, "open": 115.0, "bars": {}},
        }
        self._minute = self._current_minute()
        self._day_open_min = self._minute
        self.started_at = time.time()

    # ------------------------------------------------------------------
    @staticmethod
    def _nearest_strike(price: float, step: int = 50) -> int:
        return int(round(price / step) * step)

    @staticmethod
    def _current_minute() -> int:
        n = datetime.now(IST)
        return n.hour * 60 + n.minute

    # ------------------------------------------------------------------
    def _opt_price(self, kind: str) -> float:
        """Model ATM option premium from spot moneyness + time value."""
        K = self.atm_strike
        S = self.spot
        intrinsic = max(0.0, S - K) if kind == "CE" else max(0.0, K - S)
        moneyness = abs(S - K) / K
        tv = 95.0 * math.exp(-8 * moneyness) * (0.6 + 0.4 * math.sin(time.time() / 300))
        return max(1.0, intrinsic + tv + random.gauss(0, 1.5))

    # ------------------------------------------------------------------
    def tick(self) -> dict:
        """Advance one simulated tick; returns instrument prices."""
        # futures random walk with mild momentum + mean reversion to open
        drift = (self._state["FUT"]["open"] - self.spot) * 0.0002
        shock = random.gauss(0, self.spot * 0.0004)
        self.spot = max(100.0, self.spot + shock + drift)
        self.atm_strike = self._nearest_strike(self.spot, self.strike_step)

        prices = {"FUT": self.spot}
        for kind in ("CE", "PE"):
            base = self._opt_price(kind)
            noise = random.gauss(0, base * 0.004)
            prices[kind] = max(0.5, base + noise)

        self._aggregate(prices)
        return prices

    # ------------------------------------------------------------------
    def _aggregate(self, prices: dict) -> None:
        minute = self._current_minute()
        # compress: each real tick advances simulated clock by REPLAY_SPEED
        sim_minute = self._day_open_min + int((time.time() - self.started_at) * REPLAY_SPEED / 60)
        key = sim_minute % (24 * 60)
        for kind, px in prices.items():
            st = self._state[kind]
            st["price"] = px
            bar = st["bars"].setdefault(key, {
                "time": key, "open": px, "high": px, "low": px,
                "close": px, "volume": 0, "oi": 0,
            })
            bar["high"] = max(bar["high"], px)
            bar["low"] = min(bar["low"], px)
            bar["close"] = px
            bar["volume"] += random.randint(50, 500)
            bar["oi"] += random.randint(-30, 30)

    # ------------------------------------------------------------------
    def candles(self, kind: str, period_min: int = 5, limit: int = 78) -> list[dict]:
        """Return OHLCV bars aggregated to `period_min` buckets."""
        bars1m = self._state[kind]["bars"]
        if not bars1m:
            return []
        buckets: dict[int, dict] = {}
        for t, b in sorted(bars1m.items()):
            bk = (t // period_min) * period_min
            agg = buckets.setdefault(bk, {
                "time": bk, "open": b["open"], "high": b["high"],
                "low": b["low"], "close": b["close"],
                "volume": 0, "oi": b["oi"],
            })
            agg["high"] = max(agg["high"], b["high"])
            agg["low"] = min(agg["low"], b["low"])
            agg["close"] = b["close"]
            agg["volume"] += b["volume"]
        return list(buckets.values())[-limit:]

    def snapshot(self) -> dict:
        return {
            "spot": round(self.spot, 2),
            "atm_strike": self.atm_strike,
            "prices": {k: round(v["price"], 2) for k, v in self._state.items()},
        }
