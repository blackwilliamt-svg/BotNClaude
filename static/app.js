const $ = (sel) => document.querySelector(sel);

async function api(path, opts) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

function fmtUsd(v) {
  if (v === null || v === undefined) return "—";
  return "$" + Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function fmtPct(v) {
  if (v === null || v === undefined) return "—";
  return Number(v).toFixed(2) + "%";
}

function fmtTs(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

// ---- chart ------------------------------------------------------------
let chart, equitySeries;
function initChart() {
  chart = LightweightCharts.createChart($("#chart"), {
    layout: { background: { color: "#161b22" }, textColor: "#e6edf3" },
    grid: { vertLines: { color: "#2a2f3a" }, horzLines: { color: "#2a2f3a" } },
    timeScale: { timeVisible: true },
    width: $("#chart").clientWidth,
    height: 360,
  });
  equitySeries = chart.addAreaSeries({
    lineColor: "#58a6ff", topColor: "rgba(88,166,255,0.3)", bottomColor: "rgba(88,166,255,0.0)",
  });
  window.addEventListener("resize", () => chart.applyOptions({ width: $("#chart").clientWidth }));
}

async function refreshChart() {
  const points = await api("/api/equity_curve?limit=2000");
  const seen = new Set();
  const data = [];
  for (const p of points) {
    const t = Math.floor(p.ts);
    if (seen.has(t)) continue;
    seen.add(t);
    data.push({ time: t, value: p.equity });
  }
  if (data.length) equitySeries.setData(data);
}

// ---- price chart (candles + SMA50/200 + confidence markers, RSI, volume) --
let priceChart, candleSeries, smaFastSeries, smaSlowSeries;
let rsiChart, rsiSeries;
let volumeChart, volumeSeries, volumeAvgSeries;

const CHART_OPTS_BASE = {
  layout: { background: { color: "#161b22" }, textColor: "#e6edf3" },
  grid: { vertLines: { color: "#2a2f3a" }, horzLines: { color: "#2a2f3a" } },
  timeScale: { timeVisible: true },
};

function initPriceCharts() {
  priceChart = LightweightCharts.createChart($("#price-chart"), {
    ...CHART_OPTS_BASE, width: $("#price-chart").clientWidth, height: 300,
  });
  candleSeries = priceChart.addCandlestickSeries({
    upColor: "#3fb950", downColor: "#f85149", borderVisible: false,
    wickUpColor: "#3fb950", wickDownColor: "#f85149",
  });
  smaFastSeries = priceChart.addLineSeries({ color: "#f0b90b", lineWidth: 1 });
  smaSlowSeries = priceChart.addLineSeries({ color: "#8957e5", lineWidth: 1 });

  rsiChart = LightweightCharts.createChart($("#rsi-chart"), {
    ...CHART_OPTS_BASE, width: $("#rsi-chart").clientWidth, height: 100,
  });
  rsiSeries = rsiChart.addLineSeries({ color: "#58a6ff", lineWidth: 1 });

  volumeChart = LightweightCharts.createChart($("#volume-chart"), {
    ...CHART_OPTS_BASE, width: $("#volume-chart").clientWidth, height: 100,
  });
  volumeSeries = volumeChart.addHistogramSeries({ color: "#2a2f3a" });
  volumeAvgSeries = volumeChart.addLineSeries({ color: "#3fb950", lineWidth: 1 });

  // Keep the three time axes in lockstep so a scroll/zoom on one scrolls all.
  const charts = [priceChart, rsiChart, volumeChart];
  charts.forEach((c, i) => {
    c.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (!range) return;
      charts.forEach((other, j) => {
        if (i !== j) other.timeScale().setVisibleLogicalRange(range);
      });
    });
  });

  window.addEventListener("resize", () => {
    priceChart.applyOptions({ width: $("#price-chart").clientWidth });
    rsiChart.applyOptions({ width: $("#rsi-chart").clientWidth });
    volumeChart.applyOptions({ width: $("#volume-chart").clientWidth });
  });
}

async function refreshPriceChart() {
  const data = await api("/api/chart?limit=300");
  $("#price-chart-pair").textContent = data.pair;

  candleSeries.setData(data.candles);
  smaFastSeries.setData(data.sma_fast);
  smaSlowSeries.setData(data.sma_slow);
  rsiSeries.setData(data.rsi);
  volumeSeries.setData(data.volume);
  volumeAvgSeries.setData(data.volume_avg);

  const markers = data.markers.map((m) => ({
    time: m.time,
    position: "aboveBar",
    color: m.approved ? "#3fb950" : "#f85149",
    shape: "circle",
    text: `${Math.round(m.confidence)}%`,
  }));
  candleSeries.setMarkers(markers);
}

// ---- status -------------------------------------------------------------
async function refreshStatus() {
  const s = await api("/api/status");
  const badge = $("#mode-badge");
  badge.textContent = s.mode;
  badge.className = "mode-badge " + s.mode;
  $("#btn-mode-toggle").textContent = s.mode === "paper" ? "Go Live" : "Switch to Paper";

  $("#stat-equity").textContent = fmtUsd(s.equity);
  const dd = $("#stat-drawdown");
  dd.textContent = fmtPct(s.drawdown_pct);
  dd.className = "value " + (s.drawdown_pct >= 15 ? "danger" : s.drawdown_pct >= 8 ? "warn" : "");

  const margin = $("#stat-margin");
  margin.textContent = s.margin_level !== null ? fmtPct(s.margin_level) : "n/a (paper)";
  margin.className = "value " + (s.margin_status === "danger" ? "danger" : s.margin_status === "warning" ? "warn" : "");

  $("#stat-positions").textContent = s.open_position_count;

  const engineStat = $("#stat-engine");
  engineStat.textContent = s.engine_loop_alive ? "alive" : "STALLED";
  engineStat.className = "value " + (s.engine_loop_alive ? "pos" : "danger");
  engineStat.title = `scan last: ${fmtTs(s.last_poll_at)} · checkin last: ${fmtTs(s.last_checkin_poll_at)}`
    + (s.last_loop_error ? ` · last error in ${s.last_loop_error.loop} at ${fmtTs(s.last_loop_error.ts)}` : "");

  $("#kill-status").textContent = s.kill_switch ? "⛔ HALTED" : "";
  $("#btn-resume").style.display = s.kill_switch ? "inline-block" : "none";
  $("#btn-kill").style.display = s.kill_switch ? "none" : "inline-block";
}

// ---- positions ------------------------------------------------------------
async function refreshPositions() {
  const rows = await api("/api/positions");
  const tbody = $("#positions-table tbody");
  tbody.innerHTML = "";
  for (const p of rows) {
    const tr = document.createElement("tr");
    const pnlClass = p.unrealized_pnl > 0 ? "pos" : p.unrealized_pnl < 0 ? "neg" : "";
    tr.innerHTML = `
      <td>${p.pair}</td>
      <td class="side-${p.side}">${p.side.toUpperCase()}</td>
      <td>${Number(p.size).toFixed(6)}</td>
      <td>${p.leverage}x</td>
      <td>${fmtUsd(p.entry_price)}</td>
      <td>${fmtUsd(p.current_price)}</td>
      <td>${fmtUsd(p.stop_price)}</td>
      <td>${fmtUsd(p.liquidation_price_est)}</td>
      <td class="${pnlClass}">${fmtUsd(p.unrealized_pnl)}</td>`;
    tbody.appendChild(tr);
  }
}

// ---- trade history --------------------------------------------------------
async function refreshTrades() {
  const rows = await api("/api/trades?limit=100");
  const tbody = $("#trades-table tbody");
  tbody.innerHTML = "";
  for (const t of rows) {
    const tr = document.createElement("tr");
    const pnlClass = t.pnl > 0 ? "pos" : t.pnl < 0 ? "neg" : "";
    tr.innerHTML = `
      <td>${t.pair}</td>
      <td class="side-${t.side}">${t.side.toUpperCase()}</td>
      <td>${t.mode}</td>
      <td>${fmtUsd(t.entry_price)}</td>
      <td>${fmtUsd(t.exit_price)}</td>
      <td>${t.leverage}x</td>
      <td class="${pnlClass}">${t.pnl !== null ? fmtUsd(t.pnl) : "open"}</td>
      <td>${t.exit_reason || "—"}</td>`;
    tbody.appendChild(tr);
  }
}

// ---- reasoning feed / events ------------------------------------------------
async function refreshFeed() {
  const decisions = await api("/api/decisions?limit=80");
  const feed = $("#decisions-feed");
  feed.innerHTML = "";
  for (const d of decisions) {
    const div = document.createElement("div");
    div.className = "feed-item" + (d.stage === "scan" ? " scan-item" : "");
    const costPart = d.stage === "scan" ? "" : ` · $${(d.cost_usd || 0).toFixed(3)}`;
    div.innerHTML = `
      <div class="meta stage-${d.stage}">${d.stage.toUpperCase()} · ${d.pair || ""} · ${fmtTs(d.ts)}${costPart}</div>
      <div>${d.summary || ""}</div>`;
    feed.appendChild(div);
  }

  const events = await api("/api/events?limit=50");
  const efeed = $("#events-feed");
  efeed.innerHTML = "";
  for (const e of events) {
    const div = document.createElement("div");
    div.className = "feed-item";
    div.innerHTML = `<div class="meta">${e.level.toUpperCase()} · ${fmtTs(e.ts)}</div><div>${e.message}</div>`;
    efeed.appendChild(div);
  }
}

// ---- proposals --------------------------------------------------------------
async function refreshProposalCount() {
  const proposals = await api("/api/settings/proposals");
  const pending = proposals.filter((p) => p.status === "pending");
  $("#proposal-count").textContent = pending.length ? `(${pending.length})` : "";
}

async function openProposals() {
  const proposals = await api("/api/settings/proposals");
  const list = $("#proposals-list");
  list.innerHTML = "";
  if (!proposals.length) list.innerHTML = "<p>No proposals yet.</p>";
  for (const p of proposals) {
    const div = document.createElement("div");
    div.className = "proposal";
    const badgeClass = p.status === "approved" ? "badge-approved" : p.status === "rejected" ? "badge-rejected" : "badge-pending";
    div.innerHTML = `
      <div class="meta">${fmtTs(p.ts)} — <span class="${badgeClass}">${p.status}</span></div>
      <div>${p.rationale}</div>
      <pre>${JSON.stringify(JSON.parse(p.proposed_json), null, 2)}</pre>
      ${p.backtest_result ? `<pre>Backtest: ${JSON.stringify(JSON.parse(p.backtest_result), null, 2)}</pre>` : ""}
      ${p.status === "pending" ? `
        <div style="display:flex; gap:8px;">
          <button class="primary" data-approve="${p.id}">Approve</button>
          <button class="danger" data-reject="${p.id}">Reject</button>
        </div>` : ""}
    `;
    list.appendChild(div);
  }
  list.querySelectorAll("[data-approve]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await api(`/api/settings/proposals/${btn.dataset.approve}/approve`, { method: "POST" });
      openProposals(); refreshProposalCount();
    }));
  list.querySelectorAll("[data-reject]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      await api(`/api/settings/proposals/${btn.dataset.reject}/reject`, { method: "POST" });
      openProposals(); refreshProposalCount();
    }));
  $("#proposals-modal").classList.add("open");
}

// ---- settings modal -----------------------------------------------------------
function buildSettingsForm(data) {
  const c = data.config;
  const s = data.secrets;
  const form = $("#settings-form");
  form.innerHTML = `
    <div class="settings-grid">
      <div>
        <label>Poll interval (sec)</label>
        <input type="number" id="s-poll" value="${c.poll_interval_sec}">
        <label>Check-in interval (sec)</label>
        <input type="number" id="s-checkin" value="${c.checkin_interval_sec}">
        <label>Settings review interval (hours)</label>
        <input type="number" id="s-review-hours" value="${c.settings_review_interval_hours}">
        <label>RSI oversold</label>
        <input type="number" id="s-rsi-os" value="${c.signals.rsi_oversold}">
        <label>RSI overbought</label>
        <input type="number" id="s-rsi-ob" value="${c.signals.rsi_overbought}">
        <label>Volume multiplier min</label>
        <input type="number" step="0.1" id="s-vol-mult" value="${c.signals.volume_mult_min}">
        <label><input type="checkbox" id="s-vol-confirm" ${c.signals.require_volume_confirmation ? "checked" : ""}> Require volume confirmation</label>
      </div>
      <div>
        <label>Risk % per trade (max ${data.hard_limits.risk_pct_max * 100}%)</label>
        <input type="number" step="0.001" id="s-risk-pct" value="${c.risk.risk_pct}">
        <label>ATR stop multiplier</label>
        <input type="number" step="0.1" id="s-atr-mult" value="${c.risk.atr_stop_mult}">
        <label>Per-position exposure % (${data.hard_limits.per_position_exposure_min * 100}-${data.hard_limits.per_position_exposure_max * 100}%)</label>
        <input type="number" step="0.01" id="s-per-pos" value="${c.risk.per_position_exposure_pct}">
        <label>Total exposure % (${data.hard_limits.total_exposure_min * 100}-${data.hard_limits.total_exposure_max * 100}%)</label>
        <input type="number" step="0.01" id="s-total-exp" value="${c.risk.total_exposure_pct}">
        <label>Min edge USD (cost filter)</label>
        <input type="number" step="0.1" id="s-min-edge" value="${c.cost_filter.min_edge_usd}">
        <label>Trading pairs (comma-separated)</label>
        <input type="text" id="s-pairs" value="${c.pairs.join(',')}">
      </div>
    </div>
    <hr>
    <div class="settings-grid">
      <div>
        <label>Kraken API key ${s.kraken_api_key.configured ? `(set, ...${s.kraken_api_key.last4})` : "(not set)"}</label>
        <input type="password" id="s-kraken-key" placeholder="leave blank to keep current">
        <label>Kraken API secret ${s.kraken_api_secret.configured ? `(set, ...${s.kraken_api_secret.last4})` : "(not set)"}</label>
        <input type="password" id="s-kraken-secret" placeholder="leave blank to keep current">
      </div>
      <div>
        <label>Anthropic API key ${s.anthropic_api_key.configured ? `(set, ...${s.anthropic_api_key.last4})` : "(not set)"}</label>
        <input type="password" id="s-anthropic-key" placeholder="leave blank to keep current">
        <label>Dashboard username</label>
        <input type="text" id="s-dash-user" value="${s.dashboard_username}">
        <label>Dashboard password</label>
        <input type="password" id="s-dash-pass" placeholder="leave blank to keep current">
      </div>
    </div>
    <p style="color:var(--muted); font-size:11px;">
      Never enable withdrawal/funding permissions on the Kraken API key — trade + query only.
    </p>
  `;
}

async function openSettings() {
  const data = await api("/api/settings");
  buildSettingsForm(data);
  $("#settings-modal").classList.add("open");
}

async function saveSettings() {
  const patch = {
    config: {
      poll_interval_sec: Number($("#s-poll").value),
      checkin_interval_sec: Number($("#s-checkin").value),
      settings_review_interval_hours: Number($("#s-review-hours").value),
      pairs: $("#s-pairs").value.split(",").map((p) => p.trim()).filter(Boolean),
      signals: {
        rsi_oversold: Number($("#s-rsi-os").value),
        rsi_overbought: Number($("#s-rsi-ob").value),
        volume_mult_min: Number($("#s-vol-mult").value),
        require_volume_confirmation: $("#s-vol-confirm").checked,
      },
      risk: {
        risk_pct: Number($("#s-risk-pct").value),
        atr_stop_mult: Number($("#s-atr-mult").value),
        per_position_exposure_pct: Number($("#s-per-pos").value),
        total_exposure_pct: Number($("#s-total-exp").value),
      },
      cost_filter: { min_edge_usd: Number($("#s-min-edge").value) },
    },
    secrets: {},
  };
  const kk = $("#s-kraken-key").value, ks = $("#s-kraken-secret").value;
  const ak = $("#s-anthropic-key").value, du = $("#s-dash-user").value, dp = $("#s-dash-pass").value;
  if (kk) patch.secrets.kraken_api_key = kk;
  if (ks) patch.secrets.kraken_api_secret = ks;
  if (ak) patch.secrets.anthropic_api_key = ak;
  if (du) patch.secrets.dashboard_username = du;
  if (dp) patch.secrets.dashboard_password = dp;
  await api("/api/settings", { method: "POST", body: JSON.stringify(patch) });
  $("#settings-modal").classList.remove("open");
}

// ---- wiring ---------------------------------------------------------------
$("#btn-settings").addEventListener("click", openSettings);
$("#settings-cancel").addEventListener("click", () => $("#settings-modal").classList.remove("open"));
$("#settings-save").addEventListener("click", saveSettings);

$("#btn-proposals").addEventListener("click", openProposals);
$("#proposals-close").addEventListener("click", () => $("#proposals-modal").classList.remove("open"));

$("#btn-kill").addEventListener("click", async () => {
  const flatten = confirm("Also flatten (close) all open positions now?");
  await api("/api/kill", { method: "POST", body: JSON.stringify({ flatten }) });
  refreshStatus();
});
$("#btn-resume").addEventListener("click", async () => {
  await api("/api/resume", { method: "POST" });
  refreshStatus();
});

$("#btn-mode-toggle").addEventListener("click", async () => {
  const s = await api("/api/status");
  const target = s.mode === "paper" ? "live" : "paper";
  if (target === "live" && !confirm("Switch to LIVE trading with real funds?")) return;
  try {
    await api("/api/mode", { method: "POST", body: JSON.stringify({ mode: target }) });
    refreshStatus();
  } catch (e) {
    alert("Could not switch mode: " + e.message);
  }
});

async function refreshAll() {
  try {
    await Promise.all([
      refreshStatus(), refreshPositions(), refreshTrades(),
      refreshFeed(), refreshChart(), refreshPriceChart(), refreshProposalCount(),
    ]);
  } catch (e) {
    console.error("refresh failed", e);
  }
}

initChart();
initPriceCharts();
refreshAll();
setInterval(refreshAll, 8000);
