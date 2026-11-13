"""Web UI for the Real-Time Market Watch.

Serves a single-page dashboard (embedded HTML/JS) plus a JSON quotes API.
The page auto-refreshes every couple of seconds and renders LTP, day change %,
volume, and last trade time in a clean table.

Run via ``python main.py --web`` or directly ``python ui.py``.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Optional

import markdown

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
  .analyst { margin-top: 2rem; }
  .analyst h2 { font-size: 1.1rem; margin-bottom: .75rem; }
  #analyze-btn {
    background: #0ea5e9; color: #fff; border: none; border-radius: 8px;
    padding: .6rem 1.2rem; font-size: .9rem; cursor: pointer;
  }
  #analyze-btn:hover:not(:disabled) { background: #0284c7; }
  #analyze-btn:disabled { opacity: .5; cursor: wait; }
  #analysis-status { margin: .75rem 0; font-size: .85rem; color: #94a3b8; }
  #report {
    background: #1e293b; border-radius: 10px; padding: 1.5rem 2rem;
    line-height: 1.6; overflow-x: auto;
  }
  #report h1, #report h2, #report h3 { color: #7dd3fc; margin-top: 1.2em; }
  #report table { border-collapse: collapse; margin: .75rem 0; font-size: .85rem; }
  #report th, #report td { border: 1px solid #334155; padding: .4rem .8rem; }
  #report th { background: #334155; }
  #report code { background: #0f172a; padding: .1rem .35rem; border-radius: 4px; }
  #report pre { background: #0f172a; padding: 1rem; border-radius: 8px; overflow-x: auto; }
  #report pre code { background: none; padding: 0; }
  #report ul, #report ol { padding-left: 1.4rem; }
  #report blockquote { border-left: 3px solid #334155; margin: .75rem 0;
                       padding-left: 1rem; color: #94a3b8; }
  .picks { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 1rem; }
  .pick-card { background: #1e293b; border-radius: 10px; padding: 1.1rem 1.3rem; }
  .pick-card.bull { border-left: 4px solid #4ade80; }
  .pick-card.bear { border-left: 4px solid #f87171; }
  .pick-card h3 { margin: 0 0 .2rem; font-size: 1.05rem; }
  .pick-card .score { font-size: .8rem; color: #94a3b8; }
  .pick-card .ltp { font-size: 1.5rem; font-weight: 700; margin: .4rem 0 .1rem; }
  .pick-card .meta { font-size: .8rem; color: #94a3b8; line-height: 1.7; }
  .pick-card .move { font-size: .85rem; font-weight: 600; }
  .up { color: #4ade80; } .down { color: #f87171; } .flat { color: #94a3b8; }
  .lists { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 1rem; }
  .mini-list { background: #1e293b; border-radius: 10px; padding: .8rem 1.1rem; font-size: .85rem; }
  .mini-list h4 { margin: .2rem 0 .5rem; font-size: .85rem; color: #cbd5e1; }
  .mini-list div { display: flex; justify-content: space-between; padding: .15rem 0; }
  .scan-btn { background: #0ea5e9; color:#fff; border:none; border-radius:8px;
              padding:.5rem 1rem; font-size:.85rem; cursor:pointer; margin-left:.75rem; }
  .scan-btn:disabled { opacity:.5; cursor:wait; }
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

  <div class="analyst">
    <h2>Equity Analyst Agent</h2>
    <button id="analyze-btn" onclick="runAnalysis()">Run Momentum Analysis</button>
    <div id="analysis-status"></div>
    <div id="report"></div>
  </div>

  <div class="analyst">
    <h2>Today's NIFTY 50 Picks
      <button class="scan-btn" id="scan-btn" onclick="startScan()">Rescan</button>
    </h2>
    <div id="scan-status" style="font-size:.85rem;color:#94a3b8;margin-bottom:.5rem"></div>
    <div class="picks" id="pick-cards"></div>
    <div class="lists">
      <div class="mini-list"><h4>Top 5 Bullish</h4><div id="top5"></div></div>
      <div class="mini-list"><h4>Top 5 Bearish</h4><div id="bottom5"></div></div>
    </div>
  </div>

  <div class="analyst">
    <h2>Today's Active Stock Picks
      <button class="scan-btn" id="active-scan-btn" onclick="startActiveScan()">Rescan</button>
    </h2>
    <div id="active-scan-status" style="font-size:.85rem;color:#94a3b8;margin-bottom:.5rem"></div>
    <div class="picks" id="active-cards"></div>
  </div>
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
// ---- Analyst agent -------------------------------------------------------
let analysisTimer = null;

async function runAnalysis() {
  const btn = document.getElementById('analyze-btn');
  btn.disabled = true;
  document.getElementById('analysis-status').textContent = 'Collecting market data & generating report…';
  document.getElementById('report').innerHTML = '';
  try {
    const res = await fetch('/api/analyze', {method: 'POST'});
    const data = await res.json();
    if (!res.ok) {
      document.getElementById('analysis-status').textContent = data.detail || 'Analysis failed';
      btn.disabled = false;
      return;
    }
    if (analysisTimer) clearInterval(analysisTimer);
    analysisTimer = setInterval(pollAnalysis, 3000);
    pollAnalysis();
  } catch (e) {
    document.getElementById('analysis-status').textContent = 'Request failed — is the server running?';
    btn.disabled = false;
  }
}

async function pollAnalysis() {
  try {
    const res = await fetch('/api/analysis');
    const data = await res.json();
    const statusEl = document.getElementById('analysis-status');
    const btn = document.getElementById('analyze-btn');
    if (data.status === 'done') {
      document.getElementById('report').innerHTML = data.html;
      statusEl.textContent = 'Report ready.';
      btn.disabled = false;
      clearInterval(analysisTimer); analysisTimer = null;
    } else if (data.status === 'error') {
      statusEl.textContent = 'Error: ' + (data.error || 'unknown');
      btn.disabled = false;
      clearInterval(analysisTimer); analysisTimer = null;
    } else {
      statusEl.textContent = 'Generating report… (this can take 30–90s)';
    }
  } catch (e) { /* keep polling */ }
}

// ---- NIFTY 50 scanner ----------------------------------------------------
const fmtInr = v => v == null ? '—' : '₹' + Number(v).toLocaleString('en-IN', {maximumFractionDigits: 2});
const cls = v => v == null ? 'flat' : (v > 0 ? 'up' : v < 0 ? 'down' : 'flat');
const sgn = v => v == null ? '' : (v > 0 ? '+' : '');

function pickCard(title, clsName, p, note) {
  if (!p) return `<div class="pick-card ${clsName}"><h3>${title}</h3><div class="meta">No data</div></div>`;
  const t = p.technicals || {};
  const mv = p.movement || 0;
  return `<div class="pick-card ${clsName}">
    <h3>${p.symbol}</h3>
    <div class="score">Momentum ${p.momentum_score}/100 · RSI ${t.rsi_14 != null ? t.rsi_14.toFixed(0) : '—'}</div>
    <div class="ltp">${fmtInr(p.quote && p.quote.ltp)}</div>
    <div class="move ${cls(p.quote && p.quote.change_pct)}">${sgn(p.quote && p.quote.change_pct)}${p.quote && p.quote.change_pct != null ? p.quote.change_pct.toFixed(2) : '—'}% today</div>
    <div class="meta">Vol ${t.volume_vs_20d_avg != null ? t.volume_vs_20d_avg + 'x' : '—'} 20d avg ·
      EMA20 ${fmtInr(t.ema_20)} · EMA50 ${fmtInr(t.ema_50)}<br>
      <span class="move ${cls(mv)}">Δ ${sgn(mv)}${mv.toFixed(2)} since last poll</span>${note ? '<br>' + note : ''}
      ${p.targets ? `<br>T1 ${fmtInr(p.targets.t1)} (${p.targets.t1_tf}) · T2 ${fmtInr(p.targets.t2)} (${p.targets.t2_tf}) · SL ${fmtInr(p.targets.sl)}` : ''}</div>
  </div>`;
}

async function refreshPicks() {
  try {
    const res = await fetch('/api/picks');
    const d = await res.json();
    const cards = document.getElementById('pick-cards');
    if (d.status !== 'ready' || !d.picks) {
      cards.innerHTML = '<div class="meta" style="color:#64748b">No pick for today yet — click Rescan.</div>';
      return;
    }
    const p = d.picks;
    cards.innerHTML =
      pickCard('Bullish Pick', 'bull', p.bullish) +
      pickCard('Bearish Pick', 'bear', p.bearish, p.bearish_note);
    const fill = (id, arr) => document.getElementById(id).innerHTML =
      (arr || []).map(e => `<div><span>${e.symbol}</span><span>${e.score} · ${fmtInr(e.cmp)}</span></div>`).join('');
    fill('top5', p.top5); fill('bottom5', p.bottom5);
  } catch (e) { /* keep polling */ }
}

async function scanStatus() {
  try {
    const res = await fetch('/api/scan/status');
    const d = await res.json();
    document.getElementById('scan-status').textContent =
      d.status === 'running' ? `Scanning NIFTY 50… ${d.progress || ''}` :
      d.status === 'done' ? `Last scan: ${d.progress || ''} symbols` : '';
    document.getElementById('scan-btn').disabled = d.status === 'running';
  } catch (e) {}
}

async function startScan() {
  document.getElementById('scan-btn').disabled = true;
  await fetch('/api/scan', {method: 'POST'});
  setInterval(scanStatus, 2000);
  scanStatus();
}

// ---- Active stocks scanner ----------------------------------------------
function renderActivePicks(d) {
  const cards = document.getElementById('active-cards');
  if (d.status !== 'ready' || !d.picks) {
    cards.innerHTML = '<div class="meta" style="color:#64748b">No active-stock pick for today yet — click Rescan.</div>';
    return;
  }
  const p = d.picks;
  const card = (title, clsName, pk, note) => {
    if (!pk) return `<div class="pick-card ${clsName}"><h3>${title}</h3><div class="meta">No data</div></div>`;
    const t = pk.technicals || {};
    const mv = pk.movement || 0;
    const tg = pk.targets;
    return `<div class="pick-card ${clsName}">
      <h3>${pk.symbol}</h3>
      <div class="score">Momentum ${pk.momentum_score}/100 · RSI ${t.rsi_14 != null ? t.rsi_14.toFixed(0) : '—'}</div>
      <div class="ltp">${fmtInr(pk.quote && pk.quote.ltp)}</div>
      <div class="move ${cls(pk.quote && pk.quote.change_pct)}">${sgn(pk.quote && pk.quote.change_pct)}${pk.quote && pk.quote.change_pct != null ? pk.quote.change_pct.toFixed(2) : '—'}% today</div>
      <div class="meta">Vol ${t.volume_vs_20d_avg != null ? t.volume_vs_20d_avg + 'x' : '—'} 20d avg ·
        <span class="move ${cls(mv)}">Δ ${sgn(mv)}${mv.toFixed(2)}</span><br>
        ${tg ? `T1 ${fmtInr(tg.t1)} (${tg.t1_tf}) · T2 ${fmtInr(tg.t2)} (${tg.t2_tf}) · SL ${fmtInr(tg.sl)}` : ''}${note ? '<br>' + note : ''}</div>
    </div>`;
  };
  cards.innerHTML = card('Active Bullish Pick', 'bull', p.bullish) + card('Active Bearish Pick', 'bear', p.bearish, p.bearish_note);
}

async function refreshActivePicks() {
  try {
    const res = await fetch('/api/active/picks');
    renderActivePicks(await res.json());
  } catch (e) {}
}

async function activeScanStatus() {
  try {
    const res = await fetch('/api/active/scan/status');
    const d = await res.json();
    document.getElementById('active-scan-status').textContent =
      d.status === 'running' ? `Scanning active stocks… ${d.progress || ''}` :
      d.status === 'done' ? `Last scan: ${d.progress || ''} symbols` : '';
    document.getElementById('active-scan-btn').disabled = d.status === 'running';
  } catch (e) {}
}

async function startActiveScan() {
  document.getElementById('active-scan-btn').disabled = true;
  await fetch('/api/active/scan', {method: 'POST'});
  setInterval(activeScanStatus, 2000);
  activeScanStatus();
}

tick();
setInterval(tick, REFRESH_MS);
pollAnalysis();  // show last generated report (if any) on page load
refreshPicks();
setInterval(refreshPicks, 15000);
scanStatus();
refreshActivePicks();
setInterval(refreshActivePicks, 15000);
activeScanStatus();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------
WATCHLIST_FILE = "watchlist.json"


def _load_watchlist() -> list[Instrument]:
    """Load persisted watchlist instruments, if any."""
    try:
        data = json.loads(open(WATCHLIST_FILE, encoding="utf-8").read())
        return [
            Instrument(symbol=str(d["symbol"]).upper(), kind=d.get("kind", "EQ"))
            for d in data
            if d.get("symbol")
        ]
    except Exception:
        return []


def _save_watchlist(watch: MarketWatch) -> None:
    """Persist current watchlist symbols to disk."""
    try:
        with watch._lock:
            items = [
                {"symbol": inst.symbol, "kind": inst.kind}
                for inst in watch._instruments.values()
            ]
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f)
    except Exception:
        pass


def create_app(watch: MarketWatch, settings: Settings, scanner=None, active_scanner=None) -> FastAPI:
    """Build the FastAPI application bound to a running MarketWatch."""
    app = FastAPI(title="Groww Market Watch", version="1.0.0")

    # Analyst job state: {"status": idle|running|done|error, "html": str, "error": str}
    analysis_state: dict = {"status": "idle", "html": "", "error": ""}
    analysis_lock = threading.Lock()

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
        _save_watchlist(watch)
        return {"added": symbol, "kind": payload.kind}

    @app.delete("/api/instruments/{symbol}")
    async def remove_instrument(symbol: str) -> dict:
        symbol = symbol.strip().upper()
        if not watch.remove_instrument(symbol):
            raise HTTPException(status_code=404, detail=f"{symbol} not tracked")
        _save_watchlist(watch)
        return {"removed": symbol}

    # ------------------------------------------------------------------
    # Analyst agent endpoints
    # ------------------------------------------------------------------
    @app.post("/api/analyze", status_code=202)
    async def start_analysis() -> dict:
        with analysis_lock:
            if analysis_state["status"] == "running":
                raise HTTPException(status_code=409, detail="Analysis already running")
            analysis_state.update({"status": "running", "html": "", "error": ""})

        def _worker() -> None:
            from agent import run_analysis

            logger = logging.getLogger("market_watch")
            try:
                report_md = run_analysis(watch, settings, logger)
                html = markdown.markdown(
                    report_md, extensions=["tables", "fenced_code", "nl2br"]
                )
                with analysis_lock:
                    analysis_state.update({"status": "done", "html": html})
            except Exception as exc:
                logger.exception("Analyst agent failed: %s", exc)
                with analysis_lock:
                    analysis_state.update({"status": "error", "error": str(exc)})

        threading.Thread(target=_worker, name="analyst-agent", daemon=True).start()
        return {"status": "running"}

    @app.get("/api/analysis")
    async def get_analysis() -> dict:
        with analysis_lock:
            return dict(analysis_state)

    # ------------------------------------------------------------------
    # NIFTY 50 scanner endpoints
    # ------------------------------------------------------------------
    @app.post("/api/scan", status_code=202)
    async def start_scan() -> dict:
        if scanner is None:
            raise HTTPException(status_code=503, detail="Scanner not available")
        if not scanner.start_scan():
            raise HTTPException(status_code=409, detail="Scan already running")
        return {"status": "running"}

    @app.get("/api/scan/status")
    async def scan_status() -> dict:
        return scanner.status() if scanner else {"status": "unavailable"}

    @app.get("/api/picks")
    async def get_picks() -> dict:
        return scanner.today_picks() if scanner else {"status": "unavailable"}

    # ------------------------------------------------------------------
    # Active stocks scanner endpoints
    # ------------------------------------------------------------------
    @app.post("/api/active/scan", status_code=202)
    async def start_active_scan() -> dict:
        if active_scanner is None:
            raise HTTPException(status_code=503, detail="Scanner not available")
        if not active_scanner.start_scan():
            raise HTTPException(status_code=409, detail="Scan already running")
        return {"status": "running"}

    @app.get("/api/active/scan/status")
    async def active_scan_status() -> dict:
        return active_scanner.status() if active_scanner else {"status": "unavailable"}

    @app.get("/api/active/picks")
    async def get_active_picks() -> dict:
        return active_scanner.today_picks() if active_scanner else {"status": "unavailable"}

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

    # Web UI watchlist persists across restarts via watchlist.json
    instruments = _load_watchlist()
    logger.info("Web UI watchlist: %d persisted instrument(s).", len(instruments))
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

    # NIFTY 50 + active-stocks scanners — auto-scan on startup if needed
    scanner = None
    active_scanner = None
    if settings.data_source in {"yahoo", "demo"}:
        from scanner import ACTIVE_STOCKS, NiftyScanner

        scanner = NiftyScanner(logger)
        scanner.ensure_today()
        active_scanner = NiftyScanner(
            logger, symbols=ACTIVE_STOCKS, picks_file="active_picks.json"
        )
        active_scanner.ensure_today()
    else:
        logger.info("Scanners disabled for data_source=%s.", settings.data_source)

    app = create_app(watch, settings, scanner=scanner, active_scanner=active_scanner)
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
