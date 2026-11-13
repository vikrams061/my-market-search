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
  .perf-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: .75rem; margin: .75rem 0; }
  .perf-stat { background: #1e293b; border-radius: 10px; padding: .8rem 1rem; }
  .perf-stat .label { font-size: .72rem; color: #94a3b8; text-transform: uppercase; letter-spacing: .04em; }
  .perf-stat .value { font-size: 1.3rem; font-weight: 700; margin-top: .2rem; }
  #perf-table { width: 100%; border-collapse: collapse; background: #1e293b;
               border-radius: 10px; overflow: hidden; font-size: .82rem; }
  #perf-table th, #perf-table td { padding: .45rem .8rem; text-align: left; }
  #perf-table th { background: #334155; font-size: .72rem; text-transform: uppercase; }
  #perf-table tr + tr td { border-top: 1px solid #334155; }
  .today-tag { font-size: .65rem; background: #0ea5e9; color: #fff;
               border-radius: 4px; padding: .1rem .35rem; margin-left: .35rem; }
  .tabs { display: flex; gap: .4rem; margin: 1.25rem 0 1.5rem; flex-wrap: wrap; }
  .tab-btn {
    background: #1e293b; color: #94a3b8; border: 1px solid #334155;
    border-radius: 8px; padding: .5rem 1rem; font-size: .85rem; cursor: pointer;
  }
  .tab-btn.active { background: #6366f1; color: #fff; border-color: #6366f1; }
  .tab-pane { display: none; }
  .tab-pane.active { display: block; }
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

  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('dashboard', this)">Dashboard</button>
    <button class="tab-btn" onclick="switchTab('analyst', this)">Analyst</button>
    <button class="tab-btn" onclick="switchTab('nifty', this)">NIFTY 50</button>
    <button class="tab-btn" onclick="switchTab('active', this)">Active Stocks</button>
    <button class="tab-btn" onclick="switchTab('check', this)">Trend Check</button>
    <button class="tab-btn" onclick="switchTab('radar', this)">Momentum Radar</button>
    <button class="tab-btn" onclick="switchTab('options', this)">Options Desk</button>
    <button class="tab-btn" onclick="switchTab('orb', this)">ORB / VWAP</button>
    <button class="tab-btn" onclick="switchTab('credit', this)">Credit Desk</button>
  </div>

  <div class="tab-pane active" id="pane-dashboard">
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
    <h2>Trade Tracker</h2>
    <div class="meta" style="font-size:.8rem;color:#64748b;margin:-.4rem 0 .6rem">Track any stock, index, or option (CE/PE) with entry, target &amp; stop-loss — live status and P&L.</div>
    <div class="controls">
      <select id="tr-asset" onchange="trAssetChange()">
        <option value="EQ">Stock/Index</option>
        <option value="OPTION">Option</option>
      </select>
      <input id="tr-sym" placeholder="Symbol / Underlying (e.g. RELIANCE, NIFTY)" maxlength="30" />
      <select id="tr-kind"><option value="EQ">Stock</option><option value="INDEX">Index</option></select>
      <select id="tr-side"><option value="long">Long</option><option value="short">Short</option></select>
      <input id="tr-entry" placeholder="Entry ₹" type="number" step="0.01" style="width:90px" />
      <input id="tr-target" placeholder="Target ₹" type="number" step="0.01" style="width:90px" />
      <input id="tr-sl" placeholder="Stop ₹" type="number" step="0.01" style="width:90px" />
      <button onclick="addTrade()">+ Track</button>
    </div>
    <div class="controls" id="tr-opt-row" style="display:none">
      <input id="tr-expiry" type="date" style="width:140px" />
      <input id="tr-strike" placeholder="Strike ₹" type="number" step="0.01" style="width:90px" />
      <select id="tr-opttype"><option value="CE">CE (Call)</option><option value="PE">PE (Put)</option></select>
    </div>
    <table id="trade-table">
      <thead><tr>
        <th>Symbol</th><th>Type</th><th class="num">Entry</th><th class="num">Current</th>
        <th class="num">Target</th><th class="num">Stop</th><th class="num">P&L</th><th>Status</th><th></th>
      </tr></thead>
      <tbody id="trade-rows"></tbody>
    </table>
  </div>

  <div class="analyst">
    <h2>Agent Performance
      <button class="scan-btn" onclick="refreshPerf(true)">Refresh</button>
    </h2>
    <div id="agent-cards" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:.75rem;margin:.75rem 0"></div>
    <div class="perf-grid" id="perf-stats"></div>
    <table id="perf-table">
      <thead><tr>
        <th>Date</th><th>Agent</th><th>Side</th><th>Symbol</th>
        <th class="num">Entry</th><th class="num">Current</th><th class="num">Return</th>
      </tr></thead>
      <tbody id="perf-rows"></tbody>
    </table>
    <div id="optimizer" style="margin-top:.75rem"></div>

  </div>
  </div><!-- /pane-dashboard -->

  <div class="tab-pane" id="pane-analyst">
  <div class="analyst">
    <h2>Equity Analyst Agent</h2>
    <div class="meta" style="font-size:.8rem;color:#64748b;margin:-.4rem 0 .6rem">7-phase momentum report on your watchlist: news, technicals, targets, option strategy.</div>
    <button id="analyze-btn" onclick="runAnalysis()">Run Momentum Analysis</button>
    <div id="analysis-status"></div>
    <div id="report"></div>
  </div>
  </div><!-- /pane-analyst -->

  <div class="tab-pane" id="pane-nifty">
  <div class="analyst">
    <div class="meta" style="font-size:.8rem;color:#64748b;margin:-.4rem 0 .6rem">Daily momentum scan of all 50 NIFTY constituents — top bullish/bearish picks with bias &amp; confidence.</div>
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
  </div><!-- /pane-nifty -->

  <div class="tab-pane" id="pane-active">
  <div class="analyst">
    <div class="meta" style="font-size:.8rem;color:#64748b;margin:-.4rem 0 .6rem">Momentum scan of ~100 liquid NSE large/mid caps for today's active picks.</div>
    <h2>Today's Active Stock Picks
      <button class="scan-btn" id="active-scan-btn" onclick="startActiveScan()">Rescan</button>
    </h2>
    <div id="active-scan-status" style="font-size:.85rem;color:#94a3b8;margin-bottom:.5rem"></div>
    <div class="picks" id="active-cards"></div>
  </div>
  </div><!-- /pane-active -->

  <div class="tab-pane" id="pane-check">
  <div class="analyst">
    <h2>Trend &amp; Momentum Check</h2>
    <div class="meta" style="font-size:.8rem;color:#64748b;margin:-.4rem 0 .6rem">On-demand trend bias, momentum score &amp; confidence for any stock or index.</div>
    <div class="controls">
      <input id="check-sym" placeholder="Any symbol (e.g. TATAMOTORS, NIFTY)" maxlength="30" />
      <select id="check-kind">
        <option value="EQ">Stock</option>
        <option value="INDEX">Index</option>
      </select>
      <button onclick="checkSymbol()">Check</button>
    </div>
    <div id="check-result"></div>
  </div>
  </div><!-- /pane-check -->

  <div class="tab-pane" id="pane-radar">
  <div class="analyst">
    <div class="meta" style="font-size:.8rem;color:#64748b;margin:-.4rem 0 .6rem">Broad ~180-symbol multi-cap scan (small/mid/large) — top momentum names with bias &amp; confidence.</div>
    <h2>Momentum Radar — All Caps
      <button class="scan-btn" id="mom-scan-btn" onclick="startMomScan()">Rescan</button>
    </h2>
    <div id="mom-scan-status" style="font-size:.85rem;color:#94a3b8;margin-bottom:.5rem"></div>
    <div class="lists">
      <div class="mini-list"><h4>Top Bullish</h4><div id="mom-top"></div></div>
      <div class="mini-list"><h4>Top Bearish</h4><div id="mom-bottom"></div></div>
    </div>
  </div>
  </div><!-- /pane-radar -->

  <div class="tab-pane" id="pane-options">
  <div class="analyst">
    <h2>Options Desk — Defined-Risk Spreads</h2>
    <div class="meta" style="font-size:.8rem;color:#64748b;margin:-.4rem 0 .6rem">RSI+EMA swing setups with Bull Call / Bear Put spreads, targets &amp; 1:2 R:R checks.</div>
    <div class="controls">
      <input id="opt-sym" placeholder="Symbol (e.g. RELIANCE, NIFTY)" maxlength="30" />
      <select id="opt-kind">
        <option value="EQ">Stock</option>
        <option value="INDEX">Index</option>
      </select>
      <button onclick="runOptions()">Analyze</button>
    </div>
    <div id="opt-status" style="font-size:.85rem;color:#94a3b8"></div>
    <div id="opt-report"></div>
  </div>
  </div><!-- /pane-options -->

  <div class="tab-pane" id="pane-orb">
  <div class="analyst">
    <h2>ORB / VWAP Momentum Desk</h2>
    <div class="meta" style="font-size:.8rem;color:#64748b;margin:-.4rem 0 .6rem">15m opening-range breakout + session VWAP + RVOL filter for intraday option entries.</div>
    <div class="controls">
      <input id="orb-sym" placeholder="Symbol (e.g. RELIANCE, NIFTY)" maxlength="30" />
      <select id="orb-kind">
        <option value="EQ">Stock</option>
        <option value="INDEX">Index</option>
      </select>
      <button onclick="runOrb()">Analyze</button>
    </div>
    <div id="orb-status" style="font-size:.85rem;color:#94a3b8"></div>
    <div id="orb-report"></div>
  </div>
  </div><!-- /pane-orb -->

  <div class="tab-pane" id="pane-credit">
  <div class="analyst">
    <h2>Credit Desk — Defined-Risk Selling
      <button class="scan-btn" onclick="runCredit()">Refresh</button>
    </h2>
    <div class="meta" style="font-size:.8rem;color:#64748b;margin:-.4rem 0 .6rem">Regime-filtered credit strategies on <b>NIFTY</b> — auto-refreshes every 5 min; picks tracked for win/loss.</div>
    <div id="credit-status" style="font-size:.85rem;color:#94a3b8"></div>
    <div id="credit-report"></div>
  </div>
  </div><!-- /pane-credit -->

<script>
function switchTab(name, btn) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('pane-' + name).classList.add('active');
  btn.classList.add('active');
}
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
    <div class="score">${p.trend_bias || ''} · Confidence ${p.confidence != null ? p.confidence + '%' : '—'} · Momentum ${p.momentum_score}/100 · RSI ${t.rsi_14 != null ? t.rsi_14.toFixed(0) : '—'}</div>
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
      <div class="score">${pk.trend_bias || ''} · Confidence ${pk.confidence != null ? pk.confidence + '%' : '—'} · Momentum ${pk.momentum_score}/100 · RSI ${t.rsi_14 != null ? t.rsi_14.toFixed(0) : '—'}</div>
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

// ---- Trend & momentum quick check ----------------------------------------
async function checkSymbol() {
  const sym = document.getElementById('check-sym').value.trim().toUpperCase();
  const kind = document.getElementById('check-kind').value;
  if (sym) showCheck(sym, kind);
}

async function showCheck(sym, kind) {
  const out = document.getElementById('check-result');
  out.innerHTML = '<div class="meta" style="color:#94a3b8">Analyzing…</div>';
  try {
    const res = await fetch(`/api/check?symbol=${encodeURIComponent(sym)}&kind=${kind}`);
    const d = await res.json();
    if (!res.ok) { out.innerHTML = `<div class="meta" style="color:#f87171">${d.detail || 'Check failed'}</div>`; return; }
    const t = d.technicals || {};
    out.innerHTML = `<div class="pick-card" style="max-width:520px">
      <h3>${d.symbol} <span class="badge">${d.kind}</span></h3>
      <div class="score">${d.trend_bias} · Confidence ${d.confidence}% · Momentum ${d.momentum_score}/100</div>
      <div class="ltp">${fmtInr(d.quote && d.quote.ltp)}</div>
      <div class="meta">Day ${d.quote && d.quote.change_pct != null ? sgn(d.quote.change_pct) + d.quote.change_pct.toFixed(2) + '%' : '—'} ·
        RSI ${t.rsi_14 != null ? t.rsi_14.toFixed(1) : '—'} ·
        Vol ${t.volume_vs_20d_avg != null ? t.volume_vs_20d_avg + 'x' : '—'}<br>
        EMA20 ${fmtInr(t.ema_20)} · EMA50 ${fmtInr(t.ema_50)} · 200SMA ${fmtInr(t.sma_200)}</div>
    </div>`;
  } catch (e) {
    out.innerHTML = '<div class="meta" style="color:#f87171">Request failed</div>';
  }
}

// ---- Momentum radar (broad multi-cap scan) --------------------------------
function momRow(e) {
  const biasCls = e.bias === 'BULLISH' ? 'up' : e.bias === 'BEARISH' ? 'down' : 'flat';
  return `<div style="cursor:pointer" title="Click for detail" onclick="showCheck('${e.symbol}','EQ')"><span>${e.symbol}</span>
    <span><span class="${biasCls}">${e.bias || ''}</span>
    ${e.confidence != null ? ' · ' + e.confidence + '%' : ''}
    · ${e.score} · ${fmtInr(e.cmp)}</span></div>`;
}

async function refreshMomPicks() {
  try {
    const res = await fetch('/api/momentum/picks');
    const d = await res.json();
    if (d.status !== 'ready' || !d.picks) {
      document.getElementById('mom-top').innerHTML = '<div style="color:#64748b">Scanning…</div>';
      return;
    }
    document.getElementById('mom-top').innerHTML =
      (d.picks.top5 || []).map(momRow).join('');
    document.getElementById('mom-bottom').innerHTML =
      (d.picks.bottom5 || []).map(momRow).join('');
  } catch (e) {}
}

async function momScanStatus() {
  try {
    const res = await fetch('/api/momentum/scan/status');
    const d = await res.json();
    document.getElementById('mom-scan-status').textContent =
      d.status === 'running' ? `Scanning all caps… ${d.progress || ''}` :
      d.status === 'done' ? `Last scan: ${d.progress || ''} symbols` : '';
    document.getElementById('mom-scan-btn').disabled = d.status === 'running';
  } catch (e) {}
}

async function startMomScan() {
  document.getElementById('mom-scan-btn').disabled = true;
  await fetch('/api/momentum/scan', {method: 'POST'});
  setInterval(momScanStatus, 3000);
  momScanStatus();
}

// ---- Options desk ---------------------------------------------------------
async function runOptions() {
  const sym = document.getElementById('opt-sym').value.trim().toUpperCase();
  const kind = document.getElementById('opt-kind').value;
  const statusEl = document.getElementById('opt-status');
  const out = document.getElementById('opt-report');
  if (!sym) return;
  statusEl.textContent = 'Analyzing…';
  out.innerHTML = '';
  try {
    const res = await fetch(`/api/options/analyze?symbol=${encodeURIComponent(sym)}&kind=${kind}`);
    const d = await res.json();
    if (!res.ok) { statusEl.textContent = d.detail || 'Analysis failed'; return; }
    statusEl.textContent = '';
    out.innerHTML = d.html;
  } catch (e) {
    statusEl.textContent = 'Request failed';
  }
}

// ---- ORB / VWAP desk ------------------------------------------------------
async function runOrb() {
  const sym = document.getElementById('orb-sym').value.trim().toUpperCase();
  const kind = document.getElementById('orb-kind').value;
  const statusEl = document.getElementById('orb-status');
  const out = document.getElementById('orb-report');
  if (!sym) return;
  statusEl.textContent = 'Analyzing…';
  out.innerHTML = '';
  try {
    const res = await fetch(`/api/orb/analyze?symbol=${encodeURIComponent(sym)}&kind=${kind}`);
    const d = await res.json();
    if (!res.ok) { statusEl.textContent = d.detail || 'Analysis failed'; return; }
    statusEl.textContent = '';
    out.innerHTML = d.html;
  } catch (e) {
    statusEl.textContent = 'Request failed';
  }
}

// ---- Credit desk (NIFTY only, auto-refresh) -------------------------------
async function runCredit() {
  const statusEl = document.getElementById('credit-status');
  const out = document.getElementById('credit-report');
  statusEl.textContent = 'Analyzing NIFTY…';
  try {
    const res = await fetch('/api/credit/analyze?symbol=NIFTY&kind=INDEX');
    const d = await res.json();
    if (!res.ok) { statusEl.textContent = d.detail || 'Analysis failed'; return; }
    statusEl.textContent = 'Updated ' + new Date().toLocaleTimeString('en-IN');
    out.innerHTML = d.html;
    const tracks = d.track || [];
    if (tracks.length) {
      out.innerHTML += `<table id="credit-strat" style="margin-top:.75rem">
        <thead><tr>
          <th>Strategy</th><th>Type</th><th class="num">Sell Strike</th><th class="num">Buy Strike</th>
          <th class="num">Credit</th><th class="num">Target</th><th class="num">Stop (Underlying)</th><th></th>
        </tr></thead>
        <tbody>${tracks.map(t => `<tr>
          <td>${t.strategy}</td>
          <td>${t.opt_type === 'CE' ? 'Call (CE)' : 'Put (PE)'}</td>
          <td class="num">${fmtInr(t.short_strike)}</td>
          <td class="num">${fmtInr(t.long_strike)}</td>
          <td class="num">₹${t.entry_credit}</td>
          <td class="num">₹${t.target}</td>
          <td class="num">${t.stop_loss != null ? fmtInr(t.stop_loss) : '—'}</td>
          <td><button class="scan-btn" onclick='trackCredit(${JSON.stringify(t)})'>+ Add</button></td>
        </tr>`).join('')}</tbody>
      </table>
      <div class="meta" style="font-size:.75rem;color:#64748b;margin-top:.4rem">Credits &amp; levels are estimates — verify on NSE option chain. Added trades appear in Dashboard → Trade Tracker.</div>`;
    }
  } catch (e) {
    statusEl.textContent = 'Request failed';
  }
}

async function trackCredit(t) {
  await fetch('/api/trades', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(t),
  });
  refreshPerf(true);
}
runCredit();
setInterval(runCredit, 300000);

// ---- Agent performance ----------------------------------------------------
async function refreshPerf(force) {
  try {
    const res = await fetch('/api/performance' + (force ? '?refresh=1' : ''));
    const d = await res.json();
    const stats = d.stats || {};
    const cell = (label, val, color) =>
      `<div class="perf-stat"><div class="label">${label}</div>
       <div class="value" style="color:${color || '#e2e8f0'}">${val}</div></div>`;
    const o = stats.overall || {};
    document.getElementById('perf-stats').innerHTML =
      cell('Total Picks', o.picks ?? '—') +
      cell('Win Rate', o.win_rate != null ? o.win_rate + '%' : '—', o.win_rate >= 50 ? '#4ade80' : '#f87171') +
      cell('Avg Return', o.avg_return != null ? sgn(o.avg_return) + o.avg_return + '%' : '—', cls(o.avg_return)) +
      cell('Best', o.best != null ? '+' + o.best + '%' : '—', '#4ade80') +
      cell('Worst', o.worst != null ? o.worst + '%' : '—', '#f87171');

    // Per-agent cards with description + track record
    const AGENTS = [
      {key: 'quotes',  name: 'Market Watch',      desc: 'Real-time LTP/quote feed for your watchlist (Yahoo delayed / Groww live).', src: null},
      {key: 'analyst', name: 'Equity Analyst',    desc: '7-phase momentum report: news scan, technicals, targets, option strategy.', src: null},
      {key: 'nifty',   name: 'NIFTY 50 Scanner',  desc: 'Daily momentum scan of all 50 NIFTY constituents; picks top bullish/bearish.', src: 'nifty'},
      {key: 'active',  name: 'Active Stocks',     desc: 'Momentum scan of ~100 liquid NSE large/mid caps.', src: 'active'},
      {key: 'momentum',name: 'Momentum Radar',    desc: 'Broad ~180-symbol multi-cap scan (small/mid/large) with trend bias + confidence.', src: 'momentum'},
      {key: 'credit',  name: 'Credit Desk',       desc: 'Intraday defined-risk credit strategies (spreads/condors) with regime, VIX & time-window filters.', src: 'credit'},
    ];
    const bySrc = stats.by_source || {};
    document.getElementById('agent-cards').innerHTML = AGENTS.map(a => {
      const st = a.src ? (bySrc[a.src] || {}) : {};
      const perf = a.src
        ? (st.scored
            ? `Picks ${st.picks} · Win ${st.win_rate}% · Avg ${sgn(st.avg_return)}${st.avg_return}%`
            : 'No scored picks yet')
        : 'Data feed — no picks';
      const color = st.avg_return == null ? '#94a3b8' : cls(st.avg_return);
      return `<div class="pick-card"><h3 style="font-size:.95rem">${a.name}</h3>
        <div class="score" style="color:${color};font-weight:600">${perf}</div>
        <div class="meta" style="margin-top:.3rem">${a.desc}</div></div>`;
    }).join('');
    const rows = document.getElementById('perf-rows');
    rows.innerHTML = (d.rows || []).map(r => `<tr>
      <td>${r.date}${r.is_today ? '<span class="today-tag">today</span>' : ''}</td>
      <td>${r.source}</td>
      <td>${r.side}</td>
      <td>${r.symbol}</td>
      <td class="num">${r.entry != null ? fmtInr(r.entry) : '—'}</td>
      <td class="num">${r.current != null ? fmtInr(r.current) : '—'}</td>
      <td class="num ${cls(r.return_pct)}">${r.return_pct != null ? sgn(r.return_pct) + r.return_pct + '%' : '—'}</td>
    </tr>`).join('') || '<tr><td colspan="7" style="color:#64748b">No picks recorded yet</td></tr>';
    const opt = d.optimizer || {};
    const findings = opt.findings || [];
    document.getElementById('optimizer').innerHTML = findings.length
      ? `<div class="mini-list" style="border-left:3px solid #f59e0b">
           <h4>Optimizer Suggestions</h4>
           ${findings.map(f => `<div style="display:block;padding:.3rem 0">• ${f}</div>`).join('')}
         </div>`
      : '';

    // Manual trades
    const trs = document.getElementById('trade-rows');
    trs.innerHTML = (d.trades || []).map(t => {
      const stCls = t.status === 'TARGET HIT' ? 'up' : t.status === 'STOPPED' ? 'down' : 'flat';
      const typeLbl = t.asset_type === 'OPTION'
        ? `${t.opt_type} ${t.strike} ${t.expiry || ''}` : `${t.kind} ${t.side}`;
      return `<tr>
        <td>${t.symbol}${t.asset_type === 'OPTION' ? ' <span style="color:#94a3b8;font-size:.72rem">(' + (t.underlying||'') + ')</span>' : ''}</td>
        <td style="font-size:.78rem">${typeLbl}</td>
        <td class="num">${fmtInr(t.entry_price)}</td>
        <td class="num">${t.current != null ? fmtInr(t.current) : '—'}</td>
        <td class="num">${t.target != null ? fmtInr(t.target) : '—'}</td>
        <td class="num">${t.stop_loss != null ? fmtInr(t.stop_loss) : '—'}</td>
        <td class="num ${cls(t.pnl_pct)}">${t.pnl_pct != null ? sgn(t.pnl_pct) + t.pnl_pct + '%' : '—'}</td>
        <td class="${stCls}">${t.status}</td>
        <td><button class="del-btn" onclick="delTrade('${t.id}')">✕</button></td>
      </tr>`;
    }).join('') || '<tr><td colspan="9" style="color:#64748b">No tracked trades — add one above</td></tr>';
  } catch (e) {}
}

function trAssetChange() {
  const isOpt = document.getElementById('tr-asset').value === 'OPTION';
  document.getElementById('tr-opt-row').style.display = isOpt ? 'flex' : 'none';
  document.getElementById('tr-side').style.display = isOpt ? 'none' : '';
  document.getElementById('tr-kind').style.display = isOpt ? 'none' : '';
}

async function addTrade() {
  const isOpt = document.getElementById('tr-asset').value === 'OPTION';
  const payload = {
    asset_type: document.getElementById('tr-asset').value,
    symbol: document.getElementById('tr-sym').value.trim(),
    kind: document.getElementById('tr-kind').value,
    side: document.getElementById('tr-side').value,
    entry_price: document.getElementById('tr-entry').value,
    target: document.getElementById('tr-target').value || null,
    stop_loss: document.getElementById('tr-sl').value || null,
  };
  if (isOpt) {
    payload.underlying = payload.symbol;
    payload.expiry = document.getElementById('tr-expiry').value || null;
    payload.strike = document.getElementById('tr-strike').value;
    payload.opt_type = document.getElementById('tr-opttype').value;
    if (!payload.strike) return;
  }
  if (!payload.symbol || !payload.entry_price) return;
  await fetch('/api/trades', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
  ['tr-sym','tr-entry','tr-target','tr-sl','tr-expiry','tr-strike'].forEach(id => document.getElementById(id).value = '');
  refreshPerf(true);
}

async function delTrade(id) {
  await fetch('/api/trades/' + id, {method: 'DELETE'});
  refreshPerf(true);
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
refreshPerf();
setInterval(refreshPerf, 30000);
refreshMomPicks();
setInterval(refreshMomPicks, 20000);
momScanStatus();
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


def create_app(
    watch: MarketWatch,
    settings: Settings,
    scanner=None,
    active_scanner=None,
    momentum_scanner=None,
    performance=None,
) -> FastAPI:
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

    # ------------------------------------------------------------------
    # Agent performance endpoint
    # ------------------------------------------------------------------
    @app.get("/api/performance")
    async def get_performance(refresh: bool = False) -> dict:
        if performance is None:
            return {"stats": {}, "rows": [], "optimizer": {"findings": []}}
        data = performance.report(force=refresh)
        data["optimizer"] = performance.suggestions()
        return data

    # ------------------------------------------------------------------
    # Momentum radar (broad multi-cap) endpoints
    # ------------------------------------------------------------------
    @app.post("/api/momentum/scan", status_code=202)
    async def start_momentum_scan() -> dict:
        if momentum_scanner is None:
            raise HTTPException(status_code=503, detail="Scanner not available")
        if not momentum_scanner.start_scan():
            raise HTTPException(status_code=409, detail="Scan already running")
        return {"status": "running"}

    @app.get("/api/momentum/scan/status")
    async def momentum_scan_status() -> dict:
        return momentum_scanner.status() if momentum_scanner else {"status": "unavailable"}

    @app.get("/api/momentum/picks")
    async def get_momentum_picks() -> dict:
        return momentum_scanner.today_picks() if momentum_scanner else {"status": "unavailable"}

    # ------------------------------------------------------------------
    # Trend & momentum quick check (any symbol/index)
    # ------------------------------------------------------------------
    @app.get("/api/check")
    async def quick_check(symbol: str, kind: str = "EQ") -> dict:
        from agent import analyze_symbol

        logger = logging.getLogger("market_watch")
        try:
            return analyze_symbol(symbol, kind, logger)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------------
    # Manual trade tracker endpoints
    # ------------------------------------------------------------------
    @app.post("/api/trades", status_code=201)
    async def add_trade(payload: dict) -> dict:
        if performance is None:
            raise HTTPException(status_code=503, detail="Performance agent not available")
        try:
            return performance._tracker.add(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.delete("/api/trades/{trade_id}")
    async def remove_trade(trade_id: str) -> dict:
        if performance is None:
            raise HTTPException(status_code=503, detail="Performance agent not available")
        if not performance._tracker.remove(trade_id):
            raise HTTPException(status_code=404, detail="Trade not found")
        return {"deleted": trade_id}

    # ------------------------------------------------------------------
    # Options desk agent
    # ------------------------------------------------------------------
    @app.get("/api/options/analyze")
    async def options_analyze(symbol: str, kind: str = "EQ") -> dict:
        from options_agent import analyze_options

        logger = logging.getLogger("market_watch")
        try:
            md = analyze_options(symbol, kind, logger)
            return {"html": markdown.markdown(md, extensions=["tables", "fenced_code", "nl2br"])}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------------
    # ORB / VWAP desk agent
    # ------------------------------------------------------------------
    @app.get("/api/orb/analyze")
    async def orb_analyze(symbol: str, kind: str = "EQ") -> dict:
        from orb_agent import analyze_orb

        logger = logging.getLogger("market_watch")
        try:
            md = analyze_orb(symbol, kind, logger)
            return {"html": markdown.markdown(md, extensions=["tables", "fenced_code", "nl2br"])}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ------------------------------------------------------------------
    # Credit desk agent
    # ------------------------------------------------------------------
    @app.get("/api/credit/analyze")
    async def credit_analyze(symbol: str = "NIFTY", kind: str = "INDEX") -> dict:
        from credit_agent import analyze_credit

        logger = logging.getLogger("market_watch")
        try:
            data = analyze_credit(symbol, kind, logger)
            return {
                "html": markdown.markdown(
                    data["markdown"], extensions=["tables", "fenced_code", "nl2br"]
                ),
                "track": data.get("track", []),
            }
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

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
    momentum_scanner = None
    if settings.data_source in {"yahoo", "demo"}:
        from performance import PerformanceAgent
        from scanner import ACTIVE_STOCKS, BROAD_STOCKS, NiftyScanner

        scanner = NiftyScanner(logger, source="nifty")
        scanner.ensure_today()
        active_scanner = NiftyScanner(
            logger, symbols=ACTIVE_STOCKS, picks_file="active_picks.json", source="active"
        )
        active_scanner.ensure_today()
        momentum_scanner = NiftyScanner(
            logger, symbols=BROAD_STOCKS, picks_file="momentum_picks.json", source="momentum"
        )
        momentum_scanner.ensure_today()
        performance = PerformanceAgent(logger)
    else:
        logger.info("Scanners disabled for data_source=%s.", settings.data_source)
        performance = None

    app = create_app(
        watch, settings, scanner=scanner, active_scanner=active_scanner,
        momentum_scanner=momentum_scanner, performance=performance,
    )
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