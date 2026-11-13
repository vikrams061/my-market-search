"""Configuration management for the Real-Time Market Watch application.

Loads credentials and runtime settings from environment variables (via a
local ``.env`` file), resolves API access tokens, and defines the default
instrument watchlist.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from growwapi import GrowwAPI

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
LOG_FILE = BASE_DIR / "market_watch.log"

# ---------------------------------------------------------------------------
# Environment loading
# ---------------------------------------------------------------------------
load_dotenv(ENV_FILE)


def _get_env(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    """Read an environment variable, raising a clear error when required."""
    value = os.getenv(name, default)
    if required and not value:
        raise EnvironmentError(
            f"Missing required environment variable '{name}'. "
            f"Copy .env.example to .env and fill in your credentials."
        )
    return value


def _get_bool_env(name: str, default: bool = False) -> bool:
    """Parse a boolean-ish environment variable."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure the root logger to write to console and ``market_watch.log``."""
    logger = logging.getLogger("market_watch")
    logger.setLevel(level)
    logger.propagate = False

    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.WARNING)
        console_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

    return logger


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    """Runtime configuration resolved from environment variables."""

    api_key: str
    api_secret: str
    totp_secret: Optional[str] = None
    use_feed: bool = False
    poll_interval: float = 2.0
    request_timeout: float = 10.0
    reconnect_delay: float = 5.0
    max_reconnect_attempts: int = 10
    log_level: int = logging.INFO
    demo_mode: bool = False
    data_source: str = "groww"  # "groww" or "yahoo"
    extra_symbols: tuple[str, ...] = field(default_factory=tuple)
    extra_indices: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables."""
        log_level_name = (_get_env("LOG_LEVEL", "INFO") or "INFO").upper()
        totp_secret = _get_env("GROWW_TOTP_SECRET")
        return cls(
            api_key=_get_env("GROWW_API_KEY", required=True) or "",
            api_secret=_get_env("GROWW_API_SECRET", required=not totp_secret and not _get_bool_env("DEMO_MODE")) or "",
            totp_secret=totp_secret,
            use_feed=_get_bool_env("USE_FEED", default=False),
            poll_interval=float(_get_env("POLL_INTERVAL_SECONDS", "2.0") or 2.0),
            request_timeout=float(_get_env("REQUEST_TIMEOUT_SECONDS", "10") or 10.0),
            reconnect_delay=float(_get_env("RECONNECT_DELAY_SECONDS", "5") or 5.0),
            max_reconnect_attempts=int(_get_env("MAX_RECONNECT_ATTEMPTS", "10") or 10),
            log_level=getattr(logging, log_level_name, logging.INFO),
            demo_mode=_get_bool_env("DEMO_MODE", default=False),
            data_source=(_get_env("DATA_SOURCE", "groww") or "groww").strip().lower(),
            extra_symbols=tuple(
                s.strip().upper()
                for s in (_get_env("EXTRA_SYMBOLS", "") or "").split(",")
                if s.strip()
            ),
            extra_indices=tuple(
                s.strip().upper()
                for s in (_get_env("EXTRA_INDICES", "") or "").split(",")
                if s.strip()
            ),
        )


# ---------------------------------------------------------------------------
# Watchlist definition
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Instrument:
    """A single tradable instrument or index on the watchlist."""

    symbol: str
    kind: str = "EQ"  # "EQ" for equities, "INDEX" for indices
    exchange: str = "NSE"
    segment: str = "CASH"

    @property
    def groww_symbol(self) -> str:
        """Groww-style symbol used by ``get_instrument_by_groww_symbol``."""
        return f"{self.exchange}-{self.symbol}"


# Default watchlist: NSE equities + indices. Extend via EXTRA_SYMBOLS /
# EXTRA_INDICES environment variables (comma-separated trading symbols).
DEFAULT_EQUITIES: tuple[str, ...] = ("RELIANCE", "TCS", "INFY", "WIPRO")
DEFAULT_INDICES: tuple[str, ...] = ("NIFTY", "BANKNIFTY")


def build_watchlist(settings: Settings) -> list[Instrument]:
    """Combine default and user-configured instruments into a watchlist."""
    equities = list(DEFAULT_EQUITIES) + list(settings.extra_symbols)
    indices = list(DEFAULT_INDICES) + list(settings.extra_indices)

    instruments = [
        Instrument(symbol=sym, kind="EQ") for sym in dict.fromkeys(equities)
    ]
    instruments += [
        Instrument(symbol=idx, kind="INDEX") for idx in dict.fromkeys(indices)
    ]
    return instruments


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def get_access_token(settings: Settings, logger: logging.Logger) -> str:
    """Exchange API key/secret (and optional TOTP) for an access token.

    Retries a few times with backoff since the token endpoint is rate-limited
    (150 requests / 24h) and can transiently fail.
    """
    import time

    last_error: Optional[Exception] = None
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            # TOTP keys authenticate with a generated code only; approval keys
            # use the shared secret. Passing both can confuse the token API.
            kwargs: dict = {"api_key": settings.api_key}
            if settings.totp_secret:
                try:
                    import pyotp

                    kwargs["totp"] = pyotp.TOTP(settings.totp_secret).now()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("TOTP generation failed: %s", exc)
            elif settings.api_secret:
                kwargs["secret"] = settings.api_secret

            token = GrowwAPI.get_access_token(**kwargs)
            logger.info("Access token acquired (attempt %d/%d).", attempt, attempts)
            return token
        except Exception as exc:  # growwapi raises SDK-specific exceptions
            last_error = exc
            logger.warning(
                "Access token request failed (attempt %d/%d): %s", attempt, attempts, exc
            )
            if attempt < attempts:
                time.sleep(2 ** attempt)

    raise RuntimeError(
        f"Could not obtain Groww access token: {last_error} "
        f"{_diagnose_token_error(settings)}"
    )


def _diagnose_token_error(settings: Settings) -> str:
    """Re-issue the token request raw to surface Groww's real errorMessage.

    The SDK collapses non-200 responses into a generic 'Bad Request', hiding
    useful details like 'Token key not found or inactive'.
    """
    try:
        import time

        import requests

        kwargs: dict = {}
        if settings.totp_secret:
            import pyotp

            kwargs = {"totp": pyotp.TOTP(settings.totp_secret).now()}
        elif settings.api_secret:
            ts = str(int(time.time()))
            kwargs = {
                "secret": settings.api_secret,
                "_timestamp": ts,
                "_checksum": GrowwAPI._generate_checksum(settings.api_secret, ts),
            }

        if "totp" in kwargs:
            body = {"key_type": "totp", "totp": kwargs["totp"]}
        else:
            body = {
                "key_type": "approval",
                "checksum": kwargs["_checksum"],
                "timestamp": kwargs["_timestamp"],
            }

        resp = requests.post(
            "https://api.groww.in/v1/token/api/access",
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=15,
        )
        try:
            msg = resp.json().get("error", {}).get("errorMessage")
        except Exception:
            msg = None
        if msg:
            return f"(Groww says: {msg})"
    except Exception:
        pass
    return ""


def create_client(settings: Settings, logger: logging.Logger) -> GrowwAPI:
    """Create an authenticated ``GrowwAPI`` client."""
    token = get_access_token(settings, logger)
    return GrowwAPI(token)
