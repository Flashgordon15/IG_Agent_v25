---
name: preflight
description: Run the full pre-flight check sequence before a live trading session — pre_flight_check.py then e2e_platform_validation.py. Use before starting the agent overnight.
disable-model-invocation: false
---

Run the following two commands in sequence. Report the output of each, highlight any failures, and state whether the system is ready to trade.

1. Pre-flight checks (config, credentials, instance lock, connectivity):
```
PYTHONPATH=src python3 scripts/pre_flight_check.py --live
```

2. Full E2E platform validation (signal pipeline, order flow, dashboard API):
```
PYTHONPATH=src python3 scripts/e2e_platform_validation.py
```

3. Supervision drift check (launchd, watchdog, manual_stop):
```
PYTHONPATH=src python3 scripts/supervision_check.py --repair
```

If any command fails, report the specific failure and do not declare the system ready. If all pass, confirm "Pre-flight complete — system ready for live session."
