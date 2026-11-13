# my-market-search

Real-time market watch for Indian equities and indices using the official
[Groww Trading API](https://groww.in/trade-api) Python SDK (`growwapi`).

Displays a live-updating terminal table (via `rich`) with LTP, day change %,
volume, and last trade time for a configurable watchlist.

## Features

- **Authentication** — API key/secret flow via `GrowwAPI.get_access_token`,
  optional TOTP support, credentials loaded from `.env` (`python-dotenv`).
- **Multi-asset tracking** — NSE equities and indices; extend the watchlist
  with `EXTRA_SYMBOLS` / `EXTRA_INDICES`.
- **Two data modes** —
  - *Polling* (default): parallel `get_quote` calls, rate-limit aware.
  - *Feed* (`USE_FEED=true`): WebSocket streaming via `GrowwFeed` with
    automatic reconnect/backoff.
- **Resilience** — retry/backoff on auth, exponential backoff on total feed
    or quote failure, graceful `Ctrl+C` shutdown, structured logging to
    `market_watch.log`.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -r requirements.txt
copy .env.example .env        # then edit .env with your Groww credentials
```

Get API credentials from the Groww API keys page
(<https://groww.in/trade-api>).

## Run

Terminal UI (rich live table):

```bash
python main.py
```

Web dashboard (browser UI at http://127.0.0.1:8000):

```bash
python main.py --web
# or: python ui.py
# custom bind: python main.py --web --host 0.0.0.0 --port 8080
```

The web UI serves a single-page dashboard that auto-refreshes every 2s from
`GET /api/quotes` (plus `/api/health`). Press `Ctrl+C` to stop either mode.
Logs are written to `market_watch.log`.

## Configuration (`.env`)

| Variable | Description |
| --- | --- |
| `GROWW_API_KEY` / `GROWW_API_SECRET` | Required API credentials |
| `GROWW_TOTP_SECRET` | Optional TOTP secret for TOTP-based login |
| `EXTRA_SYMBOLS` | Comma-separated extra equity symbols (e.g. `SBIN,BAJFINANCE`) |
| `EXTRA_INDICES` | Comma-separated extra index symbols |
| `USE_FEED` | `true` for WebSocket streaming, `false` (default) for polling |
| `POLL_INTERVAL_SECONDS` | Quote refresh interval in polling mode (default `2.0`) |
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Project structure

```
├── config.py          # env loading, auth, watchlist, logging
├── market_watch.py    # quote store, polling/feed engines, rich UI
├── ui.py              # FastAPI web dashboard + /api/quotes
├── main.py            # entry point (--web flag for browser UI)
├── requirements.txt
└── .env.example
```

## Notes

- Groww live-data endpoints allow ~300 requests/minute; the default 2s poll
  interval across 6 instruments stays well under this. Widen the watchlist
  or lower `POLL_INTERVAL_SECONDS` carefully.
- Feed mode requires resolving exchange tokens via the Groww instruments
  API; if token resolution fails for a symbol it is skipped gracefully and
  polling still supplies LTP/quote data.
