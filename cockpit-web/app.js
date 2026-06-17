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

/** Card A sub-labels when gates reach COMPLETE (avoids stale backend detail strings). */
const GATE_DETAIL_WHEN_COMPLETE = {
  G2: "authenticated & session bound",
  G3: "streaming live broker feed",
};

const ASSET_NAMES = {
  "CS.D.CFPGOLD.CFP.IP": "Gold",
  "IX.D.DOW.IFM.IP": "Wall Street",
  "IX.D.NIKKEI.IFM.IP": "Japan 225",
  "CS.D.EURUSD.CFD.IP": "EUR/USD",
};

/** Localized avionics dictionary keys — one isolated metrics bucket per card. */
const EPIC_ASSET_KEYS = {
  "CS.D.CFPGOLD.CFP.IP": "GOLD",
  "IX.D.DOW.IFM.IP": "WALL_STREET",
  "IX.D.NIKKEI.IFM.IP": "JAPAN_225",
  "CS.D.EURUSD.CFD.IP": "EUR_USD",
};

const ASSET_CARD_ORDER = ["GOLD", "WALL_STREET", "JAPAN_225", "EUR_USD"];

const LOG_MAX_LINES = 120;
const TRIAGE_MAX_LINES = 100;
const SPARKLINE_LOOKBACK = 50;
const STALE_FEED_SEC = 5.0;
const PRODUCTION_CONFIDENCE_FLOOR = 62;
const RSI_OVERBOUGHT_CEILING = 85;

/** Root README.md — embedded verbatim for offline Flight Deck blueprint manifest. */
const ROOT_README_MD = `# IG Agent v29.1

Automated IG CFD trading agent — Python backend (FastAPI + multi-market trading loop) on localhost:8080, React dashboard in dashboard/.

**Authoritative docs:**

| Document | Purpose |
|----------|---------|
| IG_Agent_v29.1_COMPLETE_SPEC.md | Full operator + implementer specification |
| docs/V29.1_ARCHITECTURE.md | Module map, data flow, diagrams |
| IG_Agent_v25_COMPLETE_SPEC_v8.md | Historical v25.5 reference |
| IG_Agent_v26_FRAMEWORK.md | Future multi-strategy vision |

## Running (single command)

\`\`\`bash
# From repo root — trading + dashboard
PYTHONPATH=src python3 src/main.py

# Browser
open http://localhost:8080

# Rebuild dashboard after UI changes
cd dashboard && npm run build
\`\`\`

**macOS:** use Desktop launcher IG Agent v29.0.app (runs same entry point).

## Configuration

- **Primary overlay:** config/config_v29.json (v29.1 protective learning, demo mode)
- **Instrument matrix:** config/config_v25.json
- Credentials: interactive at startup (not persisted to disk)

## Quick health checks

\`\`\`bash
PYTHONPATH=src python3 scripts/learning_health_report.py
PYTHONPATH=src python3 -m pytest tests/ -q
\`\`\`

Restart the agent after Python or config changes.`;

const FIVE_GATE_BOOT_SPEC = `## 5-Gate Boot Pipeline (Production)

Source: src/system/boot_coordinator.py · gate1_runner → gate5_runner

| Gate | Phase | Pass criteria (summary) |
|------|-------|-------------------------|
| G1 | Environment & Preflight | Config load, demo guard, credentials path, API bind readiness (<2s) |
| G2 | REST Authentication | IG session, account bind, rest_client committed to BootContext |
| G3 | Streaming & Hub | Market data hub online (rest_poll or Lightstreamer per config) |
| G4 | State Hydration | OHLC bootstrap, orchestrator build, dormant trading loops, position sync |
| G5 | ACTIVE / READY | Atomic READY flip, unpause loops, post-ready services, deploy verify |

Card A telemetry maps G1–G4 for cockpit visibility; G5 marks the agent ACTIVE for live tick processing.`;

const PRODUCTION_PARAMETERS_SPEC = `## Production Parameters (v29.1)

Config overlay: config/config_v29.json → config/config_v25.json

### Deployment mode
  operating_mode:         DEMO
  demo_only_deployment:   true
  allow_live_trading:     false
  Profile:                B (learning_demo_mode)

### Protective learning (strict production)
  USE_TEMPORARY_TEST_GATE:  false
  signal_threshold_floor:   62% confidence minimum
  fitness_min_floor:        55
  RSI overbought ceiling:   85
  Circuit breaker:          5 consecutive losses → block new entries

### Daily risk envelope
  Soft pause:               £400 realised (learning_demo_mode)
  Hard stop:                £2,000 effective daily loss cap
  REST budget:              3 calls/min hard cap
  Quote freshness:          Hub snapshot · ~45s max tick age
  Order in-flight timeout:  30s

### Entry gate stack (per-tick, seven gates)
  1. session_open          2. cold_start_gap       3. environment_fitness
  4. points_state          5. risk_validation      6. signal_confidence (≥62%)
  7. execution             correlation · protect · no in-flight deadlock

### Enabled markets (typical)
  IX.D.NIKKEI.IFM.IP · IX.D.DOW.IFM.IP · IX.D.NASDAQ.IFM.IP
  CS.D.CFPGOLD.CFP.IP · CS.D.EURUSD.CFD.IP · CS.D.GBPUSD.CFD.IP`;

const BLUEPRINT_README_FETCH_PATH = "/static/README.md";

const logBuffer = [];
const triageBuffer = [];
let lastTriageGeneration = null;
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

function fmtSignedMoney(n) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  const v = Number(n);
  const formatted = fmtMoney(Math.abs(v));
  if (v > 0) return `+${formatted}`;
  if (v < 0) return `-${formatted}`;
  return formatted;
}

function resolvePositionSide(row) {
  if (!row || typeof row !== "object") return "—";
  if (row.signed_size != null && Number(row.signed_size) < 0) return "SELL";
  const size = Number(row.size ?? row.dealSize ?? 0);
  if (size < 0) return "SELL";
  const side = String(row.side || row.direction || "").trim().toUpperCase();
  if (side === "SELL" || side === "SHORT") return "SELL";
  if (size > 0 || side === "BUY") return "BUY";
  return side || "—";
}

function resolveSignedSize(row) {
  if (!row || typeof row !== "object") return 0;
  if (row.signed_size != null && !Number.isNaN(Number(row.signed_size))) {
    return Number(row.signed_size);
  }
  const size = Number(row.size ?? row.dealSize ?? 0);
  const side = resolvePositionSide(row);
  if (side === "SELL") {
    return size < 0 ? size : -Math.abs(size);
  }
  return Math.abs(size);
}

function computeFloatingPnl(row) {
  if (!row || typeof row !== "object") return null;
  const broker =
    row.floating_pnl_gbp ??
    row.profitAndLoss ??
    row.pnl_gbp ??
    row.pnl_currency ??
    row.upl;
  if (broker != null && !Number.isNaN(Number(broker))) {
    return Number(broker);
  }
  const entry = Number(row.entry ?? row.level ?? 0);
  const latest = Number(
    row.current ?? row.market ?? row.mkt ?? row.broker_mark ?? 0
  );
  const signedSize = resolveSignedSize(row);
  if (!entry || !latest || !signedSize) return null;
  return (entry - latest) * signedSize;
}

function epicLabel(epic) {
  return ASSET_NAMES[epic] || String(epic).slice(-18);
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatBlueprintDocument(raw) {
  const lines = String(raw || "").split("\n");
  const out = [];
  let inCode = false;
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      inCode = !inCode;
      if (inCode) {
        out.push('<span class="spec-code">');
      } else {
        out.push("</span>");
      }
      continue;
    }
    if (inCode) {
      out.push(escapeHtml(line));
      continue;
    }
    if (line.startsWith("### ")) {
      out.push(`<span class="spec-h3">${escapeHtml(line.slice(4))}</span>`);
      continue;
    }
    if (line.startsWith("## ")) {
      out.push(`<span class="spec-h2">${escapeHtml(line.slice(3))}</span>`);
      continue;
    }
    if (line.startsWith("# ")) {
      out.push(`<span class="spec-h1">${escapeHtml(line.slice(2))}</span>`);
      continue;
    }
    if (line.startsWith("|") || line.startsWith("**Authoritative")) {
      out.push(`<span class="spec-meta">${escapeHtml(line)}</span>`);
      continue;
    }
    if (/^\s{2}[A-Za-z_]+:/.test(line)) {
      out.push(`<span class="spec-kv">${escapeHtml(line)}</span>`);
      continue;
    }
    out.push(escapeHtml(line));
  }
  return out.join("\n");
}

function buildBlueprintManifestText(readmeBody) {
  return [
    String(readmeBody || ROOT_README_MD).trim(),
    "",
    "════════════════════════════════════════════════════════════════════════",
    FIVE_GATE_BOOT_SPEC.trim(),
    "",
    PRODUCTION_PARAMETERS_SPEC.trim(),
  ].join("\n");
}

function buildBlueprintManifestLayout(content, sourceLabel) {
  const el = $("blueprint-manifest-content");
  const label = $("blueprint-source-label");
  if (label) {
    label.textContent = sourceLabel
      ? `Source: ${sourceLabel}`
      : "Source: README.md · embedded 5-Gate · production parameters";
  }
  if (!el) return;
  el.innerHTML = formatBlueprintDocument(content);
}

async function loadBlueprintManifestContent() {
  try {
    const res = await fetch(`${BLUEPRINT_README_FETCH_PATH}?v=${BUILD_TS}`);
    if (res.ok) {
      const text = await res.text();
      if (text && text.trim()) {
        return {
          content: buildBlueprintManifestText(text),
          source: "README.md (cockpit-web/static)",
        };
      }
    }
  } catch (_) {
    /* use embedded root README snapshot */
  }
  return {
    content: buildBlueprintManifestText(ROOT_README_MD),
    source: "embedded README.md + 5-Gate boot spec + production parameters",
  };
}

let blueprintManifestLoaded = false;

async function ensureBlueprintManifestRendered() {
  if (blueprintManifestLoaded) return;
  const el = $("blueprint-manifest-content");
  if (!el) return;
  el.textContent = "Loading blueprint manifest…";
  const { content, source } = await loadBlueprintManifestContent();
  buildBlueprintManifestLayout(content, source);
  blueprintManifestLoaded = true;
}

function bindBlueprintToggle() {
  const btn = $("blueprint-toggle");
  const panel = $("system-blueprint-panel");
  if (!btn || !panel) return;
  btn.addEventListener("click", async () => {
    const opening = panel.classList.contains("hidden");
    if (opening) {
      await ensureBlueprintManifestRendered();
      panel.classList.remove("hidden");
      panel.hidden = false;
      btn.classList.add("active");
      btn.setAttribute("aria-expanded", "true");
      panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } else {
      panel.classList.add("hidden");
      panel.hidden = true;
      btn.classList.remove("active");
      btn.setAttribute("aria-expanded", "false");
    }
  });
}

function gateClass(status) {
  const s = String(status || "pending").toLowerCase();
  if (s === "complete" || s === "running") return "complete";
  if (s === "failed") return "failed";
  return "pending";
}

function resolveGateDetail(gid, status, rawDetail) {
  const s = String(status || "pending").toLowerCase();
  const detail = String(rawDetail || "").trim();
  if (s === "complete" || s === "running") {
    const mapped = GATE_DETAIL_WHEN_COMPLETE[gid];
    if (mapped) return mapped;
  }
  if (gid === "G2" && s === "complete" && /authenticat/i.test(detail)) {
    return GATE_DETAIL_WHEN_COMPLETE.G2;
  }
  return detail;
}

function renderGates(gates) {
  const grid = $("gate-grid");
  if (!grid) return;
  grid.innerHTML = "";
  for (const gid of GATE_ORDER) {
    const label = GATE_LABELS[gid];
    const g = (gates && gates[gid]) || {};
    const status = String(g.status || "pending").toUpperCase();
    const detail = resolveGateDetail(gid, g.status, g.detail);
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

function readLocalizedAssetBucket(payload, assetKey) {
  if (!payload || !assetKey) return null;
  const buckets = [
    payload.avionics_hud,
    payload.avionics_assets,
    payload.avionics_hud,
    payload.hud_assets,
    payload.asset_telemetry,
    payload.hud_markets,
  ];
  for (const bucket of buckets) {
    if (bucket && typeof bucket === "object" && bucket[assetKey]) {
      const row = bucket[assetKey];
      return row && typeof row === "object" ? row : null;
    }
  }
  return null;
}

/** Per-instrument market slice — explicit avionics_hud / avionics_assets only. */
function readAssetMarketSlice(payload, assetKey, epic) {
  if (!assetKey) return {};
  const hud = payload?.avionics_hud;
  if (hud && typeof hud === "object" && hud[assetKey]) {
    const row = hud[assetKey];
    return row && typeof row === "object" ? row : {};
  }
  const top = payload?.[assetKey];
  if (top && typeof top === "object" && (top.confidence != null || top.rsi != null)) {
    return top;
  }
  const avionics = payload?.avionics_assets;
  if (avionics && typeof avionics === "object" && avionics[assetKey]) {
    const row = avionics[assetKey];
    return row && typeof row === "object" ? row : {};
  }
  const markets = payload?.markets;
  if (markets && typeof markets === "object") {
    const byAsset = markets[assetKey];
    if (byAsset && typeof byAsset === "object") return byAsset;
    if (epic && markets[epic] && typeof markets[epic] === "object") {
      return markets[epic];
    }
  }
  const localized = readLocalizedAssetBucket(payload, assetKey);
  return localized && typeof localized === "object" ? localized : {};
}

function getAssetCardEl(assetKey) {
  if (!assetKey) return null;
  return document.getElementById(`asset-card-${assetKey}`);
}

function findPositionRowForEpic(payload, epic) {
  const pmap = payload?.position_map;
  if (!pmap || typeof pmap !== "object") return null;
  for (const row of Object.values(pmap)) {
    if (row && String(row.epic || "") === epic) return row;
  }
  return null;
}

function shortenBlockerReason(text) {
  const raw = String(text || "").trim();
  if (!raw) return "";
  return raw.length > 72 ? `${raw.slice(0, 69)}…` : raw;
}

function resolveStrategyBlocker(marketSlice) {
  const slice = marketSlice && typeof marketSlice === "object" ? marketSlice : {};
  const signal =
    slice.signal && typeof slice.signal === "object" ? slice.signal : {};
  const setup = String(signal.setup || slice.setup || "").trim().toUpperCase();
  const direction = String(signal.direction || slice.direction || "WAIT")
    .trim()
    .toUpperCase();
  const blockReason = shortenBlockerReason(
    signal.block_reason || slice.block_reason || slice.blocker || ""
  );
  const eligibility =
    slice.trade_eligibility ||
    signal.countdown ||
    slice.countdown ||
    null;

  if (blockReason) {
    const head = setup ? `G5: ${direction || "WAIT"}` : "G5";
    return `${head} — ${blockReason}`;
  }
  if (eligibility && typeof eligibility === "object") {
    if (eligibility.display) return String(eligibility.display);
    if (eligibility.label) return String(eligibility.label);
  }
  const health = slice.health && typeof slice.health === "object" ? slice.health : {};
  const gates = Array.isArray(health.gates) ? health.gates : [];
  const failed = gates.find((g) => g && g.pass === false);
  if (failed) {
    const gateName = String(failed.name || "gate").replace(/_/g, " ").toUpperCase();
    const detail = shortenBlockerReason(failed.detail || "blocked");
    return `${gateName}: ${detail}`;
  }
  if (setup && direction === "WAIT") {
    return `G5: ${setup} — Awaiting candle close`;
  }
  if (direction && direction !== "WAIT") {
    return `G5: ${direction} — monitoring`;
  }
  return "G5: CLEAR";
}

function resolveAiWeightLabel(marketSlice) {
  const slice = marketSlice && typeof marketSlice === "object" ? marketSlice : {};
  const signal =
    slice.signal && typeof slice.signal === "object" ? slice.signal : {};
  const points = slice.points && typeof slice.points === "object" ? slice.points : {};

  let mult =
    slice.ai_weight ??
    slice.ml_sizing_multiplier ??
    slice.ml_weight ??
    slice.learning_weight ??
    signal.ml_sizing_multiplier ??
    signal.ai_weight;

  if (mult == null) {
    const gates = Array.isArray(slice.health?.gates)
      ? slice.health.gates
      : Array.isArray(slice.gates)
        ? slice.gates
        : [];
    for (const g of gates) {
      if (!g || typeof g !== "object") continue;
      const val = g.value;
      if (g.name === "risk_validation" && val && typeof val === "object") {
        mult = val.ml_sizing_multiplier ?? mult;
      }
      if (g.name === "ml_veto" && val && typeof val === "object") {
        mult = val.sizing_multiplier ?? mult;
      }
    }
  }
  if (mult == null && points.size_multiplier != null) {
    mult = points.size_multiplier;
  }

  const weight = Number(mult ?? 1.0);
  const safe = Number.isFinite(weight) ? weight : 1.0;
  const delta = safe - 1.0;
  if (Math.abs(delta) < 0.005) return "NOMINAL 1.00x";
  const sign = delta >= 0 ? "+" : "";
  return `ADAPTIVE ${sign}${delta.toFixed(2)}x`;
}

function resolveAvionicsMetrics(assetKey, epic, payload) {
  const localized = readLocalizedAssetBucket(payload, assetKey) || {};
  const marketSlice = readAssetMarketSlice(payload, assetKey, epic);
  const signal =
    marketSlice.signal && typeof marketSlice.signal === "object"
      ? marketSlice.signal
      : localized.signal && typeof localized.signal === "object"
        ? localized.signal
        : {};
  const posRow = findPositionRowForEpic(payload, epic);

  const snap =
    signal.snapshot && typeof signal.snapshot === "object"
      ? signal.snapshot
      : marketSlice.snapshot && typeof marketSlice.snapshot === "object"
        ? marketSlice.snapshot
        : {};
  const lastBar =
    marketSlice.last && typeof marketSlice.last === "object"
      ? marketSlice.last
      : snap.last && typeof snap.last === "object"
        ? snap.last
        : {};

  let confidence =
    localized.confidence ??
    localized.signal_confidence ??
    marketSlice.confidence ??
    marketSlice.signal_confidence ??
    signal.confidence ??
    signal.signal_confidence ??
    signal.adjusted_confidence;
  if (
    confidence == null &&
    posRow &&
    String(posRow.epic || "") === String(epic || "")
  ) {
    confidence = posRow.confidence ?? posRow.signal_confidence;
  }

  let rsi =
    localized.rsi ??
    marketSlice.rsi ??
    signal.rsi ??
    snap.rsi ??
    lastBar.rsi ??
    (marketSlice.indicators && marketSlice.indicators.rsi);
  if (
    rsi == null &&
    posRow &&
    String(posRow.epic || "") === String(epic || "") &&
    posRow.rsi != null
  ) {
    rsi = posRow.rsi;
  }

  const hasRsi = rsi != null && !Number.isNaN(Number(rsi));
  const hasConf = confidence != null && !Number.isNaN(Number(confidence));

  return {
    assetKey,
    epic,
    marketSlice,
    confidence: hasConf
      ? Math.max(0, Math.min(100, Number(confidence)))
      : null,
    rsi: hasRsi ? Math.max(0, Math.min(100, Number(rsi))) : null,
    blocker: resolveStrategyBlocker(marketSlice),
    aiWeight: resolveAiWeightLabel(marketSlice),
  };
}

function renderEpicGauges(assetKey, metrics) {
  const card = getAssetCardEl(assetKey);
  if (!card || !metrics) return;
  const conf = metrics.confidence;
  const rsi = metrics.rsi;
  const confPct =
    conf == null || Number.isNaN(Number(conf))
      ? null
      : Math.max(0, Math.min(100, Number(conf)));
  const rsiPct =
    rsi == null || Number.isNaN(Number(rsi))
      ? null
      : Math.max(0, Math.min(100, Number(rsi)));

  const confVal = card.querySelector(".conf-val");
  if (confVal) {
    confVal.textContent =
      confPct == null ? "—" : `${confPct.toFixed(1)}%`;
    confVal.classList.toggle(
      "conf-above",
      confPct != null && confPct >= PRODUCTION_CONFIDENCE_FLOOR
    );
    confVal.classList.toggle(
      "conf-below",
      confPct != null && confPct < PRODUCTION_CONFIDENCE_FLOOR
    );
  }

  const confFill = card.querySelector(".conf-gauge-fill");
  if (confFill) confFill.style.width = confPct == null ? "0%" : `${confPct}%`;

  const confThreshold = card.querySelector(".conf-gauge-threshold");
  if (confThreshold) {
    confThreshold.style.left = `${PRODUCTION_CONFIDENCE_FLOOR}%`;
  }

  const rsiVal = card.querySelector(".rsi-val");
  if (rsiVal) {
    rsiVal.textContent = rsiPct == null ? "—" : rsiPct.toFixed(1);
    rsiVal.classList.toggle(
      "rsi-hot",
      rsiPct != null && rsiPct > RSI_OVERBOUGHT_CEILING
    );
  }

  const rsiTrack = card.querySelector(".rsi-heat-track");
  if (rsiTrack) {
    rsiTrack.classList.toggle(
      "rsi-overbought",
      rsiPct != null && rsiPct > RSI_OVERBOUGHT_CEILING
    );
  }

  const rsiMarker = card.querySelector(".rsi-heat-marker");
  if (rsiMarker) {
    rsiMarker.style.left = rsiPct == null ? "0%" : `${rsiPct}%`;
  }

  const blockerEl = card.querySelector(".blocker-val");
  if (blockerEl) {
    const blocker = String(metrics.blocker || "G5: CLEAR");
    blockerEl.textContent = blocker;
    blockerEl.title = blocker;
    blockerEl.classList.toggle("blocker-hot", /RSI|OVERBOUGHT|BLOCK|FAIL/i.test(blocker));
    blockerEl.classList.toggle("blocker-wait", /WAIT|AWAIT|CANDLE/i.test(blocker));
  }

  const aiWeightEl = card.querySelector(".ai-weight-val");
  if (aiWeightEl) {
    const label = String(metrics.aiWeight || "NOMINAL 1.00x");
    aiWeightEl.textContent = label;
    aiWeightEl.classList.toggle("ai-adaptive", label.startsWith("ADAPTIVE"));
    aiWeightEl.classList.toggle("ai-nominal", label.startsWith("NOMINAL"));
  }
}

function renderAssets(payload) {
  const spread = payload?.spread;
  const epics = payload?.epics;
  const marketStates = payload?.market_states_map;
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
    const assetKey = EPIC_ASSET_KEYS[epic] || epic;
    const sp = (spread && spread[epic]) || {};
    const q = (epics && epics[epic]) || {};
    const spr = sp.spread != null ? sp.spread : q.spread;
    const bid = Number(q.bid || 0);
    const offer = Number(q.offer || 0);
    const mid = bid > 0 && offer > 0 ? (bid + offer) / 2 : 0;
    if (mid > 0) pushSparkline(epic, mid);
    const ageS = Number(q.age_s ?? q.tick_age_s ?? 0);
    const stale = ageS > STALE_FEED_SEC;

    let card = getAssetCardEl(assetKey);
    if (!card) {
      card =
        container.querySelector(`[data-asset-key="${assetKey}"]`) ||
        container.querySelector(`[data-epic="${epic}"]`);
    }
    if (!card) {
      card = document.createElement("div");
      card.className = "asset-card";
      card.id = `asset-card-${assetKey}`;
      card.dataset.assetKey = assetKey;
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
        <div class="asset-gauges" aria-label="Gating metrics">
          <div class="gauge-block">
            <div class="gauge-label-row">
              <span class="gauge-label">CONFIDENCE</span>
              <span class="gauge-value conf-val">0%</span>
            </div>
            <div class="conf-gauge-track" title="Production floor 62%">
              <div class="conf-gauge-fill"></div>
              <div class="conf-gauge-threshold" style="left: 62%"></div>
            </div>
          </div>
          <div class="gauge-block">
            <div class="gauge-label-row">
              <span class="gauge-label">RSI</span>
              <span class="gauge-value rsi-val">50</span>
            </div>
            <div class="rsi-heat-track" title="Overbought block above 85">
              <div class="rsi-heat-marker" style="left: 50%"></div>
            </div>
          </div>
        </div>
        <div class="asset-strategy-meta" aria-label="Strategy blockers and ML weight">
          <div class="strategy-meta-row">
            <span class="strategy-meta-label">BLOCKER</span>
            <span class="strategy-meta-val blocker-val">G5: CLEAR</span>
          </div>
          <div class="strategy-meta-row">
            <span class="strategy-meta-label">AI WEIGHT</span>
            <span class="strategy-meta-val ai-weight-val ai-nominal">NOMINAL 1.00x</span>
          </div>
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
    const metrics = resolveAvionicsMetrics(assetKey, epic, payload);
    renderEpicGauges(assetKey, metrics);
  }

  container.querySelectorAll(".asset-card").forEach((card) => {
    const epic = card.dataset.epic || "";
    if (!sorted.includes(epic)) card.remove();
  });
}

function isCockpitTestMode(payload) {
  if (!payload || typeof payload !== "object") return false;
  const cc = payload.cockpit_controls || payload.shadow_trading?.controls || {};
  return payload.test_mode_active === true || cc.test_mode_unlock === true;
}

function resolveScalpingTelemetry(payload) {
  const testMode = isCockpitTestMode(payload);
  const direct = payload.scalping_telemetry;
  if (direct && typeof direct === "object") {
    const tv = { ...(direct.tick_velocity || {}) };
    if (testMode) {
      tv.override_active = false;
    }
    let engineState = direct.engine_state || (testMode ? "ACTIVE" : "STANDBY");
    if (testMode && String(engineState).toUpperCase() === "STANDBY") {
      engineState = "ACTIVE";
    }
    return {
      time_decay: direct.time_decay || {},
      tick_velocity: tv,
      engine_state: engineState,
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
  const overrideActive = testMode ? false : ticks200 >= 15 || microConf >= 90;

  if (testMode) {
    return {
      time_decay: {
        active: false,
        stall_seconds: 0,
        atr_compress_pct: 0,
        interval_sec: 10,
        step_pct: 15,
      },
      tick_velocity: {
        ticks_200ms: ticks200,
        override_active: false,
        confidence_pct: microConf,
        threshold_ticks: 15,
        window_ms: 200,
      },
      engine_state: "ACTIVE",
    };
  }

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
  const scalpingPanel = document.querySelector(".scalping-panel");
  if (engineEl) {
    const state = String(st.engine_state || "STANDBY").toUpperCase();
    engineEl.textContent = state;
    const engaged = state === "ENGAGED" || state === "ACTIVE";
    engineEl.className = `scalping-state ${engaged ? "engaged" : "standby"}`;
    if (scalpingPanel) {
      scalpingPanel.classList.toggle("standby", !engaged);
    }
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
  const controls = st.controls || payload.cockpit_controls || {};
  const testMode = isCockpitTestMode(payload);
  const disabled = testMode ? false : controls.disabled === true;
  const mode = String(st.mode || "OFF").toUpperCase();
  const toggle = $("shadow-mode-toggle");
  const switchWrap = $("shadow-mode-switch");
  if (toggle) {
    toggle.checked = mode === "SHADOW";
    toggle.removeAttribute("disabled");
    if (disabled) {
      toggle.disabled = true;
    }
  }
  if (switchWrap) {
    switchWrap.classList.toggle("locked", disabled);
    switchWrap.title = disabled
      ? "Controls locked — manual stop or supervisor hold active"
      : "Toggle simulated shadow fills (IG_AGENT_MODE)";
  }
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

async function bindShadowToggle() {
  const toggle = $("shadow-mode-toggle");
  if (!toggle || toggle.dataset.bound === "1") return;
  toggle.dataset.bound = "1";
  toggle.addEventListener("change", async () => {
    if (toggle.disabled) {
      toggle.checked = !toggle.checked;
      return;
    }
    const wantOn = toggle.checked;
    try {
      const res = await fetch("/api/shadow/toggle", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) {
        toggle.checked = !wantOn;
        return;
      }
      const mode = String(data.mode || (wantOn ? "SHADOW" : "LIVE")).toUpperCase();
      toggle.checked = mode === "SHADOW";
      const badge = $("shadow-mode-badge");
      if (badge) {
        badge.textContent = mode === "SHADOW" ? "SHADOW" : "OFF";
        badge.className = `shadow-mode-badge ${mode === "SHADOW" ? "active" : ""}`;
      }
    } catch (_) {
      toggle.checked = !wantOn;
    }
  });
}

function renderMission(payload) {
  const mission = payload.target_mission || {};
  const pct = Number(mission.mission_progress_pct || 0);
  const pDay = Number(mission.p_day_gbp || 0);
  const target = Number(mission.target_daily_gbp || 1000);
  const factor = Number(mission.risk_compression_factor || 1);
  const preservation = Boolean(mission.capital_preservation);

  const regimeEl = $("micro-regime");
  if (regimeEl) {
    const regime = String(payload.micro_regime || "NEUTRAL").trim().toUpperCase();
    regimeEl.textContent = `REGIME: ${regime}`;
    regimeEl.className = "stat-value";
    if (regime === "NEUTRAL") {
      regimeEl.classList.add("regime-neutral");
    } else if (/BULL|MOMENTUM|UP|HIGH|ACTIVE/i.test(regime)) {
      regimeEl.classList.add("neon-green");
    } else if (/BEAR|DOWN|LOW|BLOCK/i.test(regime)) {
      regimeEl.classList.add("regime-caution");
    } else {
      regimeEl.classList.add("regime-neutral");
    }
  }
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

function sideBadgeClass(side) {
  if (side === "SELL") return "sell";
  if (side === "BUY") return "buy";
  return "neutral";
}

function pnlToneClass(value) {
  if (value == null || Number.isNaN(Number(value))) return "";
  const v = Number(value);
  if (v < 0) return "pnl-loss";
  if (v > 0) return "pnl-profit";
  return "pnl-flat";
}

function resolveOpenPositionRows(payload) {
  const pmap = payload?.position_map;
  if (pmap && typeof pmap === "object") {
    return Object.values(pmap).filter((row) => row && typeof row === "object");
  }
  if (Array.isArray(payload?.positions)) {
    return payload.positions.filter((row) => row && typeof row === "object");
  }
  return [];
}

function resolveClosedTradeRows(payload) {
  const keys = ["closed_trades_24h", "closed_trades", "closed_executions", "transaction_history"];
  for (const key of keys) {
    const raw = payload?.[key];
    if (Array.isArray(raw) && raw.length) {
      return raw.filter((row) => row && typeof row === "object");
    }
  }
  return [];
}

function resolveClosureReason(row) {
  const reason =
    row?.closure_reason ??
    row?.exit_reason ??
    row?.close_reason ??
    row?.result ??
    row?.source;
  if (reason == null || !String(reason).trim()) return "—";
  return String(reason).replace(/_/g, " ").trim().toUpperCase();
}

function resolveRealizedPnl(row) {
  const side = resolvePositionSide(row);
  const entry = Number(row?.entry_price ?? row?.entry ?? 0);
  const exit = Number(row?.exit_price ?? row?.exit ?? 0);
  const size = Math.abs(Number(row?.size ?? row?.dealSize ?? 1)) || 1;

  let pnl = row?.realized_pnl_gbp ?? row?.pnl_gbp ?? row?.ig_pnl_currency ?? row?.pnl;
  if (pnl != null && !Number.isNaN(Number(pnl))) {
    pnl = Number(pnl);
  } else {
    pnl = null;
  }

  if (entry > 0 && exit > 0) {
    const pointsMove = exit - entry;
    const implied =
      side === "SELL" ? (entry - exit) * size : pointsMove * size;
    if (side === "BUY" && exit < entry) {
      if (pnl == null || Number(pnl) >= 0) {
        pnl = implied;
      }
    } else if (side === "SELL" && exit > entry) {
      if (pnl == null || Number(pnl) >= 0) {
        pnl = implied;
      }
    } else if (pnl == null) {
      pnl = implied;
    }
  }

  if (pnl == null || Number.isNaN(Number(pnl))) return null;
  return Number(pnl);
}

function formatPositionSize(row) {
  const size = Math.abs(Number(row?.size ?? row?.dealSize ?? 0));
  if (!size) return "—";
  return Number.isInteger(size) ? String(size) : size.toFixed(2);
}

function renderActivePositions(payload) {
  const body = $("active-pos-body");
  const empty = $("active-pos-empty");
  if (!body) return;
  body.innerHTML = "";
  const rows = resolveOpenPositionRows(payload);
  if (!rows.length) {
    if (empty) empty.classList.remove("hidden");
    return;
  }
  if (empty) empty.classList.add("hidden");
  for (const row of rows) {
    const epic = row.epic || "";
    const side = resolvePositionSide(row);
    const sideClass = sideBadgeClass(side);
    const floatingPnl = computeFloatingPnl(row);
    const pnlClass = pnlToneClass(floatingPnl);
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${epicLabel(epic)}</td>
      <td><span class="pos-side-badge ${sideClass}">${side}</span></td>
      <td>${formatPositionSize(row)}</td>
      <td>${fmtPriceForEpic(row.entry ?? row.level, epic)}</td>
      <td>${fmtPriceForEpic(row.current ?? row.market ?? row.mkt ?? row.broker_mark, epic)}</td>
      <td>${fmtPriceForEpic(row.stop ?? row.stop_level, epic)}</td>
      <td>${fmtPriceForEpic(row.target ?? row.limit ?? row.limit_level, epic)}</td>
      <td class="pos-floating-pnl ${pnlClass}">${fmtSignedMoney(floatingPnl)}</td>
    `;
    body.appendChild(tr);
  }
}

function renderClosedTransactionHistory(payload) {
  const body = $("closed-trades-body");
  const empty = $("closed-trades-empty");
  if (!body) return;
  body.innerHTML = "";
  const rows = resolveClosedTradeRows(payload);
  if (!rows.length) {
    if (empty) empty.classList.remove("hidden");
    return;
  }
  if (empty) empty.classList.add("hidden");
  for (const row of rows) {
    const epic = row.epic || row.market || "";
    const side = resolvePositionSide(row);
    const sideClass = sideBadgeClass(side);
    const realized = resolveRealizedPnl(row);
    const pnlClass = pnlToneClass(realized);
    const reason = resolveClosureReason(row);
    const reasonClass =
      realized != null && Number(realized) < 0
        ? "pnl-loss"
        : /LOSS|STOP|MANUAL|FLATTEN/i.test(reason)
          ? "pnl-loss"
          : "";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${epicLabel(epic)}</td>
      <td><span class="pos-side-badge ${sideClass}">${side}</span></td>
      <td>${formatPositionSize(row)}</td>
      <td>${fmtPriceForEpic(row.entry_price ?? row.entry, epic)}</td>
      <td>${fmtPriceForEpic(row.exit_price ?? row.exit, epic)}</td>
      <td class="closure-reason ${reasonClass}">${reason}</td>
      <td class="pos-floating-pnl ${pnlClass}">${fmtSignedMoney(realized)}</td>
    `;
    body.appendChild(tr);
  }
}

function renderPositions(payload) {
  renderActivePositions(payload);
  renderClosedTransactionHistory(payload);
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
  renderAssets(payload);
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

function renderTriageNominalEmpty() {
  const container = $("triage-log");
  if (!container) return;
  container.innerHTML = `
    <div class="triage-reconnect-fallback triage-nominal-empty" role="status" aria-live="polite">
      <span class="triage-reconnect-icon" aria-hidden="true">✓</span>
      <p class="triage-reconnect-text">24HR TRIAGE NOMINAL — ledger clear, feed live (0 events)</p>
    </div>
  `;
  const counter = $("log-line-count");
  if (counter) counter.textContent = "0 events";
}

function appendTriageEvents(events, frameMeta = {}) {
  const container = $("triage-log");
  if (!container) return;

  const gen = frameMeta.triage_generation;
  const forceReset =
    frameMeta.reset_client_cache === true ||
    frameMeta.full_sync === true ||
    (gen != null && gen !== lastTriageGeneration);
  const feedHealthy =
    frameMeta.feed_healthy === true ||
    frameMeta.ledger_initialized === true ||
    frameMeta.feed_status === "NOMINAL" ||
    frameMeta.feed_status === "INITIALIZED";

  if (!Array.isArray(events) || events.length === 0) {
    if (forceReset) {
      triageBuffer.length = 0;
      if (gen != null) lastTriageGeneration = gen;
    }
    if (feedHealthy) {
      renderTriageNominalEmpty();
    } else {
      renderTriageReconnectFallback();
    }
    return;
  }

  if (forceReset) {
    triageBuffer.length = 0;
    if (gen != null) lastTriageGeneration = gen;
  }

  for (const ev of events) {
    const key = `${ev.iso || ev.ts}-${ev.event_type}`;
    if (!forceReset && triageBuffer.some((x) => x._key === key)) continue;
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

function formatHeaderClock(now = new Date()) {
  const local = now instanceof Date ? now : new Date();
  return local.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

function updateClock() {
  const el = $("clock");
  if (!el) return;
  el.textContent = formatHeaderClock(new Date());
}

let clockTickerId = null;

function startHeaderClockTicker() {
  updateClock();
  if (clockTickerId != null) return;
  clockTickerId = setInterval(updateClock, 1000);
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

    ws.onopen = () => {
      triageBuffer.length = 0;
      lastTriageGeneration = null;
      setPill(pill, "live", {
        live: "TRIAGE LIVE",
        dead: "TRIAGE DOWN",
        connecting: "TRIAGE …",
      });
    };

    ws.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data);
        if (data && data.type === "TRIAGE_FRAME" && Array.isArray(data.events)) {
          appendTriageEvents(data.events, {
            triage_generation: data.triage_generation,
            reset_client_cache: data.reset_client_cache,
            full_sync: data.full_sync,
            feed_status: data.feed_status || data.status,
            ledger_initialized: data.ledger_initialized === true,
            feed_healthy: true,
          });
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
  startHeaderClockTicker();
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) updateClock();
  });
  bindLogTabs();
  bindShadowToggle();
  bindBlueprintToggle();
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
