"""
IG Agent Flight Deck — native tkinter avionics cockpit (main-thread host).

Run via cockpit.launcher on the primary process main thread; drains in-process
telemetry via queue.Queue.get_nowait() (put_drop_oldest bounded queue).
"""

from __future__ import annotations

import queue
import tkinter as tk
from tkinter import ttk
from typing import Any

# Aviation / cyberpunk palette
BG = "#0a0a0f"
PANEL = "#12121a"
FG = "#c8d0d8"
NEON_GREEN = "#00ff41"
AMBER = "#ffb000"
RED = "#ff0033"
DIM = "#4a5568"
CYAN = "#00e5ff"


class FlightDeckApp:
    """Native dark-theme cockpit HUD."""

    def __init__(
        self,
        telemetry_q: Any,
        command_q: Any,
        *,
        title: str = "IG Agent Flight Deck v29.1",
    ) -> None:
        self._telemetry_q = telemetry_q
        self._command_q = command_q
        self._flash_on = True
        self._last_payload: dict[str, Any] = {}

        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg=BG)
        self.root.geometry("1280x820")
        self.root.minsize(960, 640)

        self._build_layout()
        self._prime_macos_canvas()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(0, self._initial_paint)
        self.root.after(200, self._poll_telemetry)
        self.root.after(500, self._toggle_flash)

    def _prime_macos_canvas(self) -> None:
        """Force window-manager geometry registration before first paint (macOS)."""
        self.root.update_idletasks()
        self.root.update()
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(50, lambda: self.root.attributes("-topmost", False))
        except tk.TclError:
            pass

    def _initial_paint(self) -> None:
        """Seed panels with placeholder telemetry so grids are visible immediately."""
        placeholder = {
            "gates": {
                gid: {"status": "pending"}
                for gid in ("G1", "G2", "G3", "G4")
            },
            "micro_regime": "NEUTRAL",
            "micro_confidence": 0.0,
            "autopilot_rating": 0.0,
            "target_mission": {
                "p_day_gbp": 0.0,
                "target_daily_gbp": 1000.0,
                "mission_progress_pct": 0.0,
                "capital_preservation": False,
                "risk_compression_factor": 1.0,
            },
            "spread": {},
            "epics": {},
            "positions": [],
        }
        self._last_payload = placeholder
        self._render(placeholder)
        self.root.update_idletasks()

    def _styled_frame(self, parent: tk.Misc, *, title: str) -> tuple[tk.Frame, tk.Frame]:
        """Return (outer, body) — caller must pack *outer* into parent."""
        outer = tk.Frame(parent, bg=PANEL, highlightbackground=CYAN, highlightthickness=1)
        lbl = tk.Label(
            outer,
            text=title.upper(),
            bg=PANEL,
            fg=CYAN,
            font=("Menlo", 10, "bold"),
            anchor="w",
        )
        lbl.pack(fill="x", padx=8, pady=(6, 2))
        body = tk.Frame(outer, bg=PANEL)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        return outer, body

    def _build_layout(self) -> None:
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="both", expand=True, padx=8, pady=8)

        left = tk.Frame(top, bg=BG)
        right = tk.Frame(top, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, 4))
        right.pack(side="right", fill="both", expand=True, padx=(4, 0))

        # Panel A — Pre-flight gates
        gate_outer, gate_body = self._styled_frame(left, title="Panel A — Pre-Flight Gate Checklist")
        gate_outer.pack(fill="x", pady=(0, 6))
        self._gate_labels: dict[str, tk.Label] = {}
        for gid, label in (
            ("G1", "Gate 1 · Environment"),
            ("G2", "Gate 2 · REST Auth"),
            ("G3", "Gate 3 · Streaming"),
            ("G4", "Gate 4 · State Hydration"),
        ):
            row = tk.Frame(gate_body, bg=PANEL)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, bg=PANEL, fg=FG, font=("Menlo", 9), width=28, anchor="w").pack(
                side="left"
            )
            status = tk.Label(row, text="PENDING", bg=PANEL, fg=AMBER, font=("Menlo", 9, "bold"))
            status.pack(side="right")
            self._gate_labels[gid] = status

        # Panel C — Microstructure
        micro_outer, micro_body = self._styled_frame(left, title="Panel C — Microstructure Flight Vector")
        micro_outer.pack(fill="both", expand=True, pady=6)
        self._micro_regime = tk.Label(
            micro_body, text="REGIME: NEUTRAL", bg=PANEL, fg=NEON_GREEN, font=("Menlo", 14, "bold")
        )
        self._micro_regime.pack(anchor="w", pady=4)
        self._micro_conf = tk.Label(
            micro_body, text="Confidence: —", bg=PANEL, fg=FG, font=("Menlo", 11)
        )
        self._micro_conf.pack(anchor="w")
        self._autopilot = tk.Label(
            micro_body,
            text="Autopilot Confidence Rating: —",
            bg=PANEL,
            fg=CYAN,
            font=("Menlo", 11, "bold"),
        )
        self._autopilot.pack(anchor="w", pady=6)
        self._mission = tk.Label(
            micro_body,
            text="Daily Mission Progress: —",
            bg=PANEL,
            fg=NEON_GREEN,
            font=("Menlo", 11, "bold"),
        )
        self._mission.pack(anchor="w", pady=(4, 2))
        self._mission_bar = tk.Canvas(micro_body, height=14, bg=BG, highlightthickness=0)
        self._mission_bar.pack(fill="x", pady=(0, 6))
        self._victory_banner = tk.Label(
            micro_body,
            text="",
            bg=PANEL,
            fg=NEON_GREEN,
            font=("Menlo", 13, "bold"),
            wraplength=560,
            justify="center",
        )
        self._victory_banner.pack(fill="x", pady=(4, 0))
        self._mission_accomplished = False

        # Panel B — Avionics HUD
        av_outer, av_body = self._styled_frame(right, title="Panel B — Avionics HUD & Telemetry")
        av_outer.pack(fill="both", expand=True, pady=(0, 6))
        self._turbulence_banner = tk.Label(
            av_body,
            text="",
            bg=PANEL,
            fg=RED,
            font=("Menlo", 12, "bold"),
        )
        self._turbulence_banner.pack(fill="x", pady=(0, 6))
        cols = ("epic", "spread", "z", "throttle")
        self._epic_tree = ttk.Treeview(av_body, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (220, 80, 60, 80)):
            self._epic_tree.heading(c, text=c.upper())
            self._epic_tree.column(c, width=w, anchor="center")
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background=PANEL,
            foreground=FG,
            fieldbackground=PANEL,
            rowheight=22,
        )
        style.configure("Treeview.Heading", background=BG, foreground=CYAN)
        self._epic_tree.pack(fill="both", expand=True)

        # Panel D — Positions
        pos_outer, pos_body = self._styled_frame(right, title="Panel D — Multi-Position Flight Tracker")
        pos_outer.pack(fill="both", expand=True, pady=6)
        pcols = ("epic", "side", "entry", "mkt", "stop", "trail")
        self._pos_tree = ttk.Treeview(pos_body, columns=pcols, show="headings", height=6)
        for c, w in zip(pcols, (160, 50, 80, 80, 80, 70)):
            self._pos_tree.heading(c, text=c.upper())
            self._pos_tree.column(c, width=w, anchor="center")
        self._pos_tree.pack(fill="both", expand=True)

        # Panel E — Emergency (Canvas: only reliable way to force colours on macOS Aqua)
        bottom = tk.Frame(self.root, bg=BG)
        bottom.pack(fill="x", padx=8, pady=(0, 8))
        self._emergency_canvas = tk.Canvas(
            bottom,
            height=52,
            bg=RED,
            highlightthickness=2,
            highlightbackground="#cc0028",
            borderwidth=0,
            cursor="hand2",
        )
        self._emergency_canvas.pack(fill="x")
        self._emergency_text = "⚠ EMERGENCY COCKPIT OVERRIDE / FLATTEN POSITIONS ⚠"
        self._emergency_text_id = self._emergency_canvas.create_text(
            0,
            26,
            text=self._emergency_text,
            fill="#ffffff",
            font=("Menlo", 14, "bold"),
            anchor="w",
        )
        self._emergency_active = True
        self._emergency_canvas.bind("<Configure>", self._on_emergency_canvas_resize)
        self._emergency_canvas.bind("<Button-1>", lambda _e: self._on_emergency())
        self._emergency_canvas.bind("<Enter>", lambda _e: self._emergency_hover(True))
        self._emergency_canvas.bind("<Leave>", lambda _e: self._emergency_hover(False))

    def _on_emergency_canvas_resize(self, event: tk.Event) -> None:
        pad = 12
        self._emergency_canvas.coords(self._emergency_text_id, pad, event.height // 2)
        self._emergency_canvas.itemconfigure(
            self._emergency_text_id, width=max(0, event.width - pad * 2)
        )

    def _emergency_hover(self, active: bool) -> None:
        if not self._emergency_active:
            return
        color = "#cc0028" if active else RED
        self._emergency_canvas.configure(bg=color, highlightbackground=color)

    def _on_emergency(self) -> None:
        if not self._emergency_active:
            return
        try:
            self._command_q.put_nowait("EMERGENCY_FLATTEN")
            self._emergency_active = False
            self._emergency_canvas.configure(bg=DIM, highlightbackground=DIM, cursor="arrow")
            self._emergency_canvas.itemconfigure(
                self._emergency_text_id,
                text="OVERRIDE SENT — FLATTEN IN PROGRESS",
                fill=FG,
            )
        except queue.Full:
            pass

    def _on_close(self) -> None:
        self.root.destroy()

    def _toggle_flash(self) -> None:
        self._flash_on = not self._flash_on
        self._render_gates(self._last_payload)
        self.root.after(500, self._toggle_flash)

    def _poll_telemetry(self) -> None:
        latest: dict[str, Any] | None = None
        try:
            while True:
                latest = self._telemetry_q.get_nowait()
        except queue.Empty:
            pass
        except Exception:
            latest = None
        if latest is not None:
            self._last_payload = latest
            self._render(latest)
        self.root.after(150, self._poll_telemetry)

    def _gate_color(self, status: str) -> str:
        s = str(status or "").lower()
        if s in ("complete", "running"):
            return NEON_GREEN if self._flash_on else "#00aa2a"
        if s == "failed":
            return RED
        return AMBER if self._flash_on else "#996600"

    def _render_gates(self, payload: dict[str, Any]) -> None:
        gates = payload.get("gates") or {}
        for gid, lbl in self._gate_labels.items():
            g = gates.get(gid) or {}
            status = str(g.get("status") or "pending")
            lbl.configure(text=status.upper(), fg=self._gate_color(status))

    def _render(self, payload: dict[str, Any]) -> None:
        self._render_gates(payload)

        regime = str(payload.get("micro_regime") or "NEUTRAL")
        conf = float(payload.get("micro_confidence") or 0.0)
        rating = float(payload.get("autopilot_rating") or 0.0)
        self._micro_regime.configure(text=f"REGIME: {regime}")
        self._micro_conf.configure(text=f"Confidence: {conf:.0%}")
        self._autopilot.configure(text=f"Autopilot Confidence Rating: {rating:.0f}%")

        mission = payload.get("target_mission") or {}
        p_day = float(mission.get("p_day_gbp") or 0.0)
        target = float(mission.get("target_daily_gbp") or 1000.0)
        pct = float(mission.get("mission_progress_pct") or 0.0)
        preservation = bool(mission.get("capital_preservation"))
        factor = float(mission.get("risk_compression_factor") or 1.0)
        mission_fg = RED if preservation else (AMBER if pct >= 75 else NEON_GREEN)
        self._mission.configure(
            text=(
                f"Daily Mission Progress: {pct:.0f}% "
                f"(£{p_day:,.2f} / £{target:,.0f})"
                + (" — CAPITAL PRESERVATION" if preservation else f" · factor {factor:.2f}")
            ),
            fg=mission_fg if self._flash_on or preservation else mission_fg,
        )
        self._mission_bar.delete("all")
        w = max(self._mission_bar.winfo_width(), 200)
        fill_w = int(w * min(1.0, pct / 100.0))
        self._mission_bar.create_rectangle(0, 0, w, 14, fill=BG, outline=DIM)
        if fill_w > 0:
            bar_color = RED if preservation else (AMBER if pct >= 75 else NEON_GREEN)
            self._mission_bar.create_rectangle(0, 0, fill_w, 14, fill=bar_color, outline="")

        accomplished = bool(mission.get("mission_accomplished")) or (
            preservation and p_day >= target
        )
        if accomplished:
            self._mission_accomplished = True
        if self._mission_accomplished:
            banner_fg = NEON_GREEN if self._flash_on else "#00cc33"
            self._victory_banner.configure(
                text="★ MISSION ACCOMPLISHED: £1,000 TARGET SECURED ★",
                fg=banner_fg,
                bg="#0a1a0a" if self._flash_on else PANEL,
            )
        else:
            self._victory_banner.configure(text="", bg=PANEL)

        turbulence = False
        for _epic, row in (payload.get("spread") or {}).items():
            if isinstance(row, dict) and row.get("turbulence"):
                turbulence = True
                break
        if turbulence:
            self._turbulence_banner.configure(
                text="⚠ TURBULENCE / SPREAD THROTTLE — EXECUTION DEGRADED ⚠",
                fg=RED if self._flash_on else AMBER,
            )
        else:
            self._turbulence_banner.configure(text="SPREAD FORECAST: CLEAR", fg=NEON_GREEN)

        for item in self._epic_tree.get_children():
            self._epic_tree.delete(item)
        spread_map = payload.get("spread") or {}
        epic_map = payload.get("epics") or {}
        for epic in sorted(set(list(spread_map.keys()) + list(epic_map.keys()))):
            sp = spread_map.get(epic) or {}
            q = epic_map.get(epic) or {}
            spr = sp.get("spread", q.get("spread", 0))
            self._epic_tree.insert(
                "",
                "end",
                values=(
                    epic[-18:],
                    f"{float(spr):.2f}",
                    f"{float(sp.get('z_score', 0)):.2f}",
                    f"{float(sp.get('throttle', 0)):.2f}",
                ),
            )

        for item in self._pos_tree.get_children():
            self._pos_tree.delete(item)
        for row in payload.get("positions") or []:
            if not isinstance(row, dict):
                continue
            self._pos_tree.insert(
                "",
                "end",
                values=(
                    str(row.get("epic", ""))[-14:],
                    row.get("side", ""),
                    f"{float(row.get('entry', 0)):.2f}",
                    f"{float(row.get('market', 0)):.2f}",
                    f"{float(row.get('stop', 0)):.2f}",
                    f"{float(row.get('trail_pts', 0)):.1f}",
                ),
            )

    def run(self) -> None:
        self.root.mainloop()


def run_flight_deck_main_thread(telemetry_q: Any, command_q: Any) -> None:
    """Entry for main-thread Flight Deck (macOS tkinter paint requirement)."""
    app = FlightDeckApp(telemetry_q, command_q)
    app.run()


def run_flight_deck_process(telemetry_q: Any, command_q: Any) -> None:
    """Backward-compatible alias — prefer run_flight_deck_main_thread."""
    run_flight_deck_main_thread(telemetry_q, command_q)
