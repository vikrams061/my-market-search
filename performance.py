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
OUTCOMES_FILE = "pick_outcomes.json"
TRADE_OUTCOMES_FILE = "trade_outcomes.json"
TUNING_FILE = "agent_tuning.json"
TRADES_FILE = "manual_trades.json"
CACHE_TTL_SECONDS = 120  # refresh prices every 2 min; UI polls every 30s
OUTCOME_HORIZONS = (0, 1)  # T+0 = pick-day close, T+1 = next trading day close
MIN_TRADE_SAMPLE = 8  # min evaluated trades before an agent is judged


class ManualTradeTracker:
    """User-managed trades with entry/target/SL performance tracking."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def _load(self) -> list[dict]:
        try:
            return json.loads(open(TRADES_FILE, encoding="utf-8").read())
        except Exception:
            return []

    def _save(self, trades: list[dict]) -> None:
        with open(TRADES_FILE, "w", encoding="utf-8") as f:
            json.dump(trades[-200:], f, indent=2)

    def list(self) -> list[dict]:
        return self._load()

    def add(self, trade: dict) -> dict:
        with self._lock:
            trades = self._load()
            asset_type = trade.get("asset_type", "EQ").upper()
            rec = {
                "id": f"t{int(time.time() * 1000)}",
                "symbol": trade["symbol"].upper(),
                "kind": trade.get("kind", "EQ").upper(),
                "asset_type": asset_type,
                "side": trade.get("side", "long").lower(),
                "entry_price": float(trade.get("entry_price") or trade.get("entry_credit") or 0),
                "target": float(trade["target"]) if trade.get("target") else None,
                "stop_loss": float(trade["stop_loss"]) if trade.get("stop_loss") else None,
                "added_at": date.today().isoformat(),
                "status": "OPEN",
            }
            if asset_type == "OPTION":
                und = trade.get("underlying", rec["symbol"]).upper()
                rec.update({
                    "underlying": und,
                    "expiry": trade.get("expiry") or "",
                    "strike": float(trade["strike"]),
                    "opt_type": trade.get("opt_type", "CE").upper(),
                    "underlying_entry": self._spot(und),
                })
            elif asset_type == "SPREAD":
                und = trade.get("underlying", rec["symbol"]).upper()
                rec.update({
                    "underlying": und,
                    "strategy": trade.get("strategy", "Spread"),
                    "opt_type": trade.get("opt_type", "CE").upper(),
                    "short_strike": float(trade["short_strike"]),
                    "long_strike": float(trade["long_strike"]),
                    "expiry": trade.get("expiry") or "",
                    "entry_credit": float(trade["entry_credit"]),
                    "underlying_entry": self._spot(und),
                })
            trades.append(rec)
            self._save(trades)
            return rec

    @staticmethod
    def _spot(symbol: str) -> Optional[float]:
        try:
            import yfinance as yf
            idx = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}
            return float(yf.Ticker(idx.get(symbol, f"{symbol}.NS")).fast_info.last_price)
        except Exception:
            return None

    @staticmethod
    def _est_option_premium(t: dict, und_price: float) -> Optional[float]:
        """Estimate current option premium from underlying move + time decay.

        Model: intrinsic value now + entry time-value decayed linearly to
        expiry, scaled by moneyness drift. LABELLED ESTIMATE — not a quote.
        """
        try:
            K, ot, entry = t["strike"], t["opt_type"], t["entry_price"]
            S0 = t.get("underlying_entry") or und_price
            intr0 = max(0.0, (S0 - K) if ot == "CE" else (K - S0))
            intr1 = max(0.0, (und_price - K) if ot == "CE" else (K - und_price))
            tv0 = max(entry - intr0, entry * 0.10)
            try:
                exp = date.fromisoformat(t.get("expiry") or "")
                total = max((exp - date.fromisoformat(t["added_at"])).days, 1)
                left = max((exp - date.today()).days, 0)
            except Exception:
                total, left = 30, 15
            decay = max(left / total, 0.02)
            money = 0.5 + 0.5 * min(intr1 / max(intr0, 1.0), 2.0)
            return round(intr1 + tv0 * decay * money, 2)
        except Exception:
            return None

    def remove(self, trade_id: str) -> bool:
        with self._lock:
            trades = self._load()
            kept = [t for t in trades if t.get("id") != trade_id]
            if len(kept) == len(trades):
                return False
            self._save(kept)
            return True

    @staticmethod
    def _est_spread_credit(t: dict, und_price: float) -> Optional[float]:
        """Estimate remaining credit on a vertical spread via moneyness pressure.

        CE spread (bear call): profits as underlying stays below short strike;
        credit decays toward 0 as price approaches/passes long strike.
        PE spread (bull put): mirror. LABELLED ESTIMATE.
        """
        try:
            entry = t["entry_credit"]
            K1, K2, ot = t["short_strike"], t["long_strike"], t["opt_type"]
            if ot == "CE":
                width = max(K2 - K1, 1e-9)
                pressure = max(0.0, min(1.0, (und_price - K1) / width))
            else:
                width = max(K1 - K2, 1e-9)
                pressure = max(0.0, min(1.0, (K1 - und_price) / width))
            return round(entry * (1 - 0.85 * pressure), 2)
        except Exception:
            return None

    def evaluate(self, prices: dict[str, Optional[float]]) -> list[dict]:
        """Annotate each trade with current price, status and P&L %."""
        out = []
        for t in self._load():
            at = t.get("asset_type")
            if at == "SPREAD":
                und_price = prices.get(t.get("underlying") or t["symbol"])
                entry = t.get("entry_credit")
                cur = self._est_spread_credit(t, und_price) if und_price and entry else None
                pnl = round((entry - cur) / entry * 100, 2) if (cur is not None and entry) else None
            elif at == "OPTION":
                und_price = prices.get(t.get("underlying") or t["symbol"])
                cur = self._est_option_premium(t, und_price) if und_price else None
                entry = t.get("entry_price")
                pnl = round((cur / entry - 1) * 100, 2) if (cur and entry) else None
            else:
                cur = prices.get(t["symbol"])
                entry = t.get("entry_price")
                side = t.get("side", "long")
                direction = 1 if side == "long" else -1
                pnl = round((cur / entry - 1) * 100 * direction, 2) if (cur and entry) else None

            status = "OPEN"
            if cur and entry:
                tgt, sl = t.get("target"), t.get("stop_loss")
                if at == "SPREAD":
                    und_price = prices.get(t.get("underlying") or t["symbol"])
                    if und_price and sl:
                        invalid = (t.get("opt_type") == "CE" and und_price >= sl) or \
                                  (t.get("opt_type") == "PE" and und_price <= sl)
                        if invalid:
                            status = "STOPPED"
                        elif tgt and cur <= tgt:
                            status = "TARGET HIT"
                elif at == "OPTION":
                    # premium terms: long premium position only
                    if sl and cur <= sl:
                        status = "STOPPED"
                    elif tgt and cur >= tgt:
                        status = "TARGET HIT"
                else:
                    direction = 1 if t.get("side", "long") == "long" else -1
                    hit_target = tgt and ((direction == 1 and cur >= tgt) or (direction == -1 and cur <= tgt))
                    hit_sl = sl and ((direction == 1 and cur <= sl) or (direction == -1 and cur >= sl))
                    if hit_sl:
                        status = "STOPPED"
                    elif hit_target:
                        status = "TARGET HIT"
            out.append({**t, "current": cur, "pnl_pct": pnl, "status": status})
        return out


class PerformanceAgent:
    """Computes and caches performance stats for all recorded picks."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._lock = threading.Lock()
        self._cache: Optional[dict] = None
        self._cache_at: float = 0.0
        self._tracker = ManualTradeTracker()

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
        # score matured picks into pick_outcomes.json once the market is closed
        if self._market_closed():
            try:
                self.score_pending()
            except Exception as exc:
                self._logger.warning("Outcome scoring failed: %s", exc)
            try:
                self.evaluate_trades()
            except Exception as exc:
                self._logger.warning("Trade evaluation failed: %s", exc)
        try:
            history = json.loads(open(PICK_HISTORY_FILE, encoding="utf-8").read())
        except Exception:
            history = []

        today = date.today().isoformat()
        raw_trades = self._tracker.list()
        trade_symbols = set()
        for t in raw_trades:
            trade_symbols.add(t["symbol"])
            if t.get("asset_type") == "OPTION" and t.get("underlying"):
                trade_symbols.add(t["underlying"])
        prices = self._fetch_prices(history + [{"symbol": s} for s in trade_symbols])
        trades = self._tracker.evaluate(prices)

        rows = []
        for h in history:
            cur = prices.get(h["symbol"])
            entry = h.get("entry_price")
            ret = None
            if cur and entry:
                raw = (cur / entry - 1) * 100
                side = h.get("side")
                # neutral (Iron Condor) profits in flat markets -> penalize |move|
                ret = round(-abs(raw) if side == "neutral" else (raw if side == "bullish" else -raw), 2)
            # T1 target: stored on new rows; fallback to the scanner's 0.8% rule
            target = h.get("target")
            if target is None and entry:
                if h.get("side") == "bullish":
                    target = round(entry * 1.008, 2)
                elif h.get("side") == "bearish":
                    target = round(entry * 0.992, 2)
            rows.append({
                "date": h.get("date"),
                "time": h.get("time"),
                "source": h.get("source"),
                "side": h.get("side"),
                "symbol": h.get("symbol"),
                "entry": entry,
                "target": target,
                "current": cur,
                "return_pct": ret,
                "is_today": h.get("date") == today,
            })

        stats = self._stats(rows)
        outcomes = self._outcome_rows()
        trade_stats = self._trade_stats()
        try:
            tuning = json.loads(open(TUNING_FILE, encoding="utf-8").read())
        except Exception:
            tuning = {}
        return {
            "generated_at": time.time(),
            "total_picks": len(rows),
            "stats": stats,
            "tuning": tuning,
            "outcome_stats": self._outcome_stats(outcomes),
            "trade_stats": trade_stats,
            "outcomes": sorted(outcomes, key=lambda r: r.get("exit_date") or "", reverse=True)[:50],
            "rows": sorted(rows, key=lambda r: r.get("date") or "", reverse=True)[:40],
            "trades": trades,
        }

    # ------------------------------------------------------------------
    # Fixed-horizon outcome scoring (append-only pick_outcomes.json)
    # ------------------------------------------------------------------
    @staticmethod
    def _load_outcomes() -> list[dict]:
        try:
            return json.loads(open(OUTCOMES_FILE, encoding="utf-8").read())
        except Exception:
            return []

    @staticmethod
    def _save_outcomes(rows: list[dict]) -> None:
        with open(OUTCOMES_FILE, "w", encoding="utf-8") as f:
            json.dump(rows[-2000:], f, indent=2)

    @staticmethod
    def _outcome_rows() -> list[dict]:
        try:
            return json.loads(open(OUTCOMES_FILE, encoding="utf-8").read())
        except Exception:
            return []

    def score_pending(self) -> int:
        """Score unscored picks at fixed horizons; appends to pick_outcomes.json.

        A horizon is only scored once its target trading day is fully closed
        (past date, or today after market close) so intraday bars never leak in.
        Returns the number of new outcome rows written.
        """
        try:
            history = json.loads(open(PICK_HISTORY_FILE, encoding="utf-8").read())
        except Exception:
            return 0
        outcomes = self._load_outcomes()
        done = {
            (o.get("date"), o.get("source"), o.get("side"), o.get("symbol"), o.get("horizon"))
            for o in outcomes
        }
        pending = [
            h for h in history
            if h.get("entry_price") and h.get("date")
            and any((h["date"], h.get("source"), h.get("side"), h.get("symbol"), hz) not in done
                    for hz in OUTCOME_HORIZONS)
        ]
        if not pending:
            return 0

        today = date.today()
        market_closed = self._market_closed()
        idx_map = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "^CNXFIN", "MIDCPNIFT": "^CNXMID"}
        new_rows: list[dict] = []

        by_symbol: dict[str, list[dict]] = {}
        for h in pending:
            by_symbol.setdefault(h["symbol"], []).append(h)

        import yfinance as yf

        def fetch(sym: str):
            try:
                ys = idx_map.get(sym, f"{sym}.NS")
                df = yf.Ticker(ys).history(period="6mo", interval="1d", auto_adjust=False)
                if df is None or df.empty:
                    return sym, None
                df.index = df.index.tz_localize(None) if df.index.tz is not None else df.index
                return sym, df
            except Exception:
                return sym, None

        with ThreadPoolExecutor(max_workers=6) as pool:
            frames = dict(pool.map(fetch, by_symbol.keys()))

        for sym, picks in by_symbol.items():
            df = frames.get(sym)
            if df is None or df.empty:
                continue
            dates = [d.date() for d in df.index]
            closes = [float(c) for c in df["Close"]]
            for h in picks:
                try:
                    pick_date = date.fromisoformat(h["date"])
                except Exception:
                    continue
                if pick_date not in dates:
                    continue
                pos = dates.index(pick_date)
                entry = float(h["entry_price"])
                side = h.get("side")
                for hz in OUTCOME_HORIZONS:
                    key = (h["date"], h.get("source"), h.get("side"), sym, hz)
                    if key in done:
                        continue
                    tpos = pos + hz
                    if tpos >= len(dates):
                        continue
                    target_date = dates[tpos]
                    # only score fully-closed sessions
                    if target_date > today or (target_date == today and not market_closed):
                        continue
                    raw = (closes[tpos] / entry - 1) * 100
                    ret = round(-abs(raw) if side == "neutral" else (raw if side == "bullish" else -raw), 2)
                    new_rows.append({
                        "date": h["date"], "time": h.get("time"), "source": h.get("source"),
                        "side": h.get("side"), "symbol": sym,
                        "entry": round(entry, 2), "horizon": hz,
                        "exit_date": target_date.isoformat(),
                        "exit_price": round(closes[tpos], 2),
                        "return_pct": ret,
                    })
                    done.add(key)

        if new_rows:
            outcomes.extend(new_rows)
            self._save_outcomes(outcomes)
            self._logger.info("Scored %d pick outcomes.", len(new_rows))
        return len(new_rows)

    # ------------------------------------------------------------------
    # Trade-replay outcomes (trade_eval.py → trade_outcomes.json)
    # ------------------------------------------------------------------
    def evaluate_trades(self) -> int:
        """Replay unscored picks against their target/SL on 15m bars.

        Only runs for fully-closed sessions; each pick is evaluated once.
        Returns the number of new outcome rows written.
        """
        import trade_eval

        try:
            history = json.loads(open(PICK_HISTORY_FILE, encoding="utf-8").read())
        except Exception:
            return 0
        outcomes = trade_eval.load_outcomes()
        done = {trade_eval.outcome_key(o) for o in outcomes}
        new_rows = trade_eval.evaluate_pending(
            history, done, self._market_closed(), date.today(), self._logger,
        )
        if new_rows:
            outcomes.extend(new_rows)
            trade_eval.save_outcomes(outcomes)
        return len(new_rows)

    @staticmethod
    def _wilson_lower(wins: int, n: int, z: float = 1.96) -> Optional[float]:
        """Wilson score lower bound for a binomial proportion (95%)."""
        if n == 0:
            return None
        p = wins / n
        denom = 1 + z * z / n
        centre = p + z * z / (2 * n)
        margin = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
        return round(max(0.0, (centre - margin) / denom) * 100, 1)

    def _trade_stats(self) -> dict:
        """Expectancy stats per source from replayed trade outcomes."""
        try:
            rows = json.loads(open(TRADE_OUTCOMES_FILE, encoding="utf-8").read())
        except Exception:
            rows = []

        def agg(sub: list[dict]) -> dict:
            if not sub:
                return {"n": 0}
            rets = [r["ret_pct"] for r in sub if r.get("ret_pct") is not None]
            wins = [r for r in sub if r.get("result") == "win"]
            losses = [r for r in sub if r.get("result") == "loss"]
            timeouts = [r for r in sub if r.get("result") == "timeout"]
            gross_w = sum(r["ret_pct"] for r in wins if (r.get("ret_pct") or 0) > 0)
            gross_l = abs(sum(r["ret_pct"] for r in losses if (r.get("ret_pct") or 0) < 0))
            return {
                "n": len(sub),
                "wins": len(wins), "losses": len(losses), "timeouts": len(timeouts),
                "win_rate": round(100 * len(wins) / len(sub), 1),
                "wr_lower_95": self._wilson_lower(len(wins), len(sub)),
                "avg_ret": round(sum(rets) / len(rets), 2) if rets else None,
                "avg_win": round(gross_w / len(wins), 2) if wins else None,
                "avg_loss": round(-gross_l / len(losses), 2) if losses else None,
                "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
                "avg_mfe": round(sum(r.get("mfe_pct") or 0 for r in sub) / len(sub), 2),
                "avg_mae": round(sum(r.get("mae_pct") or 0 for r in sub) / len(sub), 2),
            }

        by_src: dict[str, list[dict]] = {}
        for r in rows:
            by_src.setdefault(r.get("source") or "?", []).append(r)
        return {
            "n": len(rows),
            "overall": agg(rows),
            "by_source": {s: agg(v) for s, v in sorted(by_src.items())},
        }

    @staticmethod
    def _outcome_stats(rows: list[dict]) -> dict:
        """Per-source stats at each horizon from scored outcomes."""
        out: dict[str, Any] = {}
        for hz in OUTCOME_HORIZONS:
            sub = [r for r in rows if r.get("horizon") == hz and r.get("return_pct") is not None]
            if not sub:
                continue
            by_src: dict[str, list[float]] = {}
            for r in sub:
                by_src.setdefault(r.get("source") or "?", []).append(r["return_pct"])
            out[f"T+{hz}"] = {
                "n": len(sub),
                "avg_return": round(sum(r["return_pct"] for r in sub) / len(sub), 2),
                "win_rate": round(100 * sum(1 for r in sub if r["return_pct"] > 0) / len(sub), 1),
                "by_source": {
                    s: {
                        "n": len(v),
                        "avg": round(sum(v) / len(v), 2),
                        "win_rate": round(100 * sum(1 for x in v if x > 0) / len(v), 1),
                    } for s, v in sorted(by_src.items())
                },
            }
        return out

    # ------------------------------------------------------------------
    def _fetch_prices(self, history: list[dict]) -> dict[str, Optional[float]]:
        """Fetch latest prices for all symbols in history (deduped)."""
        symbols = {h["symbol"] for h in history if h.get("symbol")}
        prices: dict[str, Optional[float]] = {}
        if not symbols:
            return prices

        import yfinance as yf

        idx_map = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "^CNXFIN", "MIDCPNIFT": "^CNXMID"}

        def get(sym: str):
            try:
                ys = idx_map.get(sym, f"{sym}.NS")
                return sym, float(yf.Ticker(ys).fast_info.last_price)
            except Exception:
                return sym, None

        with ThreadPoolExecutor(max_workers=8) as pool:
            for sym, price in pool.map(get, symbols):
                prices[sym] = price
        return prices

    # ------------------------------------------------------------------
    # Meta-analysis: findings + suggestions to improve agent pick quality
    # ------------------------------------------------------------------
    @staticmethod
    def _market_closed() -> bool:
        """True outside NSE equity hours (9:15–15:30 IST, Mon–Fri)."""
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        if now.weekday() >= 5:
            return True
        mins = now.hour * 60 + now.minute
        return mins < 9 * 60 + 15 or mins >= 15 * 60 + 30

    def suggestions(self, force: bool = False) -> dict:
        """Analyse performance rows and produce improvement recommendations.

        ``force=True`` runs the analysis on demand even during market hours
        (intraday mark-to-market stats are noisier — findings say so).
        """
        if not force and not self._market_closed():
            return {
                "findings": ["Market is still open — optimizer analysis runs after 15:30 IST."],
                "tuning": {},
            }
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

        # --- per-source weak spots (trade-replay expectancy, significance-gated) --
        # Prefer replayed target/SL outcomes; only judge a source once it has
        # MIN_TRADE_SAMPLE evaluated trades, and only tighten/relax on a
        # statistically meaningful edge (Wilson lower bound below 50% for
        # losers; positive expectancy for relaxations).
        trade_stats = report.get("trade_stats") or {}
        ts_by_src = (trade_stats.get("by_source") or {})
        gated = False
        for src, st in ts_by_src.items():
            n = st.get("n", 0)
            if n < MIN_TRADE_SAMPLE:
                continue
            gated = True
            avg = st.get("avg_ret")
            pf = st.get("profit_factor")
            wr_lb = st.get("wr_lower_95")
            if avg is not None and avg < 0 and (pf is None or pf < 1.0) and (wr_lb is None or wr_lb < 50):
                findings.append(
                    f"'{src}' underperforms on replayed trades (n={n}, avg {avg:+.2f}%, "
                    f"PF {pf}, WR floor {wr_lb}%). Restricting to high-liquidity names."
                )
                tuning[f"{src}_liquidity_filter"] = True
            elif avg is not None and avg > 0.3:
                # healthy edge — relax a previously applied liquidity restriction
                tuning[f"{src}_liquidity_filter"] = False
        if not gated:
            by_src = report.get("stats", {}).get("by_source", {})
            for src, st in by_src.items():
                if st.get("scored", 0) >= 3 and st.get("avg_return", 0) < -0.5:
                    findings.append(
                        f"'{src}' agent underperforming (avg {st['avg_return']:+.2f}%, "
                        f"win rate {st.get('win_rate', 0)}%). Its universe may be too broad — "
                        "restrict to top-liquid names or add a liquidity filter."
                    )
                    tuning[f"{src}_liquidity_filter"] = True

        # persist tuning so scanners pick it up on the next scan.
        # Merge with existing file: agent-managed keys are updated, manual
        # keys the agent doesn't touch are preserved.
        if tuning:
            try:
                try:
                    existing = json.loads(open(TUNING_FILE, encoding="utf-8").read())
                except Exception:
                    existing = {}
                merged = {**existing, **tuning}
                with open(TUNING_FILE, "w", encoding="utf-8") as f:
                    json.dump(merged, f, indent=2)
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
