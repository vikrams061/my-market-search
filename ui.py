"""Web UI for the Groww Real-Time Market Watch.

Serves a single-page dashboard (embedded HTML/JS) plus a JSON quotes API.
The page auto-refreshes every couple of seconds and renders LTP, day change %,
volume, and last trade time in a clean table.

Run via ``python main.py --web`` or directly ``python ui.py``.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
import uvicorn

from config import Instrument, Settings, create_client, setup_logging
from market_watch import MarketWatch


class InstrumentIn(BaseModel):
    """Request body for adding an instrument to the watchlist."""

    symbol: str = Field(min_length=1, max_length=30)
    kind: str = Field(pattern="^(EQ|INDEX)$")

# ---------------------------------------------------------------------------
# Embedded single-page frontend
# ---------------------------------------------------------------------------
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Groww Market Watch</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 2rem;
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
    background: #0f172a; color: #e2e8f0;
  }
  h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
  .sub { color: #94a3b8; font-size: .85rem; margin-bottom: 1.5rem; }
  .controls { display: flex; gap: .5rem; margin-bottom: 1.25rem; }
  .controls input, .controls select {
    background: #1e293b; color: #e2e8f0; border: 1px solid #334155;
    border-radius: 8px; padding: .55rem .8rem; font-size: .9rem;
  }
  .controls input { width: 220px; text-transform: uppercase; }
  .controls button {
    background: #6366f1; color: #fff; border: none; border-radius: 8px;
    padding: .55rem 1.1rem; font-size: .9rem; cursor: pointer;
  }
  .controls button:hover { background: #4f46e5; }
  .empty { color: #64748b; padding: 2rem; text-align: center;
           background: #1e293b; border-radius: 10px; }
  .del {
    background: none; border: none; color: #64748b; cursor: pointer;
    font-size: 1rem; padding: 0 .25rem;
  }
  .del:hover { color: #f87171; }
  table { width: 100%; border-collapse: collapse; background: #1e293b;
          border-radius: 10px; overflow: hidden; }
  th, td { padding: .7rem 1rem; text-align: left; }
  th { background: #334155; font-size: .75rem; text-transform: uppercase;
       letter-spacing: .05em; color: #cbd5e1; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  tr + tr td { border-top: 1px solid #334155; }
  .sym { font-weight: 600; }
  .badge { font-size: .7rem; padding: .15rem .5rem; border-radius: 999px;
           background: #334155; color: #cbd5e1; }
  .up { color: #4ade80; } .down { color: #f87171; } .flat { color: #94a3b8; }
  .pulse { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
           background: #4ade80; margin-right: .4rem; animation: p 1.5s infinite; }
  @keyframes p { 50% { opacity: .3; } }
  #status { margin-top: 1rem; font-size: .8rem; color: #64748b; }
</style>
</head>
<body>
  <h1><span class="pulse"></span>Groww Market Watch</h1>
  <div class="sub" id="meta">Loading…</div>
  <div class="controls">
    <input id="sym" placeholder="Symbol (e.g. RELIANCE, NIFTY)" maxlength="30" />
    <select id="kind">
      <option value="EQ">Equity</option>
      <option value="INDEX">Index</option>
    </select>
    <button onclick="addInstrument()">+ Add</button>
  </div>
  <table>
    <thead><tr>
      <th>Symbol</th><th>Type</th><th class="num">LTP</th>
      <th class="num">Chg %</th><th class="num">Volume</th>
      <th class="num">Last Trade</th><th class="num">Age</th><th></th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div id="empty" class="empty" style="display:none">
    No instruments yet — add a symbol above to start watching.
  </div>
  <div id="status"></div>
<script>
const REFRESH_MS = 2000;
const fmtPrice = v => v == null ? '—' : Number(v).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2});
const fmtVol = v => {
  if (v == null) return '—';
  if (v >= 1e7) return (v/1e7).toFixed(2) + ' Cr';
  if (v >= 1e5) return (v/1e5).toFixed(2) + ' L';
  if (v >= 1e3) return (v/1e3).toFixed(1) + 'K';
  return v;
};
const fmtLtt = ms => ms == null ? '—' : new Date(ms).toLocaleTimeString('en-IN');
const chgClass = v => v == null ? 'flat' : (v > 0 ? 'up' : v < 0 ? 'down' : 'flat');
const chgText = v => v == null ? '—' : (v > 0 ? '+' : '') + Number(v).toFixed(2) + '%';

async function addInstrument() {
  const sym = document.getElementById('sym').value.trim().toUpperCase();
  const kind = document.getElementById('kind').value;
  if (!sym) return;
  const res = await fetch('/api/instruments', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({symbol: sym, kind: kind})
  });
  if (res.ok) {
    document.getElementById('sym').value = '';
    tick();
  } else {
    const err = await res.json();
    alert(err.detail || 'Failed to add instrument');
  }
}

async function removeInstrument(sym) {
  await fetch('/api/instruments/' + encodeURIComponent(sym), {method: 'DELETE'});
  tick();
}

async function tick() {
  try {
    const res = await fetch('/api/quotes');
    const data = await res.json();
    const rows = document.getElementById('rows');
    rows.innerHTML = '';
    document.getElementById('empty').style.display = data.count ? 'none' : 'block';
    for (const q of data.quotes) {
      const tr = document.createElement('tr');
      tr.innerHTML =
        `<td class="sym">${q.symbol}</td>` +
        `<td><span class="badge">${q.kind}</span></td>` +
        `<td class="num">${fmtPrice(q.ltp)}</td>` +
        `<td class="num ${chgClass(q.change_pct)}">${chgText(q.change_pct)}</td>` +
        `<td class="num">${fmtVol(q.volume)}</td>` +
        `<td class="num">${fmtLtt(q.last_trade_time)}</td>` +
        `<td class="num">${q.age_seconds}s</td>` +
        `<td><button class="del" title="Remove" onclick="removeInstrument('${q.symbol}')">✕</button></td>`;
      rows.appendChild(tr);
    }
    document.getElementById('meta').textContent =
      `${data.count} instruments · updated ${new Date().toLocaleTimeString('en-IN')}` +
      (data.feed ? ' · live feed ON' : ' · polling');
    document.getElementById('status').textContent = '';
  } catch (e) {
    document.getElementById('status').textContent = 'Connection error — retrying…';
  }
}
tick();
setInterval(tick, REFRESH_MS);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
def create_app(watch: MarketWatch, settings: Settings) -> FastAPI:
    """Build the FastAPI application bound to a running MarketWatch."""
    app = FastAPI(title="Groww Market Watch", version="1.0.0")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return INDEX_HTML

    @app.get("/api/quotes")
    async def quotes() -> JSONResponse:
        return JSONResponse(
            {
                "quotes": watch.snapshot(),
                "count": len(watch.snapshot()),
                "feed": settings.use_feed,
            }
        )

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "instruments": len(watch.snapshot())}

    @app.post("/api/instruments", status_code=201)
    async def add_instrument(payload: InstrumentIn) -> dict:
        symbol = payload.symbol.strip().upper()
        if not symbol:
            raise HTTPException(status_code=400, detail="Symbol cannot be empty")
        inst = Instrument(symbol=symbol, kind=payload.kind)
        if not watch.add_instrument(inst):
            raise HTTPException(status_code=409, detail=f"{symbol} already tracked")
        return {"added": symbol, "kind": payload.kind}

    @app.delete("/api/instruments/{symbol}")
    async def remove_instrument(symbol: str) -> dict:
        symbol = symbol.strip().upper()
        if not watch.remove_instrument(symbol):
            raise HTTPException(status_code=404, detail=f"{symbol} not tracked")
        return {"removed": symbol}

    return app


def run_web(
    settings: Optional[Settings] = None,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> int:
    """Start the web UI server. Blocks until Ctrl+C."""
    logger = setup_logging()
    settings = settings or Settings.from_env()
    logger.setLevel(settings.log_level)

    if settings.demo_mode:
        logger.warning("DEMO MODE — web UI will show simulated market data.")
        groww = None
    elif settings.data_source == "yahoo":
        logger.info("Data source: Yahoo Finance (delayed data, no Groww auth needed).")
        groww = None
    else:
        groww = create_client(settings, logger)

    # Web UI starts with an empty watchlist — instruments are added via the page.
    instruments = []
    logger.info("Web UI watchlist starts empty; add symbols from the page.")
    watch = MarketWatch(
        groww=groww, instruments=instruments, settings=settings, logger=logger
    )

    if host not in {"127.0.0.1", "localhost", "::1"}:
        logger.warning(
            "Web UI is binding to %s — it has NO authentication. "
            "Anyone who can reach this port can view your watchlist data. "
            "Use a firewall/VPN or keep the default 127.0.0.1.",
            host,
        )

    watch.start_background()
    logger.info(
        "Web UI starting at http://%s:%d (%d instruments, feed=%s).",
        host,
        port,
        len(instruments),
        settings.use_feed,
    )

    app = create_app(watch, settings)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user.")
    finally:
        watch.stop()
    return 0


def main() -> int:
    import os

    port = int(os.getenv("WEB_PORT", "8000"))
    host = os.getenv("WEB_HOST", "127.0.0.1")
    return run_web(host=host, port=port)


if __name__ == "__main__":
    raise SystemExit(main())
