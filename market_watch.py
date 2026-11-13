"""Core market-watch engine.

Supports two data paths:

* **Polling mode** (default): periodically calls ``GrowwAPI.get_quote`` for
  every instrument in the watchlist using a thread pool. Respects Groww's
  live-data rate limits by spacing requests across ``poll_interval``.
* **Feed mode** (``USE_FEED=true``): subscribes to Groww's WebSocket feed via
  ``GrowwFeed`` for LTP/index values and merges them with periodically polled
  quote statistics (day change %, volume, last trade time).

The latest snapshot for every instrument is stored in a thread-safe store and
rendered as a live-updating ``rich`` table in the terminal.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from config import Instrument, Settings

try:  # growwapi is an optional import at module level for testability
    from growwapi import GrowwAPI, GrowwFeed
    from growwapi.groww.exceptions import GrowwAPIException, GrowwFeedException
except Exception:  # pragma: no cover - SDK may be absent in lint envs
    GrowwAPI = GrowwFeed = None  # type: ignore[assignment]
    GrowwAPIException = GrowwFeedException = Exception  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Quote:
    """Latest market snapshot for one instrument."""

    symbol: str
    ltp: Optional[float] = None
    change_pct: Optional[float] = None
    volume: Optional[int] = None
    last_trade_time: Optional[int] = None
    updated_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.updated_at


# ---------------------------------------------------------------------------
# Market watch engine
# ---------------------------------------------------------------------------
class MarketWatch:
    """Fetches, stores, and displays live quotes for a watchlist."""

    def __init__(
        self,
        groww: GrowwAPI,
        instruments: list[Instrument],
        settings: Settings,
        logger: logging.Logger,
        console: Optional[Console] = None,
    ) -> None:
        self._groww = groww
        self._settings = settings
        self._logger = logger
        self._console = console or Console()

        self._instruments = {inst.symbol: inst for inst in instruments}
        self._quotes: Dict[str, Quote] = {
            inst.symbol: Quote(symbol=inst.symbol) for inst in instruments
        }
        self._lock = threading.Lock()

        self._stop_event = threading.Event()
        self._feed: Optional[GrowwFeed] = None
        self._feed_thread: Optional[threading.Thread] = None

        # token -> symbol maps used to decode feed payloads
        self._ltp_token_map: Dict[str, str] = {}
        self._index_token_map: Dict[str, str] = {}

        # last-applied feed subscriptions, for clean unsubscribe on shutdown
        self._feed_subscriptions: tuple[list[dict], list[dict]] = ([], [])

        # track consecutive failures for backoff / alerting
        self._consecutive_failures = 0

        # background polling thread (web UI mode)
        self._worker_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self) -> None:
        """Run the market watch until interrupted (Ctrl+C)."""
        self._logger.info(
            "Starting market watch for %d instruments (feed=%s, interval=%.1fs).",
            len(self._instruments),
            self._settings.use_feed,
            self._settings.poll_interval,
        )

        if self._settings.use_feed and self._groww is not None:
            self._start_feed()

        try:
            with Live(
                self._render(),
                console=self._console,
                refresh_per_second=4,
                screen=False,
            ) as live:
                self._polling_loop(live)
        except KeyboardInterrupt:
            self._logger.info("Shutdown requested by user.")
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop polling/feed threads and release resources."""
        self._stop_event.set()

        # Best-effort unsubscribe before tearing down the socket
        if self._feed is not None and (self._feed_subscriptions[0] or self._feed_subscriptions[1]):
            try:
                ltp_list, index_list = self._feed_subscriptions
                if ltp_list:
                    self._feed.unsubscribe_ltp(ltp_list)
                if index_list:
                    self._feed.unsubscribe_index_value(index_list)
                self._logger.info("Feed subscriptions removed.")
            except Exception as exc:
                self._logger.debug("Feed unsubscribe failed (non-fatal): %s", exc)

        feed_close = getattr(self._feed, "close", None) if self._feed else None
        if callable(feed_close):
            try:
                self._logger.info("Closing Groww feed connection.")
                feed_close()
            except Exception as exc:
                self._logger.warning("Error while closing feed: %s", exc)
        elif self._feed is not None:
            # GrowwFeed has no close(); consume() runs on a daemon thread and
            # terminates with the process.
            self._logger.debug("GrowwFeed exposes no close(); relying on daemon shutdown.")

        if self._feed_thread is not None and self._feed_thread.is_alive():
            self._feed_thread.join(timeout=5)

        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5)

    def add_instrument(self, inst: Instrument) -> bool:
        """Add an instrument to the watchlist. Returns True if added."""
        with self._lock:
            if inst.symbol in self._instruments:
                return False
            self._instruments[inst.symbol] = inst
            self._quotes[inst.symbol] = Quote(symbol=inst.symbol)
        self._logger.info("Added %s (%s) to watchlist.", inst.symbol, inst.kind)
        return True

    def remove_instrument(self, symbol: str) -> bool:
        """Remove an instrument from the watchlist. Returns True if removed."""
        with self._lock:
            if symbol not in self._instruments:
                return False
            del self._instruments[symbol]
            self._quotes.pop(symbol, None)
        self._logger.info("Removed %s from watchlist.", symbol)
        return True

    def snapshot(self) -> list[dict]:
        """Return a JSON-serialisable snapshot of all current quotes."""
        with self._lock:
            rows = [
                (q, self._instruments[q.symbol])
                for q in self._quotes.values()
                if q.symbol in self._instruments
            ]
        return [
            {
                "symbol": q.symbol,
                "kind": inst.kind,
                "ltp": q.ltp,
                "change_pct": q.change_pct,
                "volume": q.volume,
                "last_trade_time": q.last_trade_time,
                "updated_at": q.updated_at,
                "age_seconds": round(q.age_seconds, 1),
            }
            for q, inst in sorted(rows, key=lambda x: x[0].symbol)
        ]

    def refresh_once(self) -> None:
        """Perform a single quote refresh cycle (used by background mode)."""
        self._refresh_quotes()

    def start_background(self) -> threading.Thread:
        """Start quote polling (and feed, if enabled) in a daemon thread.

        Used by the web UI so the HTTP server can serve data concurrently.
        Returns the worker thread.
        """
        if self._settings.use_feed and self._groww is not None:
            self._start_feed()

        self._worker_thread = threading.Thread(
            target=self._background_loop, name="market-watch-worker", daemon=True
        )
        self._worker_thread.start()
        return self._worker_thread

    def _background_loop(self) -> None:
        """Polling loop without a terminal display (web UI mode)."""
        while not self._stop_event.is_set():
            started = time.monotonic()
            self.refresh_once()
            elapsed = time.monotonic() - started
            sleep_for = max(0.0, self._settings.poll_interval - elapsed)
            if sleep_for:
                self._stop_event.wait(sleep_for)

    # ------------------------------------------------------------------
    # Polling engine
    # ------------------------------------------------------------------
    def _polling_loop(self, live: Live) -> None:
        """Continuously refresh quotes and update the live display."""
        while not self._stop_event.is_set():
            started = time.monotonic()
            self._refresh_quotes()
            live.update(self._render())

            elapsed = time.monotonic() - started
            sleep_for = max(0.0, self._settings.poll_interval - elapsed)
            if sleep_for:
                self._stop_event.wait(sleep_for)

    def _refresh_quotes(self) -> None:
        """Fetch quotes for all instruments in parallel."""
        errors = 0
        with self._lock:
            instruments = list(self._instruments.values())
        if not instruments:
            return
        workers = min(8, max(2, len(instruments)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="quote") as pool:
            futures = {
                pool.submit(self._fetch_quote, inst): inst
                for inst in instruments
            }
            for future in as_completed(futures):
                inst = futures[future]
                try:
                    quote = future.result()
                    if quote is not None:
                        with self._lock:
                            self._quotes[inst.symbol] = quote
                except Exception as exc:
                    errors += 1
                    self._logger.warning("Quote fetch failed for %s: %s", inst.symbol, exc)

        self._consecutive_failures = errors if errors == len(self._instruments) else 0
        if self._consecutive_failures:
            delay = min(
                self._settings.reconnect_delay * (2 ** min(self._consecutive_failures, 5)),
                60.0,
            )
            self._logger.error(
                "All quote requests failed (%d in a row); backing off %.0fs.",
                self._consecutive_failures,
                delay,
            )
            self._stop_event.wait(delay)

    # Approximate reference prices used to seed the demo random walk.
    _DEMO_BASE_PRICES: Dict[str, float] = {
        "RELIANCE": 2950.0,
        "TCS": 4100.0,
        "INFY": 1850.0,
        "WIPRO": 250.0,
        "NIFTY": 24500.0,
        "BANKNIFTY": 52000.0,
    }

    # Yahoo Finance tickers for common NSE indices
    _YAHOO_INDEX_MAP: Dict[str, str] = {
        "NIFTY": "^NSEI",
        "BANKNIFTY": "^NSEBANK",
        "FINNIFTY": "^CNXFIN",
        "MIDCPNIFT": "^CNXMID",
        "NIFTYNXT50": "^NIFTYNXT50",
    }

    # Common ticker aliases -> Yahoo symbol (users often type short names)
    _YAHOO_ALIASES: Dict[str, str] = {
        "HDFC": "HDFCBANK.NS",
        "Reliance": "RELIANCE.NS",
    }

    def _yahoo_symbol(self, inst: Instrument) -> str:
        """Map a watchlist instrument to its Yahoo Finance ticker."""
        alias = self._YAHOO_ALIASES.get(inst.symbol.upper())
        if alias:
            return alias
        if inst.kind == "INDEX":
            return self._YAHOO_INDEX_MAP.get(inst.symbol, f"^{inst.symbol}")
        return f"{inst.symbol}.NS"

    def _fetch_quote(self, inst: Instrument) -> Optional[Quote]:
        """Fetch a single quote via the configured data source."""
        if self._settings.demo_mode:
            return self._demo_quote(inst)
        if self._settings.data_source == "yahoo":
            return self._fetch_yahoo_quote(inst)
        try:
            resp = self._groww.get_quote(
                exchange=inst.exchange,
                segment=inst.segment,
                trading_symbol=inst.symbol,
            )
        except GrowwAPIException as exc:
            self._logger.debug("Groww API error for %s: %s", inst.symbol, exc)
            raise
        except Exception as exc:
            self._logger.debug("Network/unknown error for %s: %s", inst.symbol, exc)
            raise

        if not isinstance(resp, dict):
            self._logger.warning("Unexpected quote payload for %s: %r", inst.symbol, resp)
            return None

        ltp = resp.get("last_price") or resp.get("ltp")
        change_pct = resp.get("day_change_perc")
        volume = resp.get("volume")
        ltt = resp.get("last_trade_time")

        try:
            ltp = float(ltp) if ltp is not None else None
        except (TypeError, ValueError):
            ltp = None
        try:
            change_pct = float(change_pct) if change_pct is not None else None
        except (TypeError, ValueError):
            change_pct = None
        try:
            volume = int(volume) if volume is not None else None
        except (TypeError, ValueError):
            volume = None
        try:
            ltt = int(ltt) if ltt is not None else None
        except (TypeError, ValueError):
            ltt = None

        return Quote(
            symbol=inst.symbol,
            ltp=ltp,
            change_pct=change_pct,
            volume=volume,
            last_trade_time=ltt,
        )

    def _fetch_yahoo_quote(self, inst: Instrument) -> Quote:
        """Fetch a quote from Yahoo Finance via yfinance (delayed data)."""
        import yfinance as yf

        ticker = yf.Ticker(self._yahoo_symbol(inst))

        ltp: Optional[float] = None
        volume: Optional[int] = None
        ltt: Optional[int] = None
        prev_close: Optional[float] = None

        try:
            hist = ticker.history(period="1d", interval="1m", auto_adjust=False)
            if hist is not None and not hist.empty:
                ltp = float(hist["Close"].iloc[-1])
                if "Volume" in hist:
                    vol = int(hist["Volume"].sum())
                    volume = vol or None
                ltt = int(hist.index[-1].timestamp() * 1000)
                opens = hist["Open"].dropna()
                if not opens.empty:
                    prev_close = float(opens.iloc[0])
        except Exception as exc:
            self._logger.debug("yfinance history failed for %s: %s", inst.symbol, exc)

        # fast_info is a lighter fallback / supplement for price + prev close
        try:
            fi = ticker.fast_info
            if ltp is None and getattr(fi, "last_price", None):
                ltp = float(fi.last_price)
            if prev_close is None and getattr(fi, "previous_close", None):
                prev_close = float(fi.previous_close)
        except Exception as exc:
            self._logger.debug("yfinance fast_info failed for %s: %s", inst.symbol, exc)

        change_pct: Optional[float] = None
        if ltp is not None and prev_close:
            change_pct = (ltp / prev_close - 1.0) * 100.0

        return Quote(
            symbol=inst.symbol,
            ltp=ltp,
            change_pct=change_pct,
            volume=volume,
            last_trade_time=ltt,
        )

    def _demo_quote(self, inst: Instrument) -> Quote:
        """Generate a synthetic quote via a small random walk (demo mode)."""
        base = self._DEMO_BASE_PRICES.get(inst.symbol)
        if base is None:
            base = 1000.0 + (hash(inst.symbol) % 4000)

        prev = self._quotes[inst.symbol]
        anchor = prev.ltp if prev.ltp is not None else base * (1 + random.uniform(-0.02, 0.02))
        # mean-reverting jitter around the base price
        drift = (base - anchor) * 0.02
        ltp = max(0.01, anchor + drift + anchor * random.uniform(-0.0015, 0.0015))
        change_pct = (ltp / base - 1.0) * 100.0
        volume = random.randint(50_000, 5_000_000)

        return Quote(
            symbol=inst.symbol,
            ltp=round(ltp, 2),
            change_pct=round(change_pct, 2),
            volume=volume,
            last_trade_time=int(time.time() * 1000),
        )

    # ------------------------------------------------------------------
    # Feed engine (WebSocket)
    # ------------------------------------------------------------------
    def _start_feed(self) -> None:
        """Resolve instrument tokens and start the Groww feed in a thread."""
        try:
            self._feed = GrowwFeed(self._groww)
        except Exception as exc:
            self._logger.error("Failed to initialise GrowwFeed: %s", exc)
            return

        ltp_instruments, index_instruments = self._resolve_feed_instruments()
        self._feed_subscriptions = (ltp_instruments, index_instruments)

        if ltp_instruments:
            try:
                self._feed.subscribe_ltp(ltp_instruments, on_data_received=self._on_feed_data)
            except Exception as exc:
                self._logger.error("LTP subscription failed: %s", exc)

        if index_instruments:
            try:
                self._feed.subscribe_index_value(
                    index_instruments, on_data_received=self._on_feed_data
                )
            except Exception as exc:
                self._logger.error("Index subscription failed: %s", exc)

        self._feed_thread = threading.Thread(
            target=self._feed_consume_loop, name="groww-feed", daemon=True
        )
        self._feed_thread.start()

    def _resolve_feed_instruments(self) -> tuple[list[dict], list[dict]]:
        """Map watchlist symbols to feed subscription payloads.

        Returns ``(ltp_instruments, index_instruments)``. Stock exchange tokens
        are resolved through ``get_instrument_by_groww_symbol``; index feeds
        use the index trading symbol itself as the exchange token.
        """
        ltp_instruments: list[dict] = []
        index_instruments: list[dict] = []

        for inst in self._instruments.values():
            base = {"exchange": inst.exchange, "segment": inst.segment}
            if inst.kind == "INDEX":
                index_instruments.append({**base, "exchange_token": inst.symbol})
                self._index_token_map[inst.symbol] = inst.symbol
                continue

            try:
                details = self._groww.get_instrument_by_groww_symbol(inst.groww_symbol)
                token = str(details.get("exchange_token", ""))
                if token:
                    ltp_instruments.append({**base, "exchange_token": token})
                    self._ltp_token_map[token] = inst.symbol
                else:
                    self._logger.warning(
                        "No exchange token for %s; feed LTP unavailable.", inst.symbol
                    )
            except Exception as exc:
                self._logger.warning(
                    "Could not resolve feed token for %s: %s", inst.symbol, exc
                )

        self._logger.info(
            "Feed subscriptions: %d equities, %d indices.",
            len(ltp_instruments),
            len(index_instruments),
        )
        return ltp_instruments, index_instruments

    def _feed_consume_loop(self) -> None:
        """Blocking feed consumer with reconnect/backoff handling."""
        attempt = 0
        while not self._stop_event.is_set():
            try:
                self._feed.consume()  # type: ignore[union-attr]
                break  # clean exit
            except GrowwFeedException as exc:
                attempt += 1
                self._logger.error(
                    "Feed connection error (attempt %d): %s", attempt, exc
                )
            except Exception as exc:
                attempt += 1
                self._logger.error("Unexpected feed error (attempt %d): %s", attempt, exc)

            if self._stop_event.is_set():
                break
            if attempt > self._settings.max_reconnect_attempts:
                self._logger.error("Max feed reconnect attempts reached; giving up.")
                break

            delay = min(
                self._settings.reconnect_delay * (2 ** min(attempt, 5)), 120.0
            )
            self._logger.info("Reconnecting feed in %.0fs...", delay)
            self._stop_event.wait(delay)
            # Re-subscribe on reconnect
            try:
                self._feed = GrowwFeed(self._groww)
                self._start_feed_subscriptions_only()
            except Exception as exc:
                self._logger.error("Feed re-initialisation failed: %s", exc)

    def _start_feed_subscriptions_only(self) -> None:
        """Re-apply subscriptions on an existing feed instance."""
        ltp_instruments, index_instruments = [], []
        for inst in self._instruments.values():
            base = {"exchange": inst.exchange, "segment": inst.segment}
            if inst.kind == "INDEX":
                index_instruments.append({**base, "exchange_token": inst.symbol})
            elif inst.symbol in self._ltp_token_map.values():
                token = next(
                    k for k, v in self._ltp_token_map.items() if v == inst.symbol
                )
                ltp_instruments.append({**base, "exchange_token": token})

        if ltp_instruments:
            self._feed.subscribe_ltp(ltp_instruments, on_data_received=self._on_feed_data)
        if index_instruments:
            self._feed.subscribe_index_value(
                index_instruments, on_data_received=self._on_feed_data
            )

    def _on_feed_data(self, meta: Any = None) -> None:
        """Callback invoked by GrowwFeed when new data arrives."""
        try:
            self._apply_ltp_data(self._feed.get_ltp())  # type: ignore[union-attr]
        except Exception as exc:
            self._logger.debug("get_ltp decode failed: %s", exc)
        try:
            self._apply_index_data(self._feed.get_index_value())  # type: ignore[union-attr]
        except Exception as exc:
            self._logger.debug("get_index_value decode failed: %s", exc)

    def _apply_ltp_data(self, payload: Dict[str, Any]) -> None:
        """Merge LTP payload into the quote store."""
        ltp_root = payload.get("ltp", payload)
        for exchange_map in ltp_root.values():
            if not isinstance(exchange_map, dict):
                continue
            for segment_map in exchange_map.values():
                if not isinstance(segment_map, dict):
                    continue
                for token, data in segment_map.items():
                    symbol = self._ltp_token_map.get(str(token))
                    if symbol and isinstance(data, dict):
                        self._update_quote(symbol, ltp=data.get("ltp"), ts=data.get("tsInMillis"))

    def _apply_index_data(self, payload: Dict[str, Any]) -> None:
        """Merge index-value payload into the quote store."""
        for exchange_map in payload.values():
            if not isinstance(exchange_map, dict):
                continue
            for segment_map in exchange_map.values():
                if not isinstance(segment_map, dict):
                    continue
                for token, data in segment_map.items():
                    symbol = self._index_token_map.get(str(token))
                    if symbol and isinstance(data, dict):
                        self._update_quote(symbol, ltp=data.get("value"), ts=data.get("tsInMillis"))

    def _update_quote(
        self, symbol: str, ltp: Any = None, ts: Any = None
    ) -> None:
        """Thread-safe partial update of a stored quote."""
        with self._lock:
            quote = self._quotes.get(symbol)
            if quote is None:
                return
            if ltp is not None:
                try:
                    quote.ltp = float(ltp)
                except (TypeError, ValueError):
                    pass
            quote.updated_at = time.time()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _render(self) -> Group:
        """Build the rich renderable (header + table + status footer)."""
        table = Table(
            title="Groww Real-Time Market Watch",
            title_style="bold cyan",
            border_style="bright_blue",
            header_style="bold magenta",
            show_lines=False,
        )
        table.add_column("Symbol", style="bold", min_width=10)
        table.add_column("Type", justify="center", min_width=5)
        table.add_column("LTP", justify="right", min_width=12)
        table.add_column("Chg %", justify="right", min_width=9)
        table.add_column("Volume", justify="right", min_width=14)
        table.add_column("Last Trade", justify="right", min_width=10)
        table.add_column("Age", justify="right", min_width=6)

        with self._lock:
            rows = [
                (quotes[symbol], self._instruments[symbol])
                for symbol in quotes
                if symbol in self._instruments
            ]

        for q, inst in rows:
            symbol = q.symbol
            table.add_row(
                symbol,
                inst.kind,
                self._fmt_price(q.ltp),
                self._fmt_change(q.change_pct),
                self._fmt_volume(q.volume),
                self._fmt_ltt(q.last_trade_time),
                self._fmt_age(q.age_seconds),
            )

        status = Text.assemble(
            (" feed=ON " if self._settings.use_feed else " feed=OFF ", "bold"),
            (" Ctrl+C to quit", "dim"),
        )
        return Group(table, Text(""), status)

    # -- formatting helpers -------------------------------------------------
    @staticmethod
    def _fmt_price(value: Optional[float]) -> str:
        return f"{value:,.2f}" if value is not None else "—"

    @staticmethod
    def _fmt_change(value: Optional[float]) -> Text:
        if value is None:
            return Text("—", style="dim")
        color = "green" if value >= 0 else "red"
        sign = "+" if value >= 0 else ""
        return Text(f"{sign}{value:.2f}%", style=color)

    @staticmethod
    def _fmt_volume(value: Optional[int]) -> str:
        if value is None:
            return "—"
        if value >= 1_00_00_000:  # 1 crore
            return f"{value / 1_00_00_000:.2f} Cr"
        if value >= 1_00_000:  # 1 lakh
            return f"{value / 1_00_000:.2f} L"
        if value >= 1_000:
            return f"{value / 1_000:.1f}K"
        return str(value)

    @staticmethod
    def _fmt_ltt(epoch_ms: Optional[int]) -> str:
        if epoch_ms is None:
            return "—"
        try:
            return datetime.fromtimestamp(epoch_ms / 1000).strftime("%H:%M:%S")
        except (OSError, ValueError, OverflowError):
            return "—"

    @staticmethod
    def _fmt_age(seconds: float) -> str:
        if seconds < 60:
            return f"{seconds:.0f}s"
        return f"{seconds / 60:.0f}m"
