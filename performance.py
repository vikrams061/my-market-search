"""Agent Performance tracker.

Reads ``pick_history.json`` (populated by the scanners), fetches current
prices for every recorded pick, and computes per-source and overall stats:
win rate, average return, best/worst pick, and today's open P&L.

Results are cached briefly to avoid hammering Yahoo Finance on every UI poll.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Optional

PICK_HISTORY_FILE = "pick_history.json"
CACHE_TTL_SECONDS = 300  # refresh prices at most every 5 min


class PerformanceAgent:
    """Computes and caches performance stats for all recorded picks."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._lock = threading.Lock()
        self._cache: Optional[dict] = None
        self._cache_at: float = 0.0

    # ------------------------------------------------------------------
    def report(self, force: bool = False) -> dict:
        """Return the performance report (cached for CACHE_TTL_SECONDS)."""
        with self._lock:
            if not force and self._cache and (time.time() - self._cache_at) < CACHE_TTL_SECONDS:
                return self._cache
            report = self._build_report()
            self._cache = report
            self._cache_at = time.time()
            return report

    # ------------------------------------------------------------------
    def _build_report(self) -> dict:
        try:
            history = json.loads(open(PICK_HISTORY_FILE, encoding="utf-8").read())
        except Exception:
            history = []

        today = date.today().isoformat()
        prices = self._fetch_prices(history)

        rows = []
        for h in history:
            cur = prices.get(h["symbol"])
            entry = h.get("entry_price")
            ret = None
            if cur and entry:
                raw = (cur / entry - 1) * 100
                ret = round(raw if h["side"] == "bullish" else -raw, 2)
            rows.append({
                "date": h.get("date"),
                "source": h.get("source"),
                "side": h.get("side"),
                "symbol": h.get("symbol"),
                "entry": entry,
                "current": cur,
                "return_pct": ret,
                "is_today": h.get("date") == today,
            })

        stats = self._stats(rows)
        return {
            "generated_at": time.time(),
            "total_picks": len(rows),
            "stats": stats,
            "rows": sorted(rows, key=lambda r: r.get("date") or "", reverse=True)[:40],
        }

    # ------------------------------------------------------------------
    def _fetch_prices(self, history: list[dict]) -> dict[str, Optional[float]]:
        """Fetch latest prices for all symbols in history (deduped)."""
        symbols = {h["symbol"] for h in history if h.get("symbol")}
        prices: dict[str, Optional[float]] = {}
        if not symbols:
            return prices

        import yfinance as yf

        def get(sym: str):
            try:
                return sym, float(yf.Ticker(f"{sym}.NS").fast_info.last_price)
            except Exception:
                return sym, None

        with ThreadPoolExecutor(max_workers=8) as pool:
            for sym, price in pool.map(get, symbols):
                prices[sym] = price
        return prices

    # ------------------------------------------------------------------
    # Meta-analysis: findings + suggestions to improve agent pick quality
    # ------------------------------------------------------------------
    def suggestions(self) -> dict:
        """Analyse performance rows and produce improvement recommendations."""
        report = self.report()
        rows = [r for r in report.get("rows", []) if r.get("return_pct") is not None]
        findings: list[str] = []
        tuning: dict[str, Any] = {}

        if len(rows) < 4:
            return {
                "findings": ["Not enough scored picks yet — suggestions appear after ~4 tracked picks."],
                "tuning": tuning,
            }

        # --- per-side analysis -------------------------------------------------
        by_side = {}
        for side in ("bullish", "bearish"):
            rets = [r["return_pct"] for r in rows if r["side"] == side]
            if rets:
                by_side[side] = {
                    "n": len(rets),
                    "avg": sum(rets) / len(rets),
                    "wins": sum(1 for x in rets if x > 0),
                }

        bull_avg = by_side.get("bullish", {}).get("avg")
        bear_avg = by_side.get("bearish", {}).get("avg")
        if bull_avg is not None and bear_avg is not None:
            if bull_avg > 0 > bear_avg:
                findings.append(
                    f"Bullish picks avg {bull_avg:+.2f}% but bearish picks avg {bear_avg:+.2f}% — "
                    "the bearish leg is fighting the tape. Raise the bearish score threshold "
                    "(require momentum_score <= -25) or disable bearish picks in confirmed uptrends."
                )
                tuning["bearish_min_abs_score"] = 25
            elif bear_avg > 0 > bull_avg:
                findings.append(
                    f"Bearish picks avg {bear_avg:+.2f}% but bullish picks avg {bull_avg:+.2f}% — "
                    "long side is underperforming; tighten bullish entry to higher-confidence setups only."
                )
                tuning["bullish_min_confidence"] = 55

        # --- confidence-bucket analysis ---------------------------------------
        # confidence isn't stored in history rows; use score as proxy via picks
        low = [r for r in rows if r.get("return_pct", 0) < 0]
        high = [r for r in rows if r.get("return_pct", 0) >= 0]
        if low and high:
            findings.append(
                f"Losing picks: {len(low)} ({', '.join(sorted({r['symbol'] for r in low}))}). "
                "Review whether these were counter-trend or low-volume setups — consider requiring "
                "volume_vs_20d_avg > 1.0 for pick eligibility."
            )
            tuning["min_volume_ratio"] = 1.0

        # --- overall trend -----------------------------------------------------
        overall = report.get("stats", {}).get("overall", {})
        avg = overall.get("avg_return")
        if avg is not None and avg < 0:
            findings.append(
                f"Overall avg return is negative ({avg:+.2f}%). Recommendation: raise the "
                "confidence floor to 50 and require |momentum_score| >= 20 for a pick to be surfaced."
            )
            tuning.setdefault("min_confidence", 50)
            tuning.setdefault("min_abs_score", 20)
        elif avg is not None:
            findings.append(
                f"Overall avg return is positive ({avg:+.2f}%) — keep current thresholds; "
                "focus on position sizing and target discipline."
            )

        # --- per-source weak spots ----------------------------------------------
        by_src = report.get("stats", {}).get("by_source", {})
        for src, st in by_src.items():
            if st.get("scored", 0) >= 3 and st.get("avg_return", 0) < -0.5:
                findings.append(
                    f"'{src}' agent underperforming (avg {st['avg_return']:+.2f}%, "
                    f"win rate {st.get('win_rate', 0)}%). Its universe may be too broad — "
                    "restrict to top-liquid names or add a liquidity filter."
                )
                tuning[f"{src}_liquidity_filter"] = True

        # persist tuning so scanners pick it up on the next scan
        if tuning:
            try:
                with open("agent_tuning.json", "w", encoding="utf-8") as f:
                    json.dump(tuning, f, indent=2)
                findings.append("Tuning applied → agent_tuning.json (takes effect on next scan/rescan).")
            except Exception as exc:
                findings.append(f"Could not persist tuning: {exc}")

        return {"findings": findings, "tuning": tuning}

    # ------------------------------------------------------------------
    @staticmethod
    def _stats(rows: list[dict]) -> dict:
        """Aggregate stats overall and per source."""
        def agg(subset: list[dict]) -> dict:
            rets = [r["return_pct"] for r in subset if r["return_pct"] is not None]
            if not rets:
                return {"picks": len(subset), "scored": 0}
            wins = sum(1 for r in rets if r > 0)
            return {
                "picks": len(subset),
                "scored": len(rets),
                "win_rate": round(wins / len(rets) * 100, 1),
                "avg_return": round(sum(rets) / len(rets), 2),
                "total_return": round(sum(rets), 2),
                "best": round(max(rets), 2),
                "worst": round(min(rets), 2),
            }

        sources = sorted({r["source"] for r in rows if r.get("source")})
        return {
            "overall": agg(rows),
            "by_source": {s: agg([r for r in rows if r.get("source") == s]) for s in sources},
        }
