"""
Cyberpunk desktop splash — 9-stage launcher checklist + embedded terminal viewport.
"""

from __future__ import annotations

from typing import Any

# 9-stage cryptographic verification checklist (mirrors macos/launcher launcher_status.sh)
LAUNCHER_STAGES: tuple[dict[str, str], ...] = (
    {"id": "shutdown", "label": "01", "title": "Clean Shutdown", "detail": "Port eviction & manual stop hold"},
    {"id": "preflight", "label": "02", "title": "Preflight", "detail": "Config sanity & session lock"},
    {"id": "tests", "label": "03", "title": "Smoke Tests", "detail": "Pytest assessment gate"},
    {"id": "agent_boot", "label": "04", "title": "Agent Boot", "detail": "Interpreter genesis & REST bind"},
    {"id": "g5", "label": "05", "title": "Boot Gates G5", "detail": "Operational readiness contract"},
    {"id": "post_ready", "label": "06", "title": "Execution Plane", "detail": "Trade-ready routing & armed loops"},
    {"id": "warmup", "label": "07", "title": "Warm-up", "detail": "Unified execution route priming"},
    {"id": "verify", "label": "08", "title": "Verification", "detail": "Health + GUI status field audit"},
    {"id": "gui", "label": "09", "title": "Flight Deck", "detail": "Iron Cage cockpit embed"},
)

ORCHESTRATOR_STAGES: tuple[dict[str, str], ...] = (
    {"id": "STAGE_1_CONFIG_SANITY", "title": "Config"},
    {"id": "STAGE_2_GUARDIAN_WAKE", "title": "Guardian"},
    {"id": "STAGE_3_REGIME_HYDRATION", "title": "Rings"},
    {"id": "STAGE_4_TUNER_PRIME", "title": "Tuner"},
    {"id": "STAGE_5_LAUNCH_CORE", "title": "Core"},
    {"id": "STAGE_6_REST_AUTH", "title": "REST"},
    {"id": "STAGE_7_STREAM_HANDSHAKE", "title": "Stream"},
    {"id": "STAGE_8_DATA_FEED_HYDRATION", "title": "Feeds"},
    {"id": "STAGE_9_ALPHAS_ARMED", "title": "Alphas"},
)

STAGE_ID_TO_INDEX: dict[str, int] = {row["id"]: idx for idx, row in enumerate(LAUNCHER_STAGES)}
STAGE_ID_TO_INDEX["ready"] = len(LAUNCHER_STAGES) - 1
STAGE_ID_TO_INDEX["init"] = -1

_ORCH_TOKEN_ACTIVE = frozenset({"SUCCESS", "WARMING", "WARMING_HEALTHY", "HEALTHY"})
_ORCH_TOKEN_WARMING = frozenset({"WARMING", "WARMING_HEALTHY", "DEGRADED"})


def launcher_stage_index(stage: str, step: int = 0) -> int:
    key = str(stage or "").strip().lower()
    if key in STAGE_ID_TO_INDEX:
        idx = STAGE_ID_TO_INDEX[key]
        return max(0, idx)
    if step > 0:
        return max(0, min(len(LAUNCHER_STAGES) - 1, int(step) - 1))
    return 0


def orchestrator_segment_states(stage_tokens: dict[str, Any] | None) -> list[str]:
    """Return per-segment visual state: pending | warming | active."""
    tokens = stage_tokens or {}
    out: list[str] = []
    for row in ORCHESTRATOR_STAGES:
        raw = str(tokens.get(row["id"]) or "").upper()
        if raw in _ORCH_TOKEN_ACTIVE:
            out.append("warming" if raw in _ORCH_TOKEN_WARMING else "active")
        else:
            out.append("pending")
    return out


def build_splash_html() -> str:
    stage_rows = "\n".join(
        f'''<li class="stage-item" data-stage="{row["id"]}" data-index="{i}">
          <span class="stage-tick" aria-hidden="true"></span>
          <span class="stage-code">{row["label"]}</span>
          <span class="stage-copy">
            <span class="stage-title">{row["title"]}</span>
            <span class="stage-detail">{row["detail"]}</span>
          </span>
        </li>'''
        for i, row in enumerate(LAUNCHER_STAGES)
    )
    orch_segments = "\n".join(
        f'<div class="orch-seg" data-orch="{i}"><span class="orch-label">{row["title"]}</span></div>'
        for i, row in enumerate(ORCHESTRATOR_STAGES)
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=1440, height=900" />
<title>Iron Cage — Flight Deck Control Center</title>
<style>
:root {{
  --bg: #0D0E12;
  --slate: #64748b;
  --emerald: #10B981;
  --amber: #F59E0B;
  --text: #e2e8f0;
  --mono: "SF Mono", "Menlo", "JetBrains Mono", ui-monospace, monospace;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{
  width: 100%; height: 100%; overflow: hidden;
  background: var(--bg); color: var(--text);
  font-family: Inter, system-ui, sans-serif;
}}
.shell {{
  display: flex; flex-direction: column; height: 100vh;
  padding: 28px 32px 18px;
  background: radial-gradient(ellipse 80% 50% at 50% -10%, rgba(16,185,129,0.08), transparent),
              var(--bg);
}}
.shell-header {{
  display: flex; align-items: flex-end; justify-content: space-between;
  margin-bottom: 18px;
}}
.brand h1 {{
  font-size: 1.35rem; font-weight: 800; letter-spacing: 0.06em;
  text-transform: uppercase;
}}
.brand p {{
  margin-top: 6px; font-size: 0.72rem; color: var(--slate); letter-spacing: 0.12em;
}}
.shell-header-actions {{
  display: flex; align-items: center; gap: 10px;
}}
.shell-exit-btn {{
  font-family: var(--mono); font-size: 0.62rem; font-weight: 700;
  letter-spacing: 0.08em; text-transform: uppercase;
  padding: 7px 12px; border-radius: 8px; cursor: pointer;
  border: 1px solid rgba(148, 163, 184, 0.45);
  background: rgba(15, 17, 23, 0.85); color: #cbd5e1;
}}
.shell-exit-btn:hover {{
  border-color: rgba(248, 113, 113, 0.55);
  color: #fecaca;
  box-shadow: 0 0 16px rgba(248, 113, 113, 0.18);
}}
.shell-exit-btn:disabled {{
  opacity: 0.55; cursor: wait;
}}
.tier-pill {{
  font-family: var(--mono); font-size: 0.62rem; font-weight: 700;
  padding: 6px 12px; border-radius: 999px;
  border: 1px solid rgba(245,158,11,0.45);
  color: var(--amber);
  box-shadow: 0 0 18px rgba(245,158,11,0.22);
}}
.tier-pill.active {{
  border-color: rgba(16,185,129,0.55);
  color: var(--emerald);
  box-shadow: 0 0 22px rgba(16,185,129,0.35);
}}
.upper {{
  flex: 0 0 65%; display: flex; flex-direction: column; min-height: 0;
}}
.checklist {{
  flex: 1; overflow: auto; padding-right: 8px;
  list-style: none; display: flex; flex-direction: column; gap: 8px;
}}
.stage-item {{
  display: flex; align-items: flex-start; gap: 12px;
  padding: 10px 12px; border-radius: 10px;
  border: 1px solid rgba(100,116,139,0.18);
  background: rgba(15,17,23,0.65);
  color: var(--slate);
  transition: color 0.25s ease, border-color 0.25s ease, box-shadow 0.25s ease;
}}
.stage-item.active, .stage-item.complete {{
  color: var(--emerald);
  border-color: rgba(16,185,129,0.45);
  box-shadow: 0 0 20px rgba(16,185,129,0.12);
}}
.stage-item.warming {{
  color: var(--amber);
  border-color: rgba(245,158,11,0.45);
  box-shadow: 0 0 20px rgba(245,158,11,0.15);
}}
.stage-tick {{
  width: 14px; height: 14px; margin-top: 3px; border-radius: 3px;
  border: 1px solid currentColor; opacity: 0.35;
}}
.stage-item.complete .stage-tick::after,
.stage-item.active .stage-tick::after {{
  content: "✓"; display: block; font-size: 10px; line-height: 12px; text-align: center;
  font-weight: 800; letter-spacing: -0.08em;
}}
.stage-code {{
  font-family: var(--mono); font-size: 0.62rem; font-weight: 700;
  letter-spacing: 0.14em; min-width: 22px;
}}
.stage-copy {{ display: flex; flex-direction: column; gap: 2px; }}
.stage-title {{
  font-family: var(--mono); font-size: 0.78rem; font-weight: 700; letter-spacing: 0.04em;
}}
.stage-detail {{ font-size: 0.68rem; opacity: 0.85; }}
.orch-track {{
  margin-top: 14px; display: grid; grid-template-columns: repeat(5, 1fr); gap: 6px;
}}
.orch-seg {{
  height: 8px; border-radius: 4px;
  background: rgba(100,116,139,0.25);
  position: relative; overflow: hidden;
}}
.orch-seg.active {{
  background: rgba(16,185,129,0.35);
  box-shadow: 0 0 14px rgba(16,185,129,0.45);
  animation: pulse-emerald 1.4s ease-in-out infinite;
}}
.orch-seg.warming {{
  background: rgba(245,158,11,0.35);
  box-shadow: 0 0 14px rgba(245,158,11,0.4);
  animation: pulse-amber 1.2s ease-in-out infinite;
}}
.orch-label {{
  position: absolute; top: 12px; left: 0; right: 0; text-align: center;
  font-family: var(--mono); font-size: 0.52rem; letter-spacing: 0.08em; color: var(--slate);
}}
.orch-seg.active .orch-label, .orch-seg.warming .orch-label {{ color: var(--text); }}
@keyframes pulse-emerald {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0.55; }}
}}
@keyframes pulse-amber {{
  0%, 100% {{ opacity: 1; }}
  50% {{ opacity: 0.5; }}
}}
.lower {{
  flex: 0 0 35%; margin-top: 12px; min-height: 0;
  display: flex; flex-direction: column;
  border: 1px solid rgba(100,116,139,0.22);
  border-radius: 12px;
  background: rgba(2,4,8,0.92);
  box-shadow: inset 0 0 24px rgba(0,0,0,0.45);
}}
.terminal-head {{
  padding: 8px 12px; font-family: var(--mono); font-size: 0.58rem;
  letter-spacing: 0.1em; color: var(--slate);
  border-bottom: 1px solid rgba(100,116,139,0.18);
}}
#boot-terminal {{
  flex: 1; overflow-y: auto; padding: 10px 12px;
  font-family: var(--mono); font-size: 0.72rem; line-height: 1.45;
  color: #94a3b8; white-space: pre-wrap; word-break: break-word;
}}
#boot-terminal .line-err {{ color: #fca5a5; }}
#boot-terminal .line-warn {{ color: var(--amber); }}
#boot-terminal .line-ok {{ color: var(--emerald); }}
.status-line {{
  margin-top: 8px; font-family: var(--mono); font-size: 0.64rem; color: var(--slate);
}}
</style>
</head>
<body>
<div class="shell">
  <header class="shell-header">
    <div class="brand">
      <h1>Iron Cage — Flight Deck Control Center</h1>
      <p>CRYPTOGRAPHIC INITIALIZATION · 9-STAGE VERIFICATION</p>
    </div>
    <div class="shell-header-actions">
      <button type="button" class="shell-exit-btn" id="shell-exit-btn" title="Gracefully close Iron Cage Flight Deck">Exit</button>
      <div id="tier-pill" class="tier-pill">WARMING</div>
    </div>
  </header>
  <section class="upper" aria-label="Initialization checklist">
    <ol class="checklist" id="stage-checklist">
      {stage_rows}
    </ol>
    <div class="orch-track" id="orch-track" aria-label="5-stage orchestrator pulse">
      {orch_segments}
    </div>
    <p id="status-line" class="status-line">Awaiting launcher telemetry…</p>
  </section>
  <section class="lower" aria-label="Boot terminal stream">
    <div class="terminal-head">INIT STREAM · READ ONLY</div>
    <pre id="boot-terminal"></pre>
  </section>
</div>
<script>
window.__desktopShell = {{
  setStage(index, state) {{
    const items = document.querySelectorAll(".stage-item");
    items.forEach((el, i) => {{
      el.classList.remove("pending", "active", "warming", "complete");
      if (i < index) el.classList.add("complete");
      else if (i === index) el.classList.add(state || "active");
      else el.classList.add("pending");
    }});
  }},
  setOrchSegment(index, state) {{
    const seg = document.querySelector('.orch-seg[data-orch="' + index + '"]');
    if (!seg) return;
    seg.classList.remove("pending", "active", "warming");
    if (state) seg.classList.add(state);
  }},
  setTier(tier) {{
    const pill = document.getElementById("tier-pill");
    if (!pill) return;
    pill.textContent = (tier || "WARMING").toUpperCase();
    pill.classList.toggle("active", String(tier).toLowerCase() === "live");
  }},
  setStatus(text) {{
    const el = document.getElementById("status-line");
    if (el) el.textContent = text || "";
  }},
  appendTerminal(line, level) {{
    const term = document.getElementById("boot-terminal");
    if (!term) return;
    const span = document.createElement("span");
    span.className = level ? ("line-" + level) : "";
    span.textContent = line + "\\n";
    term.appendChild(span);
    while (term.childNodes.length > 400) term.removeChild(term.firstChild);
    term.scrollTop = term.scrollHeight;
  }},
  requestGracefulExit() {{
    const btn = document.getElementById("shell-exit-btn");
    if (btn) {{
      btn.disabled = true;
      btn.textContent = "Exiting…";
    }}
    this.appendTerminal("operator exit requested — graceful teardown", "warn");
    const api = window.pywebview && window.pywebview.api;
    if (api && typeof api.graceful_exit === "function") {{
      Promise.resolve(api.graceful_exit()).catch(function(err) {{
        console.error("[DesktopShell] graceful_exit", err);
        if (btn) {{
          btn.disabled = false;
          btn.textContent = "Exit";
        }}
      }});
      return;
    }}
    if (api && typeof api.emergency_exit === "function") {{
      Promise.resolve(api.emergency_exit()).catch(function() {{}});
      return;
    }}
    this.appendTerminal("pywebview bridge unavailable — cannot exit native shell", "err");
    if (btn) {{
      btn.disabled = false;
      btn.textContent = "Exit";
    }}
  }}
}};
document.getElementById("shell-exit-btn")?.addEventListener("click", function() {{
  window.__desktopShell && window.__desktopShell.requestGracefulExit();
}});
window.addEventListener("pywebviewready", function() {{
  window.__desktopShell.appendTerminal("[desktop] pywebview bridge ready", "ok");
}});
</script>
</body>
</html>"""
