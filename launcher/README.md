# Legacy launchers — RETIRED

**Canonical product:** `Trading_Desk.app` at repo root (also installed to Desktop via `scripts/install_trading_desk_app.sh`).

```bash
# Install / refresh Desktop app
./scripts/install_trading_desk_app.sh

# Or silent launch
./scripts/trading_desk_silent.sh
```

Opens **Quantum Terminal** on `:3000` with agent API on `:8080` and config
`config/config_v31_demo_throughput.json`.

The apps under `launcher/` (Flight Deck, v29.0, Apex Cockpit) are historical
bundles. Prefer Desktop `Trading_Desk.app`. `flight_deck_launch.sh` redirects
here automatically.

## Desk UI always-on (viewer only)

Keeps the **Quantum Terminal (:3000)** and **Trading Desk pywebview shell** running
across logout/login and after the window is closed. Does **not** start, stop, or
signal trading agents on `:8080`/`:8081`.

| Piece | Role |
|-------|------|
| `com.igagent.trading_desk` | LaunchAgent KeepAlive → `scripts/trading_desk_viewer_keepalive.sh` (shell) |
| `com.igagent.v30.ui` | Existing LaunchAgent → `scripts/ui_terminal_daemon.sh` (:3000) |

```bash
# Enable (safe while agents are live / A2 CFD pause)
./scripts/install_trading_desk_always_on.sh --enable

# Or refresh Desktop app + enable
./scripts/install_trading_desk_app.sh --with-always-on

# Status / disable
./scripts/install_trading_desk_always_on.sh --status
./scripts/install_trading_desk_always_on.sh --disable          # shell KeepAlive only
./scripts/install_trading_desk_always_on.sh --disable-all-ui   # shell + :3000 UI agent
```

Plists land in `~/Library/LaunchAgents/`. Closing the Desk window relaunches the
shell within ~10s (`ThrottleInterval`). Agents remain untouched.
