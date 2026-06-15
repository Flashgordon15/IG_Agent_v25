# IG Agent — Release changelog

Update this file whenever you ship **major upgrades or enhancements**.
The launch splash (Stage 3) reads the section matching `APP_VERSION` in
`src/system/app_identity.py` automatically — no dashboard code edits required.

Format:

```markdown
## X.Y.Z
Title: Short headline for the splash screen

### Major
- Bullet one

### Enhancements
- Bullet two
```

---

## 29.1.0

Title: Critical Hardening & Self-Heal Launch

### Major
- Stop attachment: 5s verify window, 3 retries with backoff, emergency close + Telegram alert if stop missing
- INITIALIZING banner clears from live prices + trading loops — no watchdog or supervision drift required
- Full pytest suite runs as the final launch gate before trading goes live

### Enhancements
- Golden path health spec, self-verification tests, and `scripts/health_check.sh`
- REST poll stall telemetry with Telegram alert after sustained quote starvation
- Quote health uses hub tick age — dashboard LIVE aligns with `/api/health`
- Desktop launcher bundle rebuild, quarantine clear, and watchdog cold-start path fixes
- CI lint import-order fix and config overlay for stop-verify timing
