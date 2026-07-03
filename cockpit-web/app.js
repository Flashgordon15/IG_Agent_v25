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

const LOG_MAX_LINES = 25;
const TRIAGE_MAX_LINES = 25;
const SRE_POLL_MS = 2000;
const PP_EXPANSION_THRESHOLD = 1200;
const PP_DEFENSE_THRESHOLD = 800;
const SPARKLINE_LOOKBACK = 50;
const STALE_FEED_SEC = 5.0;
const PRODUCTION_CONFIDENCE_FLOOR = 62;
const RSI_OVERBOUGHT_CEILING = 85;

/** Cold-start UI baselines — never throw on null Iron Ledger payloads. */
const UI_BASELINE_DEFAULTS = Object.freeze({
  performance_points: 1000,
  exposure_gbp: 0,
  pp_trajectory_trend: "neutral",
  headline_urgency: 0,
  compression_factor: 1,
});

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

const logRenderState = { dirty: false, scheduled: false };
const triageRenderState = { dirty: false, scheduled: false };
let lastMacroSteeringPayload = null;

let _jsonParseWorker = null;
let _jsonParseSeq = 0;
const _jsonParseWaiters = new Map();

function ensureJsonParseWorker() {
  if (_jsonParseWorker) return _jsonParseWorker;
  if (typeof Worker === "undefined") return null;
  try {
    _jsonParseWorker = new Worker("/static/json-parse-worker.js");
    _jsonParseWorker.onmessage = (event) => {
      const id = event.data && event.data.id;
      const waiter = _jsonParseWaiters.get(id);
      if (!waiter) return;
      _jsonParseWaiters.delete(id);
      if (event.data.ok) waiter.resolve(event.data.data);
      else waiter.resolve(null);
    };
    _jsonParseWorker.onerror = () => {
      _jsonParseWorker = null;
    };
  } catch (_) {
    _jsonParseWorker = null;
  }
  return _jsonParseWorker;
}

function parseJsonOffThread(text) {
  const worker = ensureJsonParseWorker();
  if (!worker || !text || text.length < 4096) {
    return Promise.resolve(null);
  }
  const id = ++_jsonParseSeq;
  return new Promise((resolve) => {
    _jsonParseWaiters.set(id, { resolve });
    worker.postMessage({ id, text });
    setTimeout(() => {
      if (_jsonParseWaiters.has(id)) {
        _jsonParseWaiters.delete(id);
        resolve(null);
      }
    }, 5000);
  });
}

/**
 * Null-safe JSON fetch — tolerates cold-start 404/abort without throwing.
 */
async function fetchJson(url, timeoutMs = 10000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { cache: "no-store", signal: ctrl.signal });
    clearTimeout(timer);
    if (!res.ok) return null;
    const text = await res.text();
    const offThread = await parseJsonOffThread(text);
    if (offThread) return offThread;
    const data = JSON.parse(text);
    return data && typeof data === "object" ? data : null;
  } catch (_) {
    clearTimeout(timer);
    return null;
  }
}

function safeObject(value) {
  try {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch (_) {
    return {};
  }
}

function runFlightDeckSafe(label, fn) {
  try {
    fn();
  } catch (err) {
    console.error(`[FlightDeck] ${label}`, err);
  }
}

function scoreboardTierClass(pp) {
  const n = Number(pp);
  if (!Number.isFinite(n)) return "tier-baseline";
  if (n > PP_EXPANSION_THRESHOLD) return "tier-expansion";
  if (n < PP_DEFENSE_THRESHOLD - 100) return "tier-defense-critical";
  if (n < PP_DEFENSE_THRESHOLD) return "tier-defense";
  return "tier-baseline";
}

function formatCapacityBanner(sb = {}, opt = {}) {
  const cap = Number(opt.capacity_multiplier ?? sb.capacity_multiplier);
  const size = Number(opt.size_factor_multiplier ?? sb.size_factor_multiplier);
  const parts = [];
  if (Number.isFinite(cap) && cap > 1.0001) {
    parts.push(`+${Math.round((cap - 1) * 100)}% Capacity`);
  }
  if (Number.isFinite(size) && size < 0.9999) {
    parts.push(`-${Math.round((1 - size) * 100)}% Sizing Compression`);
  }
  if (!parts.length) return "Sustained Baseline — nominal sizing envelope";
  return parts.join(" · ");
}

function formatSentimentDelta(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return { text: "—", cls: "" };
  const sign = n >= 0 ? "+" : "";
  return {
    text: `${sign}${n.toFixed(4)}/s`,
    cls: n > 0.0001 ? "delta-up" : n < -0.0001 ? "delta-down" : "",
  };
}

function formatTMinus(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) {
    return `T-${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  }
  return `T-${m}:${String(sec).padStart(2, "0")}`;
}

function replaceListItems(container, items, emptyLabel = "none") {
  if (!container) return;
  const frag = document.createDocumentFragment();
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "ledger-empty";
    li.textContent = emptyLabel;
    frag.appendChild(li);
  } else {
    for (const item of items) {
      const li = document.createElement("li");
      li.textContent = item;
      frag.appendChild(li);
    }
  }
  container.replaceChildren(frag);
  container.classList.toggle("ledger-scroll", items.length > 4);
}

function renderScoreboardPanel(orch) {
  const panel = $("scoreboard-panel");
  const ppEl = $("scoreboard-pp");
  const rankEl = $("scoreboard-rank");
  const wrEl = $("scoreboard-win-rate");
  const bannerEl = $("scoreboard-capacity-banner");
  if (!panel || !ppEl) return;

  const sb = safeObject(orch && orch.scoreboard);
  const opt = safeObject(orch && orch.optimization);
  const hasScoreboard = orch && typeof orch === "object" && orch.scoreboard != null;
  const ppVal = hasScoreboard
    ? safeNum(sb.total_pp, UI_BASELINE_DEFAULTS.performance_points)
    : UI_BASELINE_DEFAULTS.performance_points;
  ppEl.textContent = String(ppVal);
  ppEl.classList.toggle("scoreboard-warming", !hasScoreboard);
  if (rankEl) {
    rankEl.textContent = String(sb.rank || "baseline").replace(/_/g, " ").toUpperCase();
  }
  if (wrEl) {
    const wr = sb.rolling_win_rate;
    const windowN = Number(sb.rolling_window);
    if (!Number.isFinite(windowN) || windowN < 1) {
      wrEl.textContent = "WR n/a";
    } else if (wr != null) {
      wrEl.textContent = `WR ${(Number(wr) * 100).toFixed(1)}%`;
    } else {
      wrEl.textContent = "WR —";
    }
  }
  panel.className = `card sre-panel scoreboard-panel ${scoreboardTierClass(ppVal)}`;
  if (bannerEl) {
    const text = formatCapacityBanner(sb, opt);
    bannerEl.textContent = text;
    let bannerCls = "capacity-banner";
    if (text.includes("-")) bannerCls += " negative";
    else if (text.includes("+")) bannerCls += " positive";
    bannerEl.className = bannerCls;
  }
}

function renderExpectancyCommandStrip(orch) {
  const mount = $("avionics-expectancy-mount");
  if (!mount) return;

  const rows = Array.isArray(orch && orch.expectancy_metrics) ? orch.expectancy_metrics : [];
  if (!rows.length) {
    mount.innerHTML =
      '<h3 class="command-strip-section-title">Expectancy Optimizer</h3>' +
      '<p class="expectancy-strip-empty">Awaiting ML expectancy telemetry…</p>';
    return;
  }

  const head = rows[0] || {};
  const vetoFloor = head.veto_floor != null ? Number(head.veto_floor).toFixed(2) : "—";
  const frag = document.createDocumentFragment();
  const title = document.createElement("h3");
  title.className = "command-strip-section-title";
  title.textContent = "Expectancy Optimizer";
  frag.appendChild(title);

  const summary = document.createElement("p");
  summary.className = "expectancy-strip-summary";
  summary.textContent = `Veto floor ${vetoFloor} · ${rows.length} ranked`;
  frag.appendChild(summary);

  const list = document.createElement("ul");
  list.className = "expectancy-strip-list";
  for (const row of rows.slice(0, 6)) {
    if (!row || !row.epic) continue;
    const li = document.createElement("li");
    const slipTag = row.slippage_adaptive_active ? " · slip×" + Number(row.slippage_limit_factor_mult || 1).toFixed(2) : "";
    const ml = Number(row.ml_expectation_score || 0).toFixed(2);
    const kelly = Number(row.continuous_kelly_fraction || 0).toFixed(3);
    const shortEpic = String(row.epic).split(".").slice(-2, -1)[0] || row.epic;
    li.textContent = `${shortEpic} ML ${ml} · Kelly ${kelly}${slipTag}`;
    li.title = `${row.epic} · base cap ${row.base_kelly_cap} · slippage ${row.slippage_avg_pips || 0} pips`;
    list.appendChild(li);
  }
  frag.appendChild(list);
  mount.replaceChildren(frag);
}

let _cognitiveReasonerFrame = null;

function counselSeverityClass(severity) {
  const s = String(severity || "normal").toLowerCase();
  if (s === "execution_window") return "counsel-execution";
  if (s === "near_miss") return "counsel-near-miss";
  return "counsel-normal";
}

function renderCognitiveReasonerPanel(orch) {
  const panel = $("cognitive-reasoner-panel");
  const textEl = $("cognitive-reasoner-text");
  const spreadEl = $("cognitive-reasoner-spread");
  if (!panel || !textEl) return;

  const reason = String((orch && orch.cognitive_reason) || "").trim();
  const severity = (orch && orch.cognitive_reason_severity) || "normal";
  const meta = (orch && orch.cognitive_reason_meta) || {};
  const opt = (orch && orch.optimization) || {};
  const spreadRows = Array.isArray(opt.adaptive_spread_telemetry)
    ? opt.adaptive_spread_telemetry
    : [];

  textEl.textContent =
    reason || "Strategic counsel warming — awaiting orchestrator telemetry.";
  panel.className = `cognitive-reasoner-panel ${counselSeverityClass(severity)}`;

  if (spreadEl) {
    const epic = meta.epic || (spreadRows[0] && spreadRows[0].epic) || "";
    const row =
      spreadRows.find((r) => r && r.epic === epic) || spreadRows[0] || null;
    if (row && row.adaptive_ceiling_pts != null) {
      const widen = row.high_conviction_widen ? " · 1.25× ML widen" : "";
      spreadEl.textContent = `Adaptive spread ${Number(row.spread_pts).toFixed(2)} / ceiling ${Number(row.adaptive_ceiling_pts).toFixed(2)} pts${widen}`;
      spreadEl.classList.remove("hidden");
    } else if (meta.adaptive_spread_ceiling != null) {
      spreadEl.textContent = `Adaptive ceiling ${Number(meta.adaptive_spread_ceiling).toFixed(2)} pts · spread ${Number(meta.spread_pts || 0).toFixed(2)} pts`;
      spreadEl.classList.remove("hidden");
    } else {
      spreadEl.textContent = "";
      spreadEl.classList.add("hidden");
    }
  }
}

function scheduleCognitiveReasonerRender(orch) {
  if (!orch) return;
  if (_cognitiveReasonerFrame) return;
  _cognitiveReasonerFrame = requestAnimationFrame(() => {
    _cognitiveReasonerFrame = null;
    renderCognitiveReasonerPanel(orch);
  });
}

function ingestStateClass(state) {
  const s = String(state || "warming").toLowerCase();
  if (s === "active") return "ingest-active";
  if (s === "broken") return "ingest-broken";
  return "ingest-warming";
}

function renderApiIngestGrid(orch) {
  const grid = $("api-ingest-grid");
  if (!grid) return;
  const opt = (orch && orch.optimization) || {};
  const health = opt.api_ingest_health || {};
  const feeds = Array.isArray(health.feeds) ? health.feeds : [];
  const frag = document.createDocumentFragment();
  if (!feeds.length) {
    const li = document.createElement("li");
    li.className = "api-ingest-pill ingest-warming";
    li.textContent = "Ingest telemetry warming…";
    frag.appendChild(li);
  } else {
    for (const row of feeds) {
      const li = document.createElement("li");
      li.className = `api-ingest-pill ${ingestStateClass(row.state)}`;
      li.textContent = String(row.label || row.id || "feed");
      li.title = `${row.state || "warming"}${row.detail ? ` · ${row.detail}` : ""}`;
      frag.appendChild(li);
    }
  }
  grid.replaceChildren(frag);
}

function scheduleApiIngestRender(orch) {
  if (!orch) return;
  requestAnimationFrame(() => renderApiIngestGrid(orch));
}

function macroSteeringWarming(ms) {
  if (!ms || typeof ms !== "object") return true;
  const quality = ms.data_quality;
  if (quality && quality.warming === false) return false;
  if (quality && quality.warming === true) return true;
  const sentiment = ms.sentiment || {};
  const news = ms.news || {};
  const walk = ms.shadow_walk || {};
  const newsDefault =
    Number(news.seconds_to_next) >= 86400 && Number(news.countdown_norm ?? 0) === 0;
  const sentDefault =
    Number(sentiment.delta_5m) === 0 &&
    Number(sentiment.delta_30m) === 0 &&
    Number(sentiment.long_pct) === 50;
  const walkWarming =
    walk.projected_win_prob == null ||
    walk.reason === "warming" ||
    walk.reason === "insufficient_bars";
  return newsDefault && sentDefault && walkWarming;
}

function renderMacroSteeringPanel(macroSteering, wsMacro) {
  const ms = macroSteering || lastMacroSteeringPayload || {};
  const macro = ms.macro || wsMacro || {};
  const sentiment = ms.sentiment || {};
  const news = ms.news || {};
  const walk = ms.shadow_walk || {};
  const warming = macroSteeringWarming(ms);

  const d5 = sentiment.delta_5m ?? macro.sentiment_delta_5m;
  const d30 = sentiment.delta_30m ?? macro.sentiment_delta_30m;
  const d5El = $("macro-delta-5m");
  const d30El = $("macro-delta-30m");
  const newsEl = $("macro-news-tminus");
  const progEl = $("macro-news-progress");
  const walkEl = $("macro-shadow-walk");
  const epicBadge = $("macro-epic-badge");

  const d5Fmt = warming ? { text: "warming", cls: "macro-warming" } : formatSentimentDelta(d5);
  const d30Fmt = warming ? { text: "warming", cls: "macro-warming" } : formatSentimentDelta(d30);
  if (d5El) {
    d5El.textContent = d5Fmt.text;
    d5El.className = `macro-value ${d5Fmt.cls}`.trim();
  }
  if (d30El) {
    d30El.textContent = d30Fmt.text;
    d30El.className = `macro-value ${d30Fmt.cls}`.trim();
  }

  const secToNews = warming ? null : news.seconds_to_next;
  const countdownNorm = warming ? 0 : Number(news.countdown_norm ?? macro.news_countdown_norm ?? 0);
  if (newsEl) {
    newsEl.textContent =
      warming || secToNews == null ? "warming" : formatTMinus(secToNews);
    newsEl.classList.toggle("macro-warming", warming);
  }
  if (progEl) {
    const pct = Math.max(0, Math.min(100, countdownNorm * 100));
    progEl.style.width = `${pct.toFixed(1)}%`;
  }

  const walkReason = String(walk.reason || "");
  const walkWarming =
    walkReason === "warming" || walkReason === "insufficient_bars" || walk.projected_win_prob == null;
  const prob = walk.projected_win_prob;
  if (walkEl) {
    if (walkWarming) {
      walkEl.textContent = "warming";
      walkEl.className = "macro-value macro-warming";
      walkEl.title = "Insufficient bars for 48-bar shadow-walk";
    } else {
      const pct = (Number(prob) * 100).toFixed(1);
      const floor = Number(walk.veto_floor ?? 0.65);
      const veto = Boolean(walk.veto);
      if (veto) {
        walkEl.textContent = `${pct}% · macro veto`;
        walkEl.className = "macro-value shadow-caution";
        walkEl.title =
          `48-bar shadow-walk: projected ${pct}% win probability ` +
          `(floor ${(floor * 100).toFixed(0)}%) — long-hold entries discouraged`;
      } else {
        walkEl.textContent = `${pct}% · pass`;
        walkEl.className = `macro-value ${Number(prob) < floor ? "shadow-caution" : "shadow-pass"}`.trim();
        walkEl.title = `48-bar shadow-walk projected win rate ${pct}%`;
      }
    }
  }
  if (epicBadge) {
    const epic = ms.epic || "CS.D.EURUSD.CFD.IP";
    epicBadge.textContent = ASSET_NAMES[epic] || epic.split(".").slice(-2, -1)[0] || epic;
  }
}

function tokenQueueStarvedBuckets(guardian) {
  const buckets = (guardian && guardian.token_buckets) || {};
  const rows = Array.isArray(buckets) ? buckets : Object.values(buckets);
  return rows.filter((b) => {
    const waits = Number(b && b.queued_waits) || 0;
    const tokens = Number(b && b.tokens_available);
    return waits > 0 && tokens < 1.0;
  });
}

function tokenQueueDelayWarning(guardian) {
  return tokenQueueStarvedBuckets(guardian).length > 0;
}

function formatTokenBucketAlert(rows) {
  return rows
    .map((b) => {
      const name = String((b && b.name) || "bucket");
      const waits = Number(b && b.queued_waits) || 0;
      const tokens = Number(b && b.tokens_available);
      const tok = Number.isFinite(tokens) ? tokens.toFixed(2) : "—";
      return `${name}: ${waits} waits · ${tok} tok`;
    })
    .join(" · ");
}

function renderSystemHealthLedger(orch, guardian, reporting) {
  const panel = $("system-health-panel");
  const localEl = $("ledger-local-keys");
  const brokerEl = $("ledger-broker-registers");
  const badge = $("health-sync-badge");
  const alertStrip = $("health-alert-strip");
  const alertText = $("health-alert-text");
  const reportingLine = $("reporting-status-line");

  const tree = safeArray(orch && orch.position_tree);
  const localKeys = tree.map((row) => {
    const epic = String((row && row.epic) || "?");
    const dir = String((row && row.direction) || "").toUpperCase() || "—";
    const size = row && row.size != null ? row.size : "—";
    return `${epic} · ${dir} · ${size}`;
  });

  const registers = safeArray(((guardian && guardian.reconciliation_registers) || {}).registers);
  let brokerRows = [];
  if (registers.length) {
    const allStandby = registers.every(
      (reg) => String((reg && reg.sync_state) || "idle").toLowerCase() === "idle"
    );
    if (allStandby) {
      brokerRows = [`${registers.length} registers · standby (book in sync)`];
    } else {
      brokerRows = registers.map((reg) => {
        const epic = String((reg && reg.epic) || "—") || "slot";
        const sync = String((reg && reg.sync_state) || "idle").replace(/^idle$/i, "standby");
        const slot = reg && reg.slot != null ? `#${reg.slot}` : "";
        return `${slot} ${epic} · ${sync}`.trim();
      });
    }
  }

  replaceListItems(localEl, localKeys, "Flat book — no open positions");
  replaceListItems(brokerEl, brokerRows, "Registers warming…");

  const discrepancies = (guardian && guardian.state_sync_discrepancies) || [];
  const sync = (guardian && guardian.state_sync) || {};
  const syncDrift = discrepancies.length > 0 || sync.healthy === false;
  const starvedBuckets = tokenQueueStarvedBuckets(guardian);
  const tokenDelay = starvedBuckets.length > 0;
  const warn = syncDrift || tokenDelay;

  if (panel) panel.classList.toggle("sync-warning", warn);
  if (badge) {
    badge.className = warn ? "pill pill-dead" : "pill pill-live";
    badge.textContent = warn ? "SYNC ALERT" : "SYNC OK";
  }
  if (alertStrip && alertText) {
    if (warn) {
      const parts = [];
      if (discrepancies.length) {
        parts.push(`${discrepancies.length} chaos guardian discrepancy`);
      }
      if (sync.healthy === false) parts.push("state sync unhealthy");
      if (tokenDelay) {
        parts.push(`token starvation — ${formatTokenBucketAlert(starvedBuckets)}`);
      }
      alertText.textContent = parts.join(" · ");
      alertStrip.classList.remove("hidden");
    } else {
      alertStrip.classList.add("hidden");
    }
  }

  if (reportingLine) {
    const rep = reporting || {};
    const depth = rep.queue_depth != null ? rep.queue_depth : "—";
    const status = String(rep.subsystem_status || rep.status || "—").toUpperCase();
    const healthy = rep.healthy !== false;
    const coalesceSec = rep.coalesce_window_sec != null ? rep.coalesce_window_sec : 3600;
    const bufferDepth = rep.coalesce_buffer_depth != null ? rep.coalesce_buffer_depth : 0;
    const batches = rep.coalesce_batches_sent != null ? rep.coalesce_batches_sent : 0;
    reportingLine.textContent =
      `${status} · TG ${coalesceSec}s coalesce · buf ${bufferDepth} · batches ${batches} · queue ${depth} · ${healthy ? "healthy" : "degraded"}`;
  }
  renderCommandStripTelemetry(reporting, null);
}

function renderSyntheticHydrationBadge(diag) {
  const badge = $("synthetic-hydration-badge");
  if (!badge) return;
  const active =
    diag &&
    (diag.synthetic_hydration_active === true ||
      (diag.transport_recovery && diag.transport_recovery.synthetic_hydration_active === true));
  const tier = (diag && (diag.fallback_transport_tier || diag.transport_recovery?.fallback_transport_tier)) || "";
  if (active) {
    badge.classList.remove("hidden");
    const code = (diag && (diag.network_exception_code || diag.transport_failure_category)) || "";
    badge.textContent = code
      ? `SYNTHETIC HYDRATION ACTIVE · ${tier || "rest_poll"} · ${code}`
      : `SYNTHETIC HYDRATION ACTIVE · ${tier || "rest_poll"}`;
  } else {
    badge.classList.add("hidden");
  }
  renderCommandStripTelemetry(null, diag);
}

function renderCommandStripTelemetry(reporting, diag) {
  if (!document.body.classList.contains("cockpit-live")) return;
  const coalesceEl = $("command-strip-coalesce");
  const hydrationEl = $("command-strip-hydration");
  if (coalesceEl && reporting) {
    const sec = reporting.coalesce_window_sec != null ? reporting.coalesce_window_sec : 3600;
    const depth = reporting.coalesce_buffer_depth != null ? reporting.coalesce_buffer_depth : 0;
    const batches = reporting.coalesce_batches_sent != null ? reporting.coalesce_batches_sent : 0;
    const queue = reporting.queue_depth != null ? reporting.queue_depth : 0;
    coalesceEl.textContent = `TG coalesce ${sec}s · buf ${depth} · sent ${batches} · q ${queue}`;
  }
  if (hydrationEl && diag) {
    const active =
      diag.synthetic_hydration_active === true ||
      (diag.transport_recovery && diag.transport_recovery.synthetic_hydration_active === true);
    hydrationEl.classList.toggle("hidden", !active);
    if (active) {
      const tier = diag.fallback_transport_tier || diag.transport_recovery?.fallback_transport_tier || "rest_poll";
      hydrationEl.textContent = `SYNTHETIC HYDRATION ACTIVE · ${tier}`;
    }
  }
}

let _tradingHubTelemetryFrame = null;

function renderTradingHubTelemetry(diag, orch, macroSteering) {
  if (_tradingHubTelemetryFrame) return;
  _tradingHubTelemetryFrame = requestAnimationFrame(() => {
    _tradingHubTelemetryFrame = null;
    runFlightDeckSafe("renderTradingHubTelemetry", () => {
      _paintTradingHubTelemetry(diag, orch, macroSteering);
    });
  });
}

function _paintTradingHubTelemetry(diag, orch, macroSteering) {
  const safeDiag = safeObject(diag);
  const safeOrch = safeObject(orch);
  const ps = safeObject(safeDiag.portfolio_synthesis);
  const heat = safeObject(ps.cognitive_risk_heatmap || safeDiag.cognitive_risk_heatmap);
  const cov = safeObject(ps.covariance);
  const eq = safeObject(ps.equilibrium_allocation);
  const fuse = safeObject(ps.drawdown_fuse || eq.drawdown_fuse);
  const newsAlpha = safeObject(ps.news_alpha);
  const headlines = safeObject(newsAlpha.headlines);
  const recentList = safeArray(headlines.recent);
  const recentHeadline = safeObject(recentList[0]);
  const ingestHealth = safeObject(
    newsAlpha.api_ingest || safeObject(safeOrch.optimization).api_ingest_health
  );
  const ms = safeObject(macroSteering || lastMacroSteeringPayload);
  const news = safeObject(ms.news);
  const telemetry = safeObject(newsAlpha.telemetry);
  const horizon = safeObject(telemetry.horizon_sentiment);
  const sentiment = safeObject(ms.sentiment || horizon.epics || horizon);
  const walk = safeObject(ms.shadow_walk);

  const compression = safeNum(
    heat.compression_factor || cov.compression_factor,
    UI_BASELINE_DEFAULTS.compression_factor
  );
  const badge = $("global-risk-compression-badge");
  if (badge) {
    badge.textContent = `σ ${compression.toFixed(2)}`;
    badge.classList.toggle("compressed", compression < 0.99);
  }

  const grid = $("global-risk-heatmap-grid");
  const meta = $("global-risk-heatmap-meta");
  if (grid) {
    const frag = document.createDocumentFragment();
    const pairCells = safeArray(heat.pair_cells);
    const assetWeights = safeArray(heat.asset_weights);
    if (pairCells.length) {
      pairCells.slice(0, 12).forEach((cell) => {
        const row = safeObject(cell);
        const el = document.createElement("div");
        el.className = `global-risk-heatmap-cell band-${row.risk_band || "normal"}`;
        el.setAttribute("role", "listitem");
        const a = String(row.epic_a || "").split(".").pop() || "?";
        const b = String(row.epic_b || "").split(".").pop() || "?";
        el.textContent = `${a}↔${b} ${(safeNum(row.intensity) * 100).toFixed(0)}%`;
        frag.appendChild(el);
      });
    } else if (assetWeights.length) {
      assetWeights.slice(0, 12).forEach((row) => {
        const aw = safeObject(row);
        const el = document.createElement("div");
        const heatVal = safeNum(aw.heat);
        const band = heatVal >= 0.75 ? "critical" : heatVal >= 0.45 ? "elevated" : "normal";
        el.className = `global-risk-heatmap-cell band-${band}`;
        el.setAttribute("role", "listitem");
        const label = String(aw.epic || "").split(".").pop() || aw.epic || "?";
        el.textContent = `${label} w=${safeNum(aw.allocation_weight).toFixed(2)}`;
        frag.appendChild(el);
      });
    } else {
      const placeholder = document.createElement("div");
      placeholder.className = "global-risk-heatmap-cell band-normal";
      placeholder.textContent = "Portfolio synthesis warming…";
      frag.appendChild(placeholder);
    }
    grid.replaceChildren(frag);
  }
  if (meta) {
    const collective = safeNum(heat.collective_coefficient || cov.collective_coefficient);
    const pp = safeNum(
      fuse.platform_pp || safeObject(safeOrch.scoreboard).total_pp,
      UI_BASELINE_DEFAULTS.performance_points
    );
    const defensive = Boolean(fuse.defensive_fuse_active);
    meta.textContent =
      `ρ̄ ${collective.toFixed(3)} · σ ${compression.toFixed(2)} · PP ${pp}` +
      (defensive ? " · fuse ON" : "");
  }

  const urgency = safeNum(recentHeadline.urgency, UI_BASELINE_DEFAULTS.headline_urgency);
  const urgencyBadge = $("hub-headline-urgency-badge");
  const urgencyBar = $("hub-headline-urgency-bar");
  if (urgencyBadge) urgencyBadge.textContent = `U ${urgency.toFixed(2)}`;
  if (urgencyBar) urgencyBar.style.width = `${Math.min(100, urgency * 100).toFixed(1)}%`;

  const newsEl = $("hub-macro-news-tminus");
  const progEl = $("hub-macro-news-progress");
  const secToNews = news.seconds_to_next;
  if (newsEl) {
    newsEl.textContent = secToNews == null ? "—" : formatTMinus(secToNews);
  }
  if (progEl) {
    const pct = Math.max(0, Math.min(100, safeNum(news.countdown_norm) * 100));
    progEl.style.width = `${pct.toFixed(1)}%`;
  }

  const sentEl = $("hub-macro-sentiment-deltas");
  if (sentEl) {
    const d5 = safeNum(sentiment.delta_5m ?? safeObject(ms.sentiment).delta_5m);
    const d15 = safeNum(sentiment.delta_15m);
    const d1h = safeNum(sentiment.delta_1h);
    sentEl.textContent = `${formatSentimentDelta(d5).text} / ${formatSentimentDelta(d15).text} / ${formatSentimentDelta(d1h).text}`;
  }

  const walkEl = $("hub-macro-shadow-walk");
  if (walkEl) {
    const prob = walk.projected_win_prob;
    if (prob == null) {
      walkEl.textContent = "warming";
      walkEl.className = "hub-macro-value macro-warming";
    } else {
      walkEl.textContent = `${(safeNum(prob) * 100).toFixed(1)}%`;
      walkEl.className = `hub-macro-value ${walk.veto ? "shadow-caution" : "shadow-pass"}`;
    }
  }

  const hubGrid = $("hub-api-ingest-grid");
  const ingestPill = $("hub-ingest-status-pill");
  if (hubGrid) {
    const feeds = safeArray(ingestHealth.feeds);
    const frag = document.createDocumentFragment();
    const wanted = ["Yahoo Ingest", "Finnhub WS", "IG Stream", "Sentiment Surface"];
    const byLabel = new Map(feeds.map((f) => [String(safeObject(f).label || ""), safeObject(f)]));
    const renderFeeds = wanted.length
      ? wanted.map((label) => byLabel.get(label) || { label, state: "warming" })
      : feeds;
    if (!renderFeeds.length) {
      const li = document.createElement("li");
      li.className = "api-ingest-pill ingest-warming";
      li.textContent = "Ingest warming…";
      frag.appendChild(li);
    } else {
      renderFeeds.forEach((row) => {
        const r = safeObject(row);
        const li = document.createElement("li");
        li.className = `api-ingest-pill ${ingestStateClass(r.state)}`;
        li.textContent = String(r.label || r.id || "feed");
        frag.appendChild(li);
      });
    }
    hubGrid.replaceChildren(frag);
    if (ingestPill) {
      const broken = feeds.filter((f) => String(safeObject(f).state).toLowerCase() === "broken").length;
      const active = feeds.filter((f) => String(safeObject(f).state).toLowerCase() === "active").length;
      ingestPill.className = broken > 0 ? "pill pill-dead" : active > 0 ? "pill pill-live" : "pill pill-warn";
      ingestPill.textContent = broken > 0 ? "INGEST DEGRADED" : active > 0 ? "INGEST LIVE" : "INGEST WARMING";
    }
  }

  const ppTraj = safeObject(
    safeDiag.pp_trajectory_7d ||
      safeObject(window.__lastIronCage).pp_trajectory_7d ||
      {}
  );
  const ppScores = safeArray(ppTraj.pp_scores);
  const ppDays = safeArray(ppTraj.days);
  const ppTrend = String(ppTraj.trend || UI_BASELINE_DEFAULTS.pp_trajectory_trend);
  const pathEl = $("hub-pp-trajectory-path");
  const badgeEl = $("hub-pp-trajectory-badge");
  const labelsEl = $("hub-pp-trajectory-labels");
  const metaEl = $("hub-pp-trajectory-meta");
  if (pathEl && ppScores.length >= 2) {
    const w = 400;
    const h = 80;
    const pad = 6;
    const minP = Math.min(...ppScores, safeNum(ppTraj.defense_threshold, 800));
    const maxP = Math.max(...ppScores, safeNum(ppTraj.expansion_threshold, 1200));
    const span = Math.max(1, maxP - minP);
    const pts = ppScores.map((pp, i) => {
      const x = pad + (i / Math.max(1, ppScores.length - 1)) * (w - pad * 2);
      const y = h - pad - ((safeNum(pp) - minP) / span) * (h - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    pathEl.setAttribute("d", `M ${pts.join(" L ")}`);
    pathEl.className = `hub-pp-trajectory-path trend-${ppTrend}`;
  } else if (pathEl) {
    pathEl.setAttribute("d", "");
    pathEl.className = "hub-pp-trajectory-path trend-neutral";
  }
  if (badgeEl) {
    const label =
      ppTrend === "expansion" ? "EXPANSION" : ppTrend === "defense" ? "DEFENSE" : "NEUTRAL";
    badgeEl.textContent = label;
    badgeEl.className = `hub-pp-trend-badge trend-${ppTrend}`;
  }
  if (labelsEl) {
    const frag = document.createDocumentFragment();
    const labels = ppDays.length ? ppDays : ppScores.map((_, i) => `D${i + 1}`);
    labels.forEach((day) => {
      const span = document.createElement("span");
      span.textContent = String(day).slice(5) || String(day);
      frag.appendChild(span);
    });
    labelsEl.replaceChildren(frag);
  }
  if (metaEl) {
    const latest = safeNum(
      ppTraj.latest_pp || ppScores[ppScores.length - 1],
      UI_BASELINE_DEFAULTS.performance_points
    );
    const slope = safeNum(ppTraj.slope);
    metaEl.textContent =
      ppScores.length >= 2
        ? `PP ${latest} · slope ${slope >= 0 ? "+" : ""}${slope.toFixed(1)}/day · ${ppScores.length}d window`
        : `PP ${UI_BASELINE_DEFAULTS.performance_points} · Neutral trajectory · £${UI_BASELINE_DEFAULTS.exposure_gbp.toFixed(2)} exposure`;
  }
}

function renderGlobalRiskHeatMap(diag) {
  renderTradingHubTelemetry(diag, window.__lastOrch || null, lastMacroSteeringPayload);
}

function scheduleTradingHubTelemetryRender(diag, orch, macro) {
  renderTradingHubTelemetry(diag || {}, orch || {}, macro || null);
}

function refreshLiveHydrationPanels(iron, orch, diag, reporting, healthLight) {
  try {
    if (iron) {
      window.__lastIronCage = iron;
      const gateMap = ironGatesToMap(iron.gates);
      if (Object.keys(gateMap).length) {
        renderGates(gateMap);
      }
    }
    const stageTokens = effectiveBootStageTokens(
      (orch && orch.stage_tokens) || {},
      iron,
      healthLight,
      orch
    );
    const currentStage = resolveBootCurrentStage(stageTokens, iron, healthLight, diag);
    renderBootStageChecklist(
      stageTokens,
      currentStage,
      (orch && orch.stage_status) || (orch && orch.phase_status),
      (orch && orch.stage_errors) || {}
    );
    if (diag) renderBootAutonomicBanner(diag, iron, healthLight);
    if (diag) renderSyntheticHydrationBadge(diag);
    scheduleTradingHubTelemetryRender(diag, orch, lastMacroSteeringPayload);
    if (reporting || diag) renderCommandStripTelemetry(reporting, diag);
    refineMasterVitalsFromSre(orch, iron);
  } catch (e) {
    console.error("[FlightDeck] refreshLiveHydrationPanels", e);
  }
}

function renderRotationPanel(rotationPayload, rejectionsPayload) {
  const pill = $("rotation-meta-pill");
  const meta = $("rotation-meta-line");
  const list = $("rotation-rank-list");
  const rejectLine = $("rotation-reject-line");
  if (!list) return;

  const body = rotationPayload || {};
  const rot = body.rotation || {};
  const ranks = safeArray(body.orchestrator_ranks);
  const active = safeArray(body.active_epics);
  const sweeps = Number(rot.rotation_sweep_count || body.routing?.rotation_sweep_count || 0);
  const current = String(body.routing?.current_epic || active[0] || "—");
  const escape = Boolean(rot.rotation_escape_active);

  if (pill) {
    pill.className = sweeps > 0 ? "pill pill-live" : "pill pill-warn";
    pill.textContent = sweeps > 0 ? "ROTATION LIVE" : "ROTATION WARMING";
  }
  if (meta) {
    const activeLabel = epicLabel(current);
    const topN = active.length ? active.map(epicLabel).join(" · ") : "—";
    meta.textContent =
      `${sweeps} sweeps · focus ${activeLabel} · top stack: ${topN}` +
      (escape ? " · escape hatch active" : "");
  }

  const rows = ranks.length
    ? ranks.map((row) => {
        const epic = String(row.epic || "");
        const status = String(row.status || "");
        const rank = row.rank != null ? `#${row.rank}` : "";
        const score = row.score != null ? Number(row.score).toFixed(1) : "—";
        const tag =
          status === "IN_TOP_3"
            ? "ACTIVE"
            : status === "MUTED"
              ? "MUTED"
              : status === "RANKED_OUT"
                ? "out"
                : status.toLowerCase();
        return `${rank} ${epicLabel(epic)} · ${score} · ${tag}`;
      })
    : active.map((epic, i) => `#${i + 1} ${epicLabel(epic)} · active`);

  replaceListItems(list, rows, "No rotation ranks yet");

  if (rejectLine) {
    const rejects = safeArray((rejectionsPayload && rejectionsPayload.rejections) || []);
    const latest = rejects[0];
    const reason = latest && (latest.reason || latest.rejection_reason || latest.detail);
    if (reason) {
      rejectLine.textContent = `Latest reject: ${String(reason).slice(0, 120)}`;
      rejectLine.classList.remove("hidden");
    } else {
      rejectLine.textContent = "";
      rejectLine.classList.add("hidden");
    }
  }
}

/**
 * Parallel SRE fetches — partial success keeps poll loop alive (no all-or-nothing).
 */
async function fetchSreBundle(qs) {
    const specs = [
    ["gauge", `/api/iron_gauge${qs}`],
    ["orch", `/api/orchestrator_state${qs}`],
    ["guardian", `/api/guardian_status${qs}`],
    ["reporting", `/api/reporting_status${qs}`],
    ["macro", `/api/macro_steering${qs}`],
    ["diag", `/api/ai_diagnostics${qs}`],
    ["iron", `/api/iron_cage_status${qs}`],
    ["rotation", `/api/rotation_status${qs}`],
    ["rejections", `/api/rejections${qs}`],
  ];
  const results = await Promise.allSettled(
    specs.map(([, url]) => fetchJson(url, 8000))
  );
  const out = {};
  specs.forEach(([key], idx) => {
    const row = results[idx];
    out[key] = row.status === "fulfilled" ? row.value : null;
  });
  return out;
}

async function pollSreTelemetry() {
  if (pollSreTelemetry._inFlight) return;
  pollSreTelemetry._inFlight = true;
  let orch = null;
  let guardian = null;
  let reporting = null;
  let macro = null;
  let diag = null;
  let iron = null;
  let rotation = null;
  let rejections = null;
  try {
    const qs = `?v=${BUILD_TS}&_=${Date.now()}`;
    const pill = $("sre-poll-status");
    const bundle = await fetchSreBundle(qs);
    const gauge = bundle.gauge;
    orch = bundle.orch;
    guardian = bundle.guardian;
    reporting = bundle.reporting;
    macro = bundle.macro;
    diag = bundle.diag;
    iron = bundle.iron;
    rotation = bundle.rotation;
    rejections = bundle.rejections;
    const liveCount = [orch, guardian, reporting, iron, diag, macro].filter(Boolean).length;
    pollSreTelemetry._failStreak = liveCount > 0 ? 0 : (pollSreTelemetry._failStreak || 0) + 1;
    requestAnimationFrame(() => {
      runFlightDeckSafe("pollSreTelemetry.frame", () => {
        if (pill) {
          if (liveCount >= 3) {
            pill.className = "pill pill-live";
            pill.textContent = "SRE LIVE";
          } else if (liveCount >= 1) {
            pill.className = "pill pill-warn";
            pill.textContent = `SRE PARTIAL (${liveCount}/8)`;
          } else {
            pill.className = "pill pill-warn";
            pill.textContent = "SRE WARMING";
          }
        }
        if (gauge) window.__lastIronGauge = gauge;
    if (orch) window.__lastOrch = orch;
        runFlightDeckSafe("scoreboard", () => orch && renderScoreboardPanel(orch));
        runFlightDeckSafe("expectancy", () => orch && renderExpectancyCommandStrip(orch));
        runFlightDeckSafe("cognitive", () => scheduleCognitiveReasonerRender(orch));
        runFlightDeckSafe("macro", () => {
          if (macro) {
            lastMacroSteeringPayload = macro;
            renderMacroSteeringPanel(macro, null);
          }
        });
        runFlightDeckSafe("tradingHub", () =>
          scheduleTradingHubTelemetryRender(diag, orch, macro)
        );
        runFlightDeckSafe("healthLedger", () =>
          renderSystemHealthLedger(orch, guardian, reporting)
        );
        runFlightDeckSafe("apiIngest", () => scheduleApiIngestRender(orch));
        runFlightDeckSafe("rotation", () => renderRotationPanel(rotation, rejections));
        runFlightDeckSafe("vitals", () => refineMasterVitalsFromSre(orch, iron));
        runFlightDeckSafe("bootStages", () => {
          if (!orch) return;
          const tokens = orch.stage_tokens || {};
          const status = orch.stage_status || orch.phase_status || {};
          const current = resolveBootCurrentStage(tokens, iron, diag, orch);
          renderBootStageChecklist(tokens, current, status, orch.stage_errors || {});
        });
        runFlightDeckSafe("autonomicSweep", () =>
          applyAutonomicStageRecoverySweep(orch, diag, iron, null)
        );
        if (bootSplashDismissed) {
          runFlightDeckSafe("hydrationPanels", () =>
            refreshLiveHydrationPanels(iron, orch, diag, reporting, null)
          );
        } else {
          runFlightDeckSafe("bootSplash", () => {
            if (diag) renderSyntheticHydrationBadge(diag);
            if (reporting || diag) renderCommandStripTelemetry(reporting, diag);
            maybeForceLiveCockpitLayout(iron || window.__lastIronCage, orch, diag, reporting);
          });
        }
      });
    });
  } catch (e) {
    console.error("[FlightDeck] pollSreTelemetry", e);
    const pill = $("sre-poll-status");
    if (pill) {
      pill.className = "pill pill-dead";
      pill.textContent = "SRE DEGRADED";
    }
    runFlightDeckSafe("pollFallbackHub", () =>
      scheduleTradingHubTelemetryRender(diag || {}, orch || {}, macro)
    );
  } finally {
    pollSreTelemetry._inFlight = false;
  }
}

function startSrePollLoop() {
  pollSreTelemetry();
  if (startSrePollLoop._timer) return;
  startSrePollLoop._timer = setInterval(pollSreTelemetry, SRE_POLL_MS);
}

const VITALS_MESSAGES = {
  HEALTHY:
    "🟢 All gates complete. Telemetry live. AI monitoring execution.",
  WARMING:
    "🟡 Cockpit operational — orchestrator surfaces still synchronizing.",
  STABILIZER: (n, total) =>
    `🟡 Production stabilizer cycle ${n}/${total} — panels unlock when seal APPROVED (~${Math.max(1, total - n) * 30}s cooldown remaining)`,
  BOOT_PROGRESS: (stage) =>
    `🟡 9-stage boot in progress — ${stage || "orchestrator"} running…`,
  DEGRADED: (mod) =>
    `🟡 Fault found in ${mod || "System"}. Identifying and passing to sandbox engineer.`,
  PEAK: "🚀 Trading active. Execution plane armed.",
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
  const v = safeNum(n, 0);
  return `£${v.toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function safeNum(value, fallback = 0) {
  try {
    const n = Number(value);
    return Number.isFinite(n) ? n : fallback;
  } catch (_) {
    return fallback;
  }
}

function safeArray(value) {
  try {
    return Array.isArray(value) ? value : [];
  } catch (_) {
    return [];
  }
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

function formatEpicPrice(epic, mid) {
  const m = Number(mid);
  if (!m || m <= 0) return "—";
  if (String(epic || "").includes("EURUSD")) return m.toFixed(5);
  if (String(epic || "").includes("GOLD")) return m.toFixed(2);
  return m.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
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
  renderCompactGatesSidebar(gates);
}

function ironGatesToMap(ironGates) {
  const map = {};
  if (!Array.isArray(ironGates)) return map;
  for (const g of ironGates) {
    const id = String((g && g.id) || "").toUpperCase();
    if (!id) continue;
    map[id] = {
      status: String((g && g.status) || "pending").toLowerCase(),
      detail: String((g && g.detail) || ""),
    };
  }
  return map;
}

function renderCompactGatesSidebar(gates) {
  const mount = $("avionics-gates-mount");
  if (!mount || !document.body.classList.contains("cockpit-live")) return;
  mount.innerHTML = "";
  const heading = document.createElement("h3");
  heading.className = "command-strip-section-title";
  heading.textContent = "4-Gate Checklist";
  mount.appendChild(heading);
  const wrap = document.createElement("div");
  wrap.className = "gate-grid";
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
    wrap.appendChild(row);
  }
  mount.appendChild(wrap);
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

function stabilizerProgress(orch) {
  const stab =
    (orch && orch.optimization && orch.optimization.runtime_stabilizer) || {};
  if (stab.skipped) return null;
  const seal = String(stab.seal || "PENDING").toUpperCase();
  if (seal === "APPROVED" || seal === "REJECTED") return null;
  const cycles = safeArray(stab.cycles);
  const total = Number(stab.cycle_count || 5);
  return { done: cycles.length, total: total > 0 ? total : 5, seal };
}

function bootStageInProgress(orch) {
  const tokens = (orch && orch.stage_tokens) || {};
  const status = (orch && orch.stage_status) || {};
  if (!tokens || typeof tokens !== "object") return null;
  for (const stage of BOOT_STAGES) {
    const rag = String(status[stage] || "").toUpperCase();
    const tok = String(tokens[stage] || "").toUpperCase();
    if (rag === "RUNNING" || rag === "PENDING") return stage;
    if (tok && !tokenIsSuccess(tok)) return stage;
  }
  return null;
}

function refineMasterVitalsFromSre(orch, iron) {
  const banner = $("master-vitals-banner");
  if (!banner) return;
  const locked = new Set(["EMERGENCY", "DEGRADED"]);
  if (locked.has(String(banner.dataset.status || "").toUpperCase())) return;

  const stab = stabilizerProgress(orch);
  if (stab) {
    banner.textContent = VITALS_MESSAGES.STABILIZER(stab.done, stab.total);
    banner.className = "master-vitals-banner vitals-warming";
    banner.dataset.status = "WARMING";
    return;
  }

  const bootStage = bootStageInProgress(orch);
  if (bootStage && !Boolean((orch && orch.trade_ready) || (iron && iron.trade_ready))) {
    banner.textContent = VITALS_MESSAGES.BOOT_PROGRESS(bootStage);
    banner.className = "master-vitals-banner vitals-warming";
    banner.dataset.status = "WARMING";
    return;
  }

  const tradeReady = Boolean(
    (iron && iron.trade_ready) || (orch && orch.trade_ready) || allIronGatesGreen(iron)
  );
  const orchWarming = Boolean(
    orch && (orch.warming_up || orch.feed_warming_progress || !orch.primed)
  );
  const stageTokens = (orch && orch.stage_tokens) || {};
  const effective = effectiveBootStageTokens(stageTokens, iron, null, orch);
  const bootStale = tradeReady && !allBootStagesGreen(effective);

  if (orch && orch.armed && tradeReady && !orchWarming) {
    banner.textContent = VITALS_MESSAGES.PEAK;
    banner.className = "master-vitals-banner vitals-peak";
    banner.dataset.status = "PEAK";
  } else if (tradeReady && (orchWarming || bootStale)) {
    banner.textContent = VITALS_MESSAGES.WARMING;
    banner.className = "master-vitals-banner vitals-warming";
    banner.dataset.status = "WARMING";
  } else if (tradeReady) {
    banner.textContent = VITALS_MESSAGES.HEALTHY;
    banner.className = "master-vitals-banner vitals-healthy";
    banner.dataset.status = "HEALTHY";
  } else if (orch && (orch.armed || Object.keys(stageTokens).length)) {
    banner.textContent = VITALS_MESSAGES.BOOT_PROGRESS(bootStageInProgress(orch));
    banner.className = "master-vitals-banner vitals-warming";
    banner.dataset.status = "WARMING";
  }
}

function renderMarketStatusPill(card, epic, marketStates, feedStale = false) {
  if (!card) return;
  const states = marketStates && typeof marketStates === "object" ? marketStates : {};
  const state = String(states[epic] || "LISTENING").toUpperCase();
  const label = MARKET_STATE_LABELS[state] || state;
  const feedPill = card.querySelector(".feed-status-pill");
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
  if (feedPill) feedPill.hidden = true;
  if (feedStale) {
    pill.textContent = "FEED STALE";
    pill.className = "market-status-pill state-stale";
    pill.title = "Quote stream older than freshness threshold";
    return;
  }
  pill.textContent = state === "LISTENING" ? "LIVE" : `LIVE · ${label}`;
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
  const spread = (payload && payload.spread) || {};
  const epics = (payload && payload.epics) || {};
  const marketStates = (payload && payload.market_states_map) || {};
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
        <div class="asset-price" aria-label="Mid price">—</div>
        <div class="asset-spread" aria-label="Spread">—</div>
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
    renderMarketStatusPill(card, epic, marketStates, stale);
    const nameEl = card.querySelector(".asset-name");
    if (nameEl) nameEl.textContent = epicLabel(epic);
    const priceEl = card.querySelector(".asset-price");
    if (priceEl) priceEl.textContent = formatEpicPrice(epic, mid);
    const sprEl = card.querySelector(".asset-spread");
    if (sprEl) {
      const spreadVal = Number(spr || (offer > 0 && bid > 0 ? offer - bid : 0));
      sprEl.textContent =
        spreadVal > 0 ? `Spread ${spreadVal.toFixed(epic.includes("EURUSD") ? 5 : 2)}` : "Spread —";
    }
    const zEl = card.querySelector(".z-val");
    if (zEl) zEl.textContent = Number(sp.z_score || 0).toFixed(2);
    const thEl = card.querySelector(".th-val");
    if (thEl) thEl.textContent = Number(sp.throttle || 0).toFixed(2);
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
    const live = mode !== "SHADOW";
    badge.textContent = live ? "LIVE" : "SHADOW";
    badge.className = `shadow-mode-badge ${live ? "live" : "active"}`;
  }
  const unreal = $("shadow-unrealized");
  const real = $("shadow-realized");
  const total = $("shadow-total");
  const openCount = $("shadow-open-count");
  if (unreal) unreal.textContent = fmtMoney(safeNum(st.unrealized_gbp, 0));
  if (real) real.textContent = fmtMoney(safeNum(st.realized_gbp, 0));
  if (total) total.textContent = fmtMoney(safeNum(st.total_gbp, 0));
  if (openCount) openCount.textContent = String(safeNum(st.open_count, 0));
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
        const live = mode !== "SHADOW";
        badge.textContent = live ? "LIVE" : "SHADOW";
        badge.className = `shadow-mode-badge ${live ? "live" : "active"}`;
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

let telemetryRenderScheduled = false;
let pendingTelemetryPayload = null;

function renderPayload(payload) {
  if (!payload || typeof payload !== "object") return;
  pendingTelemetryPayload = payload;
  if (telemetryRenderScheduled) return;
  telemetryRenderScheduled = true;
  requestAnimationFrame(() => {
    telemetryRenderScheduled = false;
    const data = pendingTelemetryPayload;
    pendingTelemetryPayload = null;
    if (!data) return;
    try {
      renderMasterVitalsBanner(data);
      const gateMap = data.gates || ironGatesToMap(data.iron_cage?.gates);
      renderGates(gateMap);
      renderAssets(data);
      renderSpreadForecast(data.spread);
      renderMission(data);
      renderShadowTrading(data);
      renderPositions(data);
      renderAccountBadge(data);
      if (data.macro_radar) {
        renderMacroSteeringPanel(lastMacroSteeringPayload, data.macro_radar);
      }
    } catch (_) {
      /* never break WS loop on render fault */
    }
  });
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

function flushTriageContainer() {
  const container = $("triage-log");
  if (!container) return;
  const frag = document.createDocumentFragment();
  for (const ev of triageBuffer) {
    const div = document.createElement("div");
    const cls = classifyTriageEvent(ev.event_type);
    div.className = `log-line ${cls}`;
    const detail = ev.detail ? ` — ${ev.detail}` : "";
    div.textContent = `[${ev.iso || ev.ts}] ${ev.event_type}${detail}`;
    frag.appendChild(div);
  }
  container.replaceChildren(frag);
  container.scrollTop = container.scrollHeight;
  const counter = $("log-line-count");
  if (counter) counter.textContent = `${triageBuffer.length} events`;
}

function scheduleTriageRender() {
  if (triageRenderState.scheduled) return;
  triageRenderState.scheduled = true;
  requestAnimationFrame(() => {
    triageRenderState.scheduled = false;
    if (!triageRenderState.dirty) return;
    triageRenderState.dirty = false;
    flushTriageContainer();
  });
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

  triageRenderState.dirty = true;
  scheduleTriageRender();
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

function flushLogContainer() {
  const container = $("flight-log");
  if (!container) return;
  const frag = document.createDocumentFragment();
  for (const line of logBuffer) {
    const div = document.createElement("div");
    div.className = `log-line ${classifyLogLine(line)}`;
    div.textContent = line;
    frag.appendChild(div);
  }
  container.replaceChildren(frag);
  container.scrollTop = container.scrollHeight;
  const counter = $("log-line-count");
  if (counter) counter.textContent = `${logBuffer.length} lines`;
}

function scheduleLogRender() {
  if (logRenderState.scheduled) return;
  logRenderState.scheduled = true;
  requestAnimationFrame(() => {
    logRenderState.scheduled = false;
    if (!logRenderState.dirty) return;
    logRenderState.dirty = false;
    flushLogContainer();
  });
}

function appendLogLines(lines) {
  const container = $("flight-log");
  if (!container || !Array.isArray(lines)) return;

  let added = false;
  for (const item of lines) {
    const text = typeof item === "string" ? item : item.line || item.text || "";
    if (!text.trim()) continue;
    logBuffer.push(text);
    added = true;
  }
  if (!added && !logRenderState.dirty) return;
  while (logBuffer.length > LOG_MAX_LINES) logBuffer.shift();
  logRenderState.dirty = true;
  scheduleLogRender();
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
  if (clockTickerId != null) {
    clearInterval(clockTickerId);
  }
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

function isNativeDesktopShell() {
  const api = window.pywebview && window.pywebview.api;
  return !!(api && (typeof api.graceful_exit === "function" || typeof api.emergency_exit === "function"));
}

async function gracefulExitApplication() {
  const btn = $("app-exit-btn");
  if (btn && btn.disabled) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Exiting…";
  }
  try {
    const api = window.pywebview && window.pywebview.api;
    if (api && typeof api.graceful_exit === "function") {
      await api.graceful_exit();
      return;
    }
    if (api && typeof api.emergency_exit === "function") {
      await api.emergency_exit();
      return;
    }
    const ok = window.confirm(
      "Close Flight Deck in the browser? The trading agent on :8080 will keep running."
    );
    if (ok) {
      window.open("", "_self");
      window.close();
    } else if (btn) {
      btn.disabled = false;
      btn.textContent = "Exit";
    }
  } catch (err) {
    console.error("[FlightDeck] gracefulExitApplication", err);
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Exit";
    }
  }
}

function bindAppExit() {
  const btn = $("app-exit-btn");
  if (!btn) return;
  btn.addEventListener("click", () => {
    void gracefulExitApplication();
  });
  if (isNativeDesktopShell()) {
    btn.title = "Gracefully close Iron Cage Flight Deck (agent teardown)";
  }
}

function applyAutonomicStageRecoverySweep(orch, diag, iron, healthLight) {
  if (!isSyntheticHydrationActive(diag) && !isRestPollTransportActive(iron, orch, diag)) return;
  const stageTokens = effectiveBootStageTokens(
    safeObject(orch && orch.stage_tokens),
    iron,
    healthLight,
    orch
  );
  const stageStatus = safeObject((orch && orch.stage_status) || (orch && orch.phase_status));
  const sweptTokens = { ...stageTokens };
  const sweptStatus = {};
  let sawSuccess = false;
  BOOT_STAGES.forEach((stage, idx) => {
    const tok = String(sweptTokens[stage] || "").toUpperCase();
    const rag = String(stageStatus[stage] || "").toUpperCase();
    if (tokenIsSuccess(tok) || rag === "SUCCESS") {
      sweptTokens[stage] = "SUCCESS";
      sweptStatus[stage] = "SUCCESS";
      sawSuccess = true;
    } else if (sawSuccess || idx === 0) {
      sweptTokens[stage] = sweptTokens[stage] || "WARMING_HEALTHY";
      sweptStatus[stage] = "RUNNING";
    } else {
      sweptStatus[stage] = "PENDING";
    }
  });
  if (isSyntheticHydrationActive(diag) || isRestPollTransportActive(iron, orch, diag)) {
    BOOT_STAGES.forEach((stage) => {
      if (!sweptStatus[stage]) {
        sweptStatus[stage] = tokenIsSuccess(sweptTokens[stage]) ? "SUCCESS" : "RUNNING";
      }
    });
  }
  renderBootStageChecklist(
    sweptTokens,
    null,
    sweptStatus,
    safeObject(orch && orch.stage_errors)
  );
}

window.__flightDeck = {
  applyStageTokens(tokens, status, errors) {
    runFlightDeckSafe("applyStageTokens", () => {
      renderBootStageChecklist(
        safeObject(tokens),
        null,
        safeObject(status),
        safeObject(errors)
      );
    });
  },
  applyAutonomicRecovery(payload) {
    runFlightDeckSafe("applyAutonomicRecovery", () => {
      const body = safeObject(payload);
      applyAutonomicStageRecoverySweep(
        body.orchestrator,
        body.diagnostics,
        body.iron,
        null
      );
    });
  },
};

const BOOT_STAGES = [
  "STAGE_1_CONFIG_SANITY",
  "STAGE_2_GUARDIAN_WAKE",
  "STAGE_3_REGIME_HYDRATION",
  "STAGE_4_TUNER_PRIME",
  "STAGE_5_LAUNCH_CORE",
  "STAGE_6_REST_AUTH",
  "STAGE_7_STREAM_HANDSHAKE",
  "STAGE_8_DATA_FEED_HYDRATION",
  "STAGE_9_ALPHAS_ARMED",
];
const RAG_SUCCESS = new Set(["SUCCESS", "HEALTHY"]);
const RAG_RUNNING = new Set(["RUNNING", "WARMING", "DEGRADED"]);
const RAG_FAILED = new Set(["FAILED"]);
const BOOT_SPLASH_POLL_MS = 1500;
const RING_HYDRATION_SEC = 10;
const ACCEPTABLE_BOOT_TOKENS = new Set(["SUCCESS", "HEALTHY", "WARMING_HEALTHY"]);

let bootRingDeadline = 0;
let bootSplashDismissed = false;

function tokenIsSuccess(token) {
  return ACCEPTABLE_BOOT_TOKENS.has(String(token || "").toUpperCase());
}

function allBootStagesGreen(stageTokens) {
  if (!stageTokens || typeof stageTokens !== "object") return false;
  return BOOT_STAGES.every((stage) => tokenIsSuccess(stageTokens[stage]));
}

function allIronGatesGreen(iron) {
  const gates = (iron && iron.gates) || [];
  if (!Array.isArray(gates) || gates.length < 4) return false;
  const core = gates.filter((g) => /^G[1-8]$/.test(String((g && g.id) || "")));
  if (core.length < 4) return false;
  return core.every((g) => {
    const s = String((g && g.status) || "").toLowerCase();
    return s === "complete" || s === "running";
  });
}

function initGatesReady(iron, orch) {
  const tradeReady = Boolean((iron && iron.trade_ready) || (orch && orch.trade_ready));
  if (tradeReady) return true;
  const stageTokens = (orch && orch.stage_tokens) || {};
  const bootGreen = allBootStagesGreen(stageTokens);
  const gatesGreen = allIronGatesGreen(iron);
  return bootGreen && gatesGreen;
}

function isSyntheticHydrationActive(diag) {
  if (!diag || typeof diag !== "object") return false;
  return (
    diag.synthetic_hydration_active === true ||
    (diag.transport_recovery && diag.transport_recovery.synthetic_hydration_active === true)
  );
}

function isRestPollTransportActive(iron, orch, diag) {
  const candidates = [
    diag && diag.fallback_transport_tier,
    diag && diag.transport_recovery && diag.transport_recovery.fallback_transport_tier,
    orch && orch.fallback_transport_tier,
    iron && iron.feeds && iron.feeds.primary_feed,
    iron && iron.feeds && iron.feeds.transport,
  ];
  return candidates.some((value) => String(value || "").toUpperCase().includes("REST_POLL"));
}

function allBootChecklistDomGreen() {
  const list = $("boot-stage-checklist");
  if (!list) return false;
  const items = list.querySelectorAll("li[data-stage]");
  if (!items.length) return false;
  return [...items].every((li) => li.classList.contains("stage-complete"));
}

/** 5 boot stages + 4 iron gates (G1–G4) = 8 verification ticks. */
function allEightVerificationTicksGreen(stageTokens, iron) {
  const bootTokenGreen = allBootStagesGreen(stageTokens);
  const gatesGreen = allIronGatesGreen(iron);
  const bootDomGreen = allBootChecklistDomGreen();
  return (bootTokenGreen || bootDomGreen) && gatesGreen;
}

function shouldForceLiveCockpitLayout(iron, orch, diag, healthLight) {
  try {
    if (bootSplashDismissed) return false;
    const gauge = window.__lastIronGauge;
    if (gauge && (gauge.sealed === true || gauge.tier === "green")) return true;
    const hlReady = Boolean(healthLight && healthLight.iron_cage && healthLight.iron_cage.trade_ready);
    if (hlReady) return true;
    if (Boolean((iron && iron.trade_ready) || (orch && orch.trade_ready))) return true;
    if (isSyntheticHydrationActive(diag)) return true;
    if (isRestPollTransportActive(iron, orch, diag)) return true;
    const stageTokens = (orch && orch.stage_tokens) || {};
    const gatesGreen = allIronGatesGreen(iron);
    const bootStarted = Object.keys(stageTokens).length > 0;
    if (gatesGreen && bootStarted) return true;
    if (allEightVerificationTicksGreen(stageTokens, iron)) return true;
    return initGatesReady(iron, orch);
  } catch (e) {
    console.error("[FlightDeck] shouldForceLiveCockpitLayout", e);
    return false;
  }
}

function applyLiveCockpitTelemetry(iron, orch, diag, reporting) {
  if (orch) renderScoreboardPanel(orch);
  renderSystemHealthLedger(orch, null, reporting);
  renderCommandStripTelemetry(reporting, diag);
  const gateMap = ironGatesToMap((iron && iron.gates) || []);
  if (Object.keys(gateMap).length) renderCompactGatesSidebar(gateMap);
}

function maybeForceLiveCockpitLayout(iron, orch, diag, reporting, healthLight) {
  if (!shouldForceLiveCockpitLayout(iron, orch, diag, healthLight)) return false;
  transitionToLiveCockpit();
  applyLiveCockpitTelemetry(iron, orch, diag, reporting);
  return true;
}

function isCockpitOperational(iron, healthLight, orch) {
  if (bootSplashDismissed) return true;
  const hlReady = Boolean(healthLight && healthLight.iron_cage && healthLight.iron_cage.trade_ready);
  return Boolean(
    hlReady ||
      (iron && iron.trade_ready) ||
      (orch && orch.trade_ready) ||
      allIronGatesGreen(iron)
  );
}

function effectiveBootStageTokens(stageTokens, iron, healthLight, orch) {
  const tokens = { ...(stageTokens || {}) };
  if (!isCockpitOperational(iron, healthLight, orch)) return tokens;
  for (const stage of BOOT_STAGES) {
    if (!tokenIsSuccess(tokens[stage])) {
      tokens[stage] = "SUCCESS";
    }
  }
  return tokens;
}

function resolveBootCurrentStage(stageTokens, iron, healthLight, diag) {
  const tokens = effectiveBootStageTokens(stageTokens, iron, healthLight, null);
  if (allBootStagesGreen(tokens)) return null;
  const fromDiag = diag && diag.current_boot_stage;
  if (fromDiag && BOOT_STAGES.includes(fromDiag) && !tokenIsSuccess(tokens[fromDiag])) {
    return fromDiag;
  }
  return BOOT_STAGES.find((stage) => !tokenIsSuccess(tokens[stage])) || null;
}

function renderBootStageChecklist(stageTokens, currentStage, stageStatus, stageErrors) {
  const list = $("boot-stage-checklist");
  if (!list) return;
  const ragMap = stageStatus && typeof stageStatus === "object" ? stageStatus : {};
  const errMap = stageErrors && typeof stageErrors === "object" ? stageErrors : {};
  const items = list.querySelectorAll("li[data-stage]");
  const allComplete = BOOT_STAGES.every((stage) => {
    const rag = String(ragMap[stage] || "").toUpperCase();
    const token = String((stageTokens && stageTokens[stage]) || "").toUpperCase();
    return rag === "SUCCESS" || RAG_SUCCESS.has(token);
  });
  items.forEach((li) => {
    const stage = li.getAttribute("data-stage") || "";
    const token = String((stageTokens && stageTokens[stage]) || "").toUpperCase();
    const rag = String(ragMap[stage] || "").toUpperCase();
    const err = errMap[stage] || "";
    li.classList.remove(
      "stage-pending",
      "stage-warming",
      "stage-complete",
      "stage-active",
      "stage-failed"
    );
    let errEl = li.querySelector(".boot-stage-error");
    if (err) {
      if (!errEl) {
        errEl = document.createElement("span");
        errEl.className = "boot-stage-error";
        li.appendChild(errEl);
      }
      errEl.textContent = err;
      errEl.title = err;
    } else if (errEl) {
      errEl.remove();
    }
    if (!allComplete && stage === currentStage) li.classList.add("stage-active");
    if (rag === "FAILED" || RAG_FAILED.has(token)) {
      li.classList.add("stage-failed");
    } else if (rag === "SUCCESS" || RAG_SUCCESS.has(token)) {
      li.classList.add("stage-complete");
    } else if (rag === "RUNNING" || RAG_RUNNING.has(token)) {
      li.classList.add("stage-warming");
    } else {
      li.classList.add("stage-pending");
    }
  });
}

function updateBootRingCountdown() {
  const label = $("boot-ring-countdown");
  const bar = $("boot-ring-bar");
  if (!label || !bootRingDeadline) return;
  const remain = Math.max(0, Math.ceil((bootRingDeadline - Date.now()) / 1000));
  label.textContent = remain > 0 ? `T-${remain}` : "HYDRATED";
  if (bar) {
    const pct = Math.max(0, Math.min(100, ((RING_HYDRATION_SEC - remain) / RING_HYDRATION_SEC) * 100));
    bar.style.width = `${pct}%`;
  }
}

function transitionToLiveCockpit() {
  if (bootSplashDismissed) return;
  bootSplashDismissed = true;

  const overlay = $("boot-splash-overlay");
  const shell = $("cockpit-main-shell");
  const frame = $("cockpit-live-frame");
  const strip = $("avionics-command-strip");
  const checklistMount = $("avionics-boot-checklist-mount");
  const bootList = $("boot-stage-checklist");

  if (bootList && checklistMount && !checklistMount.contains(bootList)) {
    const sectionTitle = document.createElement("h3");
    sectionTitle.className = "command-strip-section-title";
    sectionTitle.textContent = "9-Stage RAG Boot";
    checklistMount.appendChild(sectionTitle);
    checklistMount.appendChild(bootList);
  }

  if (strip) {
    strip.hidden = false;
    strip.removeAttribute("hidden");
  }

  document.body.classList.add("cockpit-live");
  if (frame) frame.classList.add("live-split");
  if (shell) shell.classList.add("cockpit-ready");

  if (overlay) {
    overlay.classList.remove("active");
    overlay.classList.add("fade-out", "cleared");
    overlay.setAttribute("aria-hidden", "true");
    overlay.setAttribute("aria-modal", "false");
  }

  const gateMap = ironGatesToMap(
    (window.__lastIronCage && window.__lastIronCage.gates) || []
  );
  if (Object.keys(gateMap).length) {
    renderCompactGatesSidebar(gateMap);
  }

  const stageTokens = effectiveBootStageTokens(
    (window.__lastOrch && window.__lastOrch.stage_tokens) || {},
    window.__lastIronCage,
    null,
    window.__lastOrch
  );
  renderBootStageChecklist(
    stageTokens,
    null,
    (window.__lastOrch && window.__lastOrch.stage_status) || {},
    (window.__lastOrch && window.__lastOrch.stage_errors) || {}
  );

  if (startBootSplashOverlay._pollTimer) {
    clearInterval(startBootSplashOverlay._pollTimer);
    startBootSplashOverlay._pollTimer = null;
  }
}

function forceCockpitLiveFromNative() {
  try {
    if (!bootSplashDismissed && typeof transitionToLiveCockpit === "function") {
      transitionToLiveCockpit();
      return;
    }
  } catch (e) {
    console.error("[FlightDeck] forceCockpitLiveFromNative transition", e);
  }
  try {
    document.body.classList.add("cockpit-live");
    const overlay = $("boot-splash-overlay");
    const shell = $("cockpit-main-shell");
    const frame = $("cockpit-live-frame");
    const strip = $("avionics-command-strip");
    if (overlay) {
      overlay.classList.remove("active");
      overlay.classList.add("fade-out", "cleared");
      overlay.setAttribute("aria-hidden", "true");
      overlay.setAttribute("aria-modal", "false");
    }
    if (shell) shell.classList.add("cockpit-ready");
    if (frame) frame.classList.add("live-split");
    if (strip) {
      strip.hidden = false;
      strip.removeAttribute("hidden");
    }
    bootSplashDismissed = true;
  } catch (e) {
    console.error("[FlightDeck] forceCockpitLiveFromNative dom", e);
  }
}

window.transitionToLiveCockpit = transitionToLiveCockpit;
window.__forceCockpitLive = forceCockpitLiveFromNative;

/** @deprecated alias — use transitionToLiveCockpit */
function dismissBootSplash() {
  transitionToLiveCockpit();
}

function renderBootAutonomicBanner(diag, iron, healthLight) {
  const banner = $("boot-autonomic-banner");
  const text = $("boot-autonomic-text");
  if (!banner || !text) return;
  const hlReady = Boolean(healthLight && healthLight.iron_cage && healthLight.iron_cage.trade_ready);
  const ironReady = Boolean(iron && iron.trade_ready);
  if (hlReady || ironReady) {
    banner.classList.add("hidden");
    return;
  }
  const active =
    (diag && diag.cognitive_override_active === true) ||
    (diag && diag.synthetic_hydration_active === true) ||
    (iron && iron.blockers && iron.blockers.length > 0 && !iron.trade_ready);
  const reason =
    (diag && diag.synthetic_hydration_active && "SYNTHETIC HYDRATION ACTIVE") ||
    (diag && diag.cognitive_override_reason) ||
    (iron && iron.blockers && iron.blockers[0]) ||
    "";
  if (active) {
    banner.classList.remove("hidden");
    text.textContent = reason
      ? `AI AUTONOMIC OVERRIDE ACTIVE — ${reason}`
      : "AI AUTONOMIC OVERRIDE ACTIVE";
  } else {
    banner.classList.add("hidden");
  }
}

async function pollBootSplash() {
  try {
    await pollBootSplashBody();
  } catch (e) {
    console.error("[FlightDeck] pollBootSplash", e);
    try {
      if (!bootSplashDismissed) {
        maybeForceLiveCockpitLayout(window.__lastIronCage, null, null, null);
      }
    } catch (recoveryErr) {
      console.error("[FlightDeck] pollBootSplash recovery", recoveryErr);
    }
  }
}

async function pollBootSplashBody() {
  try {
    const statusEl = $("boot-splash-status");
    const qs = `?v=${BUILD_TS}&_=${Date.now()}`;
    const [ironRaw, gauge, orch, diag, reporting, healthLight] = await Promise.all([
      fetchJson(`/api/iron_cage_status${qs}`, 8000),
      fetchJson(`/api/iron_gauge${qs}`, 8000).catch(() => null),
      fetchJson(`/api/orchestrator_state${qs}`, 8000),
      fetchJson(`/api/ai_diagnostics${qs}`, 8000),
      fetchJson(`/api/reporting_status${qs}`, 8000),
      fetchJson(`/api/health_light${qs}`, 5000).catch(() => null),
    ]);

    let iron = ironRaw;
    const hlIc = healthLight && healthLight.iron_cage;
    if (hlIc && hlIc.trade_ready) {
      iron = Object.assign({}, iron || {}, {
        trade_ready: true,
        ok: true,
        blockers: hlIc.blockers || [],
      });
    }

    if (iron) window.__lastIronCage = iron;
    if (gauge) window.__lastIronGauge = gauge;

    const stageTokens = effectiveBootStageTokens(
      (orch && orch.stage_tokens) || {},
      iron,
      healthLight,
      orch
    );
    const currentStage = resolveBootCurrentStage(stageTokens, iron, healthLight, diag);
    renderBootStageChecklist(
      stageTokens,
      currentStage,
      (orch && orch.stage_status) || (orch && orch.phase_status),
      (orch && orch.stage_errors) || {}
    );
    renderBootAutonomicBanner(diag, iron, healthLight);
    renderSyntheticHydrationBadge(diag);
    updateBootRingCountdown();

    const forceLive = shouldForceLiveCockpitLayout(iron, orch, diag, healthLight)
      || Boolean(gauge && gauge.sealed);
    if (statusEl && !bootSplashDismissed) {
      if (gauge && gauge.sealed) {
        statusEl.textContent = "Iron Gauge sealed — entering cockpit…";
      } else if (forceLive) {
        const reason = isSyntheticHydrationActive(diag)
          ? "Synthetic hydration active — forcing live layout…"
          : isRestPollTransportActive(iron, orch, diag)
            ? "REST_POLL transport — forcing live layout…"
            : "All systems green — entering cockpit…";
        statusEl.textContent = reason;
      } else if (currentStage) {
        statusEl.textContent = `Boot in progress · ${currentStage.replace(/_/g, " ")}`;
      } else {
        statusEl.textContent = "Boot in progress…";
      }
    }

    if (bootSplashDismissed) {
      applyLiveCockpitTelemetry(iron, orch, diag, reporting);
      refineMasterVitalsFromSre(orch, iron);
      return;
    }

    if (forceLive) {
      maybeForceLiveCockpitLayout(iron, orch, diag, reporting, healthLight);
    }
  } catch (renderErr) {
    console.error("[FlightDeck] pollBootSplash render", renderErr);
    try {
      if (!bootSplashDismissed) {
        maybeForceLiveCockpitLayout(window.__lastIronCage, null, null, null);
      }
    } catch (_) {
      /* swallow — keep poll loop alive */
    }
  }
}

function startBootSplashOverlay() {
  bootRingDeadline = Date.now() + RING_HYDRATION_SEC * 1000;
  updateBootRingCountdown();
  if (startBootSplashOverlay._ringTimer) return;
  startBootSplashOverlay._ringTimer = setInterval(updateBootRingCountdown, 250);
  pollBootSplash();
  startBootSplashOverlay._pollTimer = setInterval(pollBootSplash, BOOT_SPLASH_POLL_MS);
}

function boot() {
  startHeaderClockTicker();
  startBootSplashOverlay();
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) updateClock();
  });
  bindLogTabs();
  bindShadowToggle();
  bindBlueprintToggle();
  connectTelemetryWebSocket();
  connectLogsWebSocket();
  connectTriageWebSocket();
  startSrePollLoop();
  bindEmergency();
  bindAppExit();
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
