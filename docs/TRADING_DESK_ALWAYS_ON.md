# Trading Desk UI always-on

Viewer/UI persistence only. Trading cores on `:8080`/`:8081` are never restarted by this path.

## What stays up

1. **Quantum Terminal** on `:3000` — `com.igagent.v30.ui` → `scripts/ui_terminal_daemon.sh`
2. **Trading Desk shell** (pywebview) — `com.igagent.trading_desk` → `scripts/trading_desk_viewer_keepalive.sh`

## Enable

```bash
./scripts/install_trading_desk_always_on.sh --enable
# or
./scripts/install_trading_desk_app.sh --with-always-on
```

## Disable

```bash
./scripts/install_trading_desk_always_on.sh --disable
# also stop :3000 LaunchAgent:
./scripts/install_trading_desk_always_on.sh --disable-all-ui
```

## Manual open (one-shot)

Desktop `Trading_Desk.app` or `./scripts/trading_desk_silent.sh` — when dual engines are already breathing, silent launch opens the shell only and does not tear down agents.

## Safety

Closing the Trading Desk window (Trading Desk native mode) exits the pywebview
shell only. It does **not** mark manual stop or kill agents. Always-on then
relaunches the shell via launchd KeepAlive.
