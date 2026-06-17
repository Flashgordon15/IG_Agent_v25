/**
 * IG Agent Flight Deck v29.1 — cache-busted telemetry + avionics log HUD
 */

const BUILD_TS = window.__COCKPIT_BUILD__ || Date.now();
const TELEMETRY_HZ = 2.5;
const LOG_HZ = 2.5;

/** Strict 4-gate map — no legacy G5/G6 or "Gate 1/6" references */
const GATE_ORDER = ["G1", "G2", "G3", "G4"];
const GATE_LABELS = {
  G1: "Gate 1 · Environment",
  G2: "Gate 2 · REST Auth",
  G3: "Gate 3 · Streaming",
  G4: "Gate 4 · State Hydration",
};

const ASSET_NAMES = {
  "CS.D.CFPGOLD.CFP.IP": "Gold",
  "IX.D.DOW.IFM.IP": "Wall Street",
  "IX.D.NIKKEI.IFM.IP": "Japan 225",
  "CS.D.EURUSD.CFD.IP": "EUR/USD",
};

const LOG_MAX_LINES = 120;
const TRIAGE_MAX_LINES = 100;
const SPARKLINE_LOOKBACK = 50;
const STALE_FEED_SEC = 5.0;
const logBuffer = [];
const triageBuffer = [];
const sparklineBuffers = new Map();
const sparklineCanvases = new Map();

const VITALS_MESSAGES = {
  HEALTHY:
    "🟢 All systems running correctly. E2E tests passing. AI is monitoring.",
  DEGRADED: (mod) =>
    `🟡 Fault found in ${mod || "System"}. Identifying and passing to sandbox engineer.`,
  PEAK: "🚀 Trading now. Afterburners are on.",
  EMERGENCY:
    "🚨 We have a real issue. Position lockdown triggered. Action required.",
};

const MARKET_STATE_LABELS = {
  LISTENING: "LISTENING",
  LEARNING: "LEARNING",
  TRADING: "TRADING",
};

const $ = (id) => document.getElementById(id);

function wsUrl(path) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${path}?v=${BUILD_TS}&t=${Date.now()}`;
}

function fmtMoney(n) {
  const v = Number(n) || 0;
  return `£${v.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function epicLabel(epic) {
  return ASSET_NAMES[epic] || String(epic).slice(-18);
}

function gateClass(status) {
  const s = String(status || "pending").toLowerCase();
  if (s === "complete" || s === "running") return "complete";
  if (s === "failed") return "failed";
  return "pending";
}

function renderGates(gates) {
  const grid = $("gate-grid");
  if (!grid) return;
  grid.innerHTML = "";
  for (const gid of GATE_ORDER) {
    const label = GATE_LABELS[gid];
    const g = (gates && gates[gid]) || {};
    const status = String(g.status || "pending").toUpperCase();
    const detail = String(g.detail || "").trim();
    const row = document.createElement("div");
    row.className = "gate-row";
    row.innerHTML = `
      <div class="gate-row-main">
        <span class="gate-id">${gid}</span>
        <span class="gate-name">${label}</span>
      </div>
      <div class="gate-row-meta">
        <span class="gate-pill ${gateClass(g.status)}">${status}</span>
        ${detail ? `<span class="gate-detail">${escapeHtml(detail)}</span>` : ""}
      </div>
    `;
    grid.appendChild(row);
  }
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function pushSparkline(epic, mid) {
  const v = Number(mid);
  if (!epic || Number.isNaN(v) || v <= 0) return;
  let buf = sparklineBuffers.get(epic);
  if (!buf) {
    buf = [];
    sparklineBuffers.set(epic, buf);
  }
  buf.push(v);
  while (buf.length > SPARKLINE_LOOKBACK) buf.shift();
}

function drawSparkline(canvas, epic) {
  if (!canvas) return;
  const buf = sparklineBuffers.get(epic) || [];
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (buf.length < 2) {
    ctx.strokeStyle = "rgba(100,116,139,0.5)";
    ctx.beginPath();
    ctx.moveTo(0, h / 2);
    ctx.lineTo(w, h / 2);
    ctx.stroke();
    return;
  }
  const min = Math.min(...buf);
  const max = Math.max(...buf);
  const span = max - min || 1;
  const rising = buf[buf.length - 1] >= buf[0];
  const stroke = rising ? "#c084fc" : "#ff6b6b";
  const glow = rising ? "rgba(192,132,252,0.55)" : "rgba(255,107,107,0.55)";
  ctx.shadowColor = glow;
  ctx.shadowBlur = 6;
  ctx.strokeStyle = stroke;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  for (let i = 0; i < buf.length; i++) {
    const x = (i / (buf.length - 1)) * (w - 2) + 1;
    const y = h - 2 - ((buf[i] - min) / span) * (h - 4);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();
  ctx.shadowBlur = 0;
}

function renderMasterVitalsBanner(payload) {
  const banner = $("master-vitals-banner");
  if (!banner) return;
  const key = String(payload.global_ai_status_key || "HEALTHY").toUpperCase();
  const mod =
    payload.global_ai_status_module ||
    payload.co_pilot_vitals?.degraded_module ||
    "System";
  let text;
  if (key === "DEGRADED") {
    text = VITALS_MESSAGES.DEGRADED(mod);
  } else {
    text = VITALS_MESSAGES[key] || VITALS_MESSAGES.HEALTHY;
  }
  banner.textContent = text;
  banner.className = `master-vitals-banner vitals-${key.toLowerCase()}`;
  banner.dataset.status = key;
}

function renderMarketStatusPill(card, epic, marketStates) {
  if (!card) return;
  const states = marketStates && typeof marketStates === "object" ? marketStates : {};
  const state = String(states[epic] || "LISTENING").toUpperCase();
  const label = MARKET_STATE_LABELS[state] || state;
  let pill = card.querySelector(".market-status-pill");
  if (!pill) {
    const hud = card.querySelector(".asset-hud-pills");
    if (hud) {
      pill = document.createElement("span");
      pill.className = "market-status-pill";
      hud.appendChild(pill);
    }
  }
  if (!pill) return;
  pill.textContent = label;
  pill.className = `market-status-pill state-${state.toLowerCase()}`;
  pill.title =
    state === "TRADING"
      ? "Position open — scalping strategies active"
      : state === "LEARNING"
        ? "NumPy arrays filling — classifiers and Z-scores updating"
        : "Stream channel open — gathering live market ticks";
}

function renderAssets(spread, epics, marketStates) {
  const container = $("asset-cards");
  if (!container) return;
  const keys = new Set([...Object.keys(spread || {}), ...Object.keys(epics || {})]);
  const order = [
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.DOW.IFM.IP",
    "IX.D.NIKKEI.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
  ];
  const sorted = [...keys].sort(
    (a, b) =>
      (order.indexOf(a) === -1 ? 99 : order.indexOf(a)) -
      (order.indexOf(b) === -1 ? 99 : order.indexOf(b))
  );

  for (const epic of sorted) {
    const sp = (spread && spread[epic]) || {};
    const q = (epics && epics[epic]) || {};
    const spr = sp.spread != null ? sp.spread : q.spread;
    const bid = Number(q.bid || 0);
    const offer = Number(q.offer || 0);
    const mid = bid > 0 && offer > 0 ? (bid + offer) / 2 : 0;
    if (mid > 0) pushSparkline(epic, mid);
    const ageS = Number(q.age_s ?? q.tick_age_s ?? 0);
    const stale = ageS > STALE_FEED_SEC;

    let card = container.querySelector(`[data-epic="${epic}"]`);
    if (!card) {
      card = document.createElement("div");
      card.className = "asset-card";
      card.dataset.epic = epic;
      card.innerHTML = `
        <div class="asset-card-top">
          <div class="asset-name"></div>
          <div class="asset-hud-pills">
            <span class="feed-status-pill">LIVE</span>
            <span class="market-status-pill state-listening">LISTENING</span>
          </div>
        </div>
        <canvas class="sparkline-canvas" width="280" height="44" aria-hidden="true"></canvas>
        <div class="asset-spread"></div>
        <div class="asset-meta">
          <span>Z <strong class="z-val">0</strong></span>
          <span>Throttle <strong class="th-val">0</strong></span>
        </div>
      `;
      container.appendChild(card);
      const canvas = card.querySelector(".sparkline-canvas");
      if (canvas) sparklineCanvases.set(epic, canvas);
    }

    card.classList.toggle("feed-stale", stale);
    const pill = card.querySelector(".feed-status-pill");
    if (pill) {
      pill.textContent = stale ? "[FEED STALE]" : "LIVE";
      pill.className = `feed-status-pill ${stale ? "stale" : "live"}`;
    }
    const nameEl = card.querySelector(".asset-name");
    if (nameEl) nameEl.textContent = epicLabel(epic);
    const sprEl = card.querySelector(".asset-spread");
    if (sprEl) sprEl.textContent = Number(spr || 0).toFixed(2);
    const zEl = card.querySelector(".z-val");
    if (zEl) zEl.textContent = Number(sp.z_score || 0).toFixed(2);
    const thEl = card.querySelector(".th-val");
    if (thEl) thEl.textContent = Number(sp.throttle || 0).toFixed(2);
    renderMarketStatusPill(card, epic, marketStates);
    drawSparkline(sparklineCanvases.get(epic), epic);
  }

  container.querySelectorAll(".asset-card").forEach((card) => {
    if (!sorted.includes(card.dataset.epic)) card.remove();
  });
}

function resolveScalpingTelemetry(payload) {
  const direct = payload.scalping_telemetry;
  if (direct && typeof direct === "object") {
    return {
      time_decay: direct.time_decay || {},
      tick_velocity: direct.tick_velocity || {},
      engine_state: direct.engine_state || "ACTIVE",
    };
  }

  const pmap = payload.position_map;
  const rows =
    pmap && typeof pmap === "object"
      ? Object.values(pmap)
      : Array.isArray(payload.positions)
        ? payload.positions
        : [];

  let maxStallSec = 0;
  for (const row of rows) {
    const mins = Number(row.open_mins ?? row.time_open_mins ?? 0);
    const sec = mins > 0 ? mins * 60 : 0;
    if (sec > maxStallSec) maxStallSec = sec;
  }

  const compressSteps =
    maxStallSec >= 45 ? Math.floor((maxStallSec - 45) / 10) + 1 : 0;
  const compressPct = Math.min(75, compressSteps * 15);

  const microConf = Number(payload.micro_confidence || 0);
  const ticks200 = Number(payload.orderbook_ticks_200ms || payload.tick_burst_count || 0);
  const overrideActive = ticks200 >= 15 || microConf >= 90;

  return {
    time_decay: {
      active: maxStallSec >= 45,
      stall_seconds: Math.round(maxStallSec),
      atr_compress_pct: compressPct,
      interval_sec: 10,
      step_pct: 15,
    },
    tick_velocity: {
      ticks_200ms: ticks200,
      override_active: overrideActive,
      confidence_pct: microConf,
      threshold_ticks: 15,
      window_ms: 200,
    },
    engine_state: rows.length > 0 ? "ENGAGED" : "STANDBY",
  };
}

function renderScalpingTelemetry(payload) {
  const st = resolveScalpingTelemetry(payload);
  const td = st.time_decay || {};
  const tv = st.tick_velocity || {};

  const engineEl = $("scalping-engine-state");
  if (engineEl) {
    engineEl.textContent = st.engine_state || "STANDBY";
    engineEl.className = `scalping-state ${st.engine_state === "ENGAGED" ? "engaged" : ""}`;
  }

  const tdStatus = $("time-decay-status");
  const tdDetail = $("time-decay-detail");
  if (tdStatus) {
    tdStatus.textContent = td.active ? "COMPRESSING" : "NOMINAL";
    tdStatus.className = `scalping-value ${td.active ? "amber" : "green"}`;
  }
  if (tdDetail) {
    tdDetail.textContent = `ATR −${Number(td.atr_compress_pct || 0)}% every ${td.interval_sec || 10}s · stall ${Number(td.stall_seconds || 0)}s`;
  }

  const tvStatus = $("tick-velocity-status");
  const tvDetail = $("tick-velocity-detail");
  if (tvStatus) {
    tvStatus.textContent = tv.override_active ? "90% OVERRIDE" : "NORMAL";
    tvStatus.className = `scalping-value ${tv.override_active ? "green" : ""}`;
  }
  if (tvDetail) {
    tvDetail.textContent = `${Number(tv.ticks_200ms || 0)} ticks / ${tv.window_ms || 200}ms · override ${tv.override_active ? "ON" : "OFF"}`;
  }
}

function renderShadowTrading(payload) {
  const st = payload.shadow_trading || {};
  const mode = String(st.mode || "OFF").toUpperCase();
  const badge = $("shadow-mode-badge");
  if (badge) {
    badge.textContent = mode;
    badge.className = `shadow-mode-badge ${mode === "SHADOW" ? "active" : ""}`;
  }
  const unreal = $("shadow-unrealized");
  const real = $("shadow-realized");
  const total = $("shadow-total");
  const openCount = $("shadow-open-count");
  if (unreal) unreal.textContent = fmtMoney(st.unrealized_gbp || 0);
  if (real) real.textContent = fmtMoney(st.realized_gbp || 0);
  if (total) total.textContent = fmtMoney(st.total_gbp || 0);
  if (openCount) openCount.textContent = String(st.open_count || 0);
}

function renderMission(payload) {
  const mission = payload.target_mission || {};
  const pct = Number(mission.mission_progress_pct || 0);
  const pDay = Number(mission.p_day_gbp || 0);
  const target = Number(mission.target_daily_gbp || 1000);
  const factor = Number(mission.risk_compression_factor || 1);
  const preservation = Boolean(mission.capital_preservation);

  $("micro-regime").textContent = `REGIME: ${payload.micro_regime || "NEUTRAL"}`;
  const sessionAp = payload.session_autopilot || {};
  const floorPct = sessionAp.session_micro_floor_pct;
  const rating = Math.round(Number(payload.autopilot_rating || 0));
  const ratingEl = $("autopilot-rating");
  if (ratingEl) {
    if (floorPct > 0 && sessionAp.tokyo_momentum_active) {
      ratingEl.textContent = `${rating}% (floor ${Math.round(floorPct)}%)`;
    } else if (floorPct > 0) {
      ratingEl.textContent = `${rating}% · floor ${Math.round(floorPct)}%`;
    } else {
      ratingEl.textContent = `${rating}%`;
    }
    if (sessionAp.overnight_size_multiplier != null && sessionAp.overnight_size_multiplier < 1) {
      ratingEl.textContent += ` · ${sessionAp.overnight_size_multiplier}x size`;
    }
  }
  $("risk-factor").textContent = factor.toFixed(2);

  $("mission-label").textContent = `Daily Mission Progress: ${pct.toFixed(0)}%`;
  $("mission-amount").textContent = `${fmtMoney(pDay)} / ${fmtMoney(target)}`;

  const bar = $("mission-bar");
  bar.style.width = `${Math.min(100, Math.max(0, pct))}%`;
  bar.classList.toggle("preservation", preservation);

  const banner = $("victory-banner");
  if (mission.mission_accomplished || (preservation && pDay >= target)) {
    banner.textContent = "★ MISSION ACCOMPLISHED: £1,000 TARGET SECURED ★";
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
  }

  renderScalpingTelemetry(payload);
}

function renderSpreadForecast(spread) {
  const el = $("spread-forecast");
  if (!el) return;
  let turbulence = false;
  for (const row of Object.values(spread || {})) {
    if (row && row.turbulence) {
      turbulence = true;
      break;
    }
  }
  if (turbulence) {
    el.textContent = "⚠ TURBULENCE — EXECUTION DEGRADED";
    el.classList.add("alert");
  } else {
    el.textContent = "SPREAD FORECAST: CLEAR";
    el.classList.remove("alert");
  }
}

function fmtPriceForEpic(v, epic) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  const key = String(epic || "").toUpperCase();
  if (key.includes("EUR") || key.includes("GBPUSD") || key.includes("CS.D.EUR")) {
    return Number(v).toFixed(5);
  }
  if (key.includes("GOLD") || key.includes("CFPGOLD")) {
    return Number(v).toFixed(2);
  }
  return Number(v).toFixed(5);
}

function renderPositions(payload) {
  const body = $("pos-body");
  const empty = $("pos-empty");
  if (!body) return;
  body.innerHTML = "";
  const driftByDeal = (payload?.position_drift?.by_deal) || {};
  const pmap = payload?.position_map;
  const rows =
    pmap && typeof pmap === "object"
      ? Object.values(pmap)
      : Array.isArray(payload?.positions)
        ? payload.positions
        : [];
  if (!rows.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  for (const row of rows) {
    const epic = row.epic || "";
    const dealId = row.dealId || row.deal_id || "";
    const drift =
      Boolean(row.drift_detected) ||
      Boolean(driftByDeal[dealId]?.drift_detected);
    const driftPill = drift
      ? `<span class="drift-pill" title="Local cache vs IG broker mismatch">⚠️ DRIFT DETECTED</span>`
      : `<span class="sync-ok">OK</span>`;
    const tr = document.createElement("tr");
    if (drift) tr.classList.add("row-drift");
    tr.innerHTML = `
      <td>${epicLabel(epic)}</td>
      <td>${row.side || "—"}</td>
      <td>${fmtPriceForEpic(row.entry, epic)}</td>
      <td>${fmtPriceForEpic(row.market || row.mkt || row.current, epic)}</td>
      <td>${fmtPriceForEpic(row.stop, epic)}</td>
      <td>${Number(row.trail_pts || 0).toFixed(1)}</td>
      <td>${driftPill}</td>
    `;
    body.appendChild(tr);
  }
}

function renderAccountBadge(payload) {
  const el = $("account-badge");
  if (!el) return;
  const acct = String(payload.ig_account_id || "").trim();
  el.textContent = acct ? `ACCT ${acct}` : "ACCT —";
}

function renderPayload(payload) {
  if (!payload || typeof payload !== "object") return;
  renderMasterVitalsBanner(payload);
  renderGates(payload.gates);
  renderAssets(payload.spread, payload.epics, payload.market_states_map);
  renderSpreadForecast(payload.spread);
  renderMission(payload);
  renderShadowTrading(payload);
  renderPositions(payload);
  renderAccountBadge(payload);
}

function classifyLogLine(line) {
  const u = String(line).toUpperCase();
  if (
    /FAIL|FAULT|ERROR|CRITICAL|PURGE|FREEZE|EMERGENCY|ABORT|FATAL/.test(u)
  ) {
    return "log-red";
  }
  if (/WARN|ALERT|STALE|WAIT|HYDRAT|GRACE|THROTTLE|CAUTION/.test(u)) {
    return "log-amber";
  }
  if (
    /ALLOWED|READY|COMPLETE|ACTIVE|ENGAGED|MISSION|GATE5|LIVE|TICK/.test(u)
  ) {
    return "log-green";
  }
  return "log-dim";
}

function renderTriageReconnectFallback() {
  const container = $("triage-log");
  if (!container) return;
  container.innerHTML = `
    <div class="triage-reconnect-fallback" role="status" aria-live="polite">
      <span class="triage-reconnect-icon" aria-hidden="true">📡</span>
      <p class="triage-reconnect-text">Reconnecting to Feed... Hydrating 24Hr Triage Ledger</p>
    </div>
  `;
}

function appendTriageEvents(events) {
  const container = $("triage-log");
  if (!container) return;
  if (!Array.isArray(events) || events.length === 0) {
    renderTriageReconnectFallback();
    return;
  }
  for (const ev of events) {
    const key = `${ev.iso || ev.ts}-${ev.event_type}`;
    if (triageBuffer.some((x) => x._key === key)) continue;
    triageBuffer.push({ ...ev, _key: key });
  }
  while (triageBuffer.length > TRIAGE_MAX_LINES) triageBuffer.shift();

  container.innerHTML = "";
  for (const ev of triageBuffer) {
    const div = document.createElement("div");
    const cls = classifyTriageEvent(ev.event_type);
    div.className = `log-line ${cls}`;
    const detail = ev.detail ? ` — ${ev.detail}` : "";
    div.textContent = `[${ev.iso || ev.ts}] ${ev.event_type}${detail}`;
    container.appendChild(div);
  }
  container.scrollTop = container.scrollHeight;
}

function classifyTriageEvent(eventType) {
  const u = String(eventType || "").toUpperCase();
  if (/FREEZE|FAIL|ERROR|BREACH|DRIFT/.test(u)) return "log-red";
  if (/STALL|WARN|PORT_FLUSH/.test(u)) return "log-amber";
  return "log-green";
}

function bindLogTabs() {
  const tabLog = $("tab-flight-log");
  const tabTriage = $("tab-triage");
  const panelLog = $("panel-flight-log");
  const panelTriage = $("panel-triage");
  if (!tabLog || !tabTriage) return;

  function activate(which) {
    const isLog = which === "log";
    tabLog.classList.toggle("active", isLog);
    tabTriage.classList.toggle("active", !isLog);
    tabLog.setAttribute("aria-selected", isLog ? "true" : "false");
    tabTriage.setAttribute("aria-selected", !isLog ? "true" : "false");
    if (panelLog) {
      panelLog.classList.toggle("active", isLog);
      panelLog.hidden = !isLog;
    }
    if (panelTriage) {
      panelTriage.classList.toggle("active", !isLog);
      panelTriage.hidden = isLog;
    }
    const counter = $("log-line-count");
    if (counter) {
      counter.textContent = isLog
        ? `${logBuffer.length} lines`
        : `${triageBuffer.length} events`;
    }
  }

  tabLog.addEventListener("click", () => activate("log"));
  tabTriage.addEventListener("click", () => activate("triage"));
}

function appendLogLines(lines) {
  const container = $("flight-log");
  if (!container || !Array.isArray(lines)) return;

  for (const item of lines) {
    const text = typeof item === "string" ? item : item.line || item.text || "";
    if (!text.trim()) continue;
    logBuffer.push(text);
  }
  while (logBuffer.length > LOG_MAX_LINES) logBuffer.shift();

  container.innerHTML = "";
  for (const line of logBuffer) {
    const div = document.createElement("div");
    div.className = `log-line ${classifyLogLine(line)}`;
    div.textContent = line;
    container.appendChild(div);
  }
  container.scrollTop = container.scrollHeight;

  const counter = $("log-line-count");
  if (counter) counter.textContent = `${logBuffer.length} lines`;
}

function updateClock() {
  const el = $("clock");
  if (!el) return;
  const now = new Date();
  el.textContent =
    now.toLocaleTimeString("en-GB", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      timeZone: "Europe/London",
    }) + " BST";
}

function setPill(el, state, labels) {
  if (!el) return;
  el.className = "pill";
  if (state === "live") {
    el.classList.add("pill-live");
    el.textContent = labels.live;
  } else if (state === "dead") {
    el.classList.add("pill-dead");
    el.textContent = labels.dead;
  } else {
    el.classList.add("pill-warn");
    el.textContent = labels.connecting;
  }
}

function connectTelemetryWebSocket() {
  const url = wsUrl("/ws/telemetry");
  let ws;
  let reconnectTimer;
  const pill = $("ws-status");

  function connect() {
    setPill(pill, "connecting", {
      live: "TELEMETRY LIVE",
      dead: "TELEMETRY DOWN",
      connecting: "TELEMETRY …",
    });
    ws = new WebSocket(url);

    ws.onopen = () =>
      setPill(pill, "live", {
        live: "TELEMETRY LIVE",
        dead: "TELEMETRY DOWN",
        connecting: "TELEMETRY …",
      });

    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data && data.type === "SYSTEM_HOT_RELOAD") {
          window.location.href = `${location.pathname}?v=${Date.now()}`;
          return;
        }
        renderPayload(data);
      } catch (_) {
        /* ignore malformed frame */
      }
    };

    ws.onclose = () => {
      setPill(pill, "dead", {
        live: "TELEMETRY LIVE",
        dead: "TELEMETRY DOWN",
        connecting: "TELEMETRY …",
      });
      reconnectTimer = setTimeout(connect, 2000);
    };

    ws.onerror = () => ws.close();
  }

  connect();
  return () => {
    clearTimeout(reconnectTimer);
    if (ws) ws.close();
  };
}

function connectLogsWebSocket() {
  const url = wsUrl("/ws/logs");
  let ws;
  let reconnectTimer;
  const pill = $("log-ws-status");

  function connect() {
    setPill(pill, "connecting", {
      live: "LOGS LIVE",
      dead: "LOGS DOWN",
      connecting: "LOGS …",
    });
    ws = new WebSocket(url);

    ws.onopen = () =>
      setPill(pill, "live", {
        live: "LOGS LIVE",
        dead: "LOGS DOWN",
        connecting: "LOGS …",
      });

    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data && data.type === "LOG_FRAME" && Array.isArray(data.lines)) {
          appendLogLines(data.lines);
        } else if (Array.isArray(data)) {
          appendLogLines(data);
        } else if (data && data.line) {
          appendLogLines([data]);
        }
      } catch (_) {
        appendLogLines([String(ev.data)]);
      }
    };

    ws.onclose = () => {
      setPill(pill, "dead", {
        live: "LOGS LIVE",
        dead: "LOGS DOWN",
        connecting: "LOGS …",
      });
      reconnectTimer = setTimeout(connect, 2500);
    };

    ws.onerror = () => ws.close();
  }

  connect();
  return () => {
    clearTimeout(reconnectTimer);
    if (ws) ws.close();
  };
}

function connectTriageWebSocket() {
  const url = wsUrl("/ws/triage");
  let ws;
  let reconnectTimer;
  const pill = $("triage-ws-status");

  function connect() {
    setPill(pill, "connecting", {
      live: "TRIAGE LIVE",
      dead: "TRIAGE DOWN",
      connecting: "TRIAGE …",
    });
    ws = new WebSocket(url);

    ws.onopen = () =>
      setPill(pill, "live", {
        live: "TRIAGE LIVE",
        dead: "TRIAGE DOWN",
        connecting: "TRIAGE …",
      });

    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data && data.type === "TRIAGE_FRAME" && Array.isArray(data.events)) {
          appendTriageEvents(data.events);
        }
      } catch (_) {
        /* ignore */
      }
    };

    ws.onclose = () => {
      setPill(pill, "dead", {
        live: "TRIAGE LIVE",
        dead: "TRIAGE DOWN",
        connecting: "TRIAGE …",
      });
      renderTriageReconnectFallback();
      reconnectTimer = setTimeout(connect, 3000);
    };

    ws.onerror = () => ws.close();
  }

  connect();
  return () => {
    clearTimeout(reconnectTimer);
    if (ws) ws.close();
  };
}

async function bindEmergency() {
  const btn = $("emergency-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = "OVERRIDE SENT — FLATTEN IN PROGRESS";
    try {
      await fetch(`/api/emergency?v=${BUILD_TS}`, { method: "POST" });
    } catch (_) {
      btn.textContent = "REQUEST FAILED — RETRY FROM DASHBOARD";
      btn.disabled = false;
    }
  });
}

function boot() {
  updateClock();
  setInterval(updateClock, 1000);
  bindLogTabs();
  connectTelemetryWebSocket();
  connectLogsWebSocket();
  connectTriageWebSocket();
  bindEmergency();
  renderTriageReconnectFallback();
  appendLogLines([
    `[Flight Deck] Superjet HUD build ${BUILD_TS} — sparklines + triage online`,
    "[Flight Deck] Awaiting engine avionics stream on /ws/logs …",
  ]);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
