# Maintenance Patches — Closed-Session Only

Apply only when `.cursorrules` pre-development audit passes:

- [Market Sessions Closed?] YES
- [Watchdog Hold Active?] manual_stop set before cycle
- [Active PIDs Cleaned?] verified after graceful shutdown

Do **not** hot-reload or kill processes while the agent is live.

---

## PATCH-001 — `_touch_peak_profit` MFE peak tracker (stale decay bypass)

**Logged:** 2026-06-14  
**Audit ref:** Omission audit — `_at_mfe` always `True` when peak dict entry missing  
**Impact:** Stale position decay compression is inert in production; standard trailing stops still apply (fail-safe).  
**Files:** `src/trading/trade_manager.py`, `tests/test_stale_decay_trailing.py`

### Problem

When `trade_id` is absent from `_peak_profit_pts`, `.get(trade_id, profit_pts)` sets `peak == profit_pts`, so `profit_pts > peak` is never true, the dict is never written, and `_at_mfe` always returns `True` — permanently bypassing stale decay.

### Fix — replace `_touch_peak_profit` in `src/trading/trade_manager.py`

**Remove (lines ~758–763):**

```python
    def _touch_peak_profit(self, trade_id: int, profit_pts: float) -> float:
        peak = self._peak_profit_pts.get(trade_id, profit_pts)
        if profit_pts > peak:
            peak = profit_pts
            self._peak_profit_pts[trade_id] = peak
        return peak
```

**Insert:**

```python
    def _touch_peak_profit(self, trade_id: int, profit_pts: float) -> float:
        prev = self._peak_profit_pts.get(trade_id)
        if prev is None or profit_pts > prev:
            self._peak_profit_pts[trade_id] = profit_pts
            return profit_pts
        return prev
```

`_at_mfe` is unchanged.

### Unit test — append to `StaleDecayTradeManagerWiringTests` in `tests/test_stale_decay_trailing.py`

```python
    def test_at_mfe_false_after_pullback_without_manual_peak_seed(self) -> None:
        opened = datetime.utcnow() - timedelta(minutes=40)
        tid = _open_trade(self.store, stop=90.0, opened_at=opened)
        # Organic peak tracking — no manual _peak_profit_pts seed
        self.assertEqual(self.mgr._touch_peak_profit(tid, 20.0), 20.0)
        self.assertTrue(self.mgr._at_mfe(tid, 20.0))
        self.assertFalse(self.mgr._at_mfe(tid, 19.0))   # 1.0 pt pullback > 0.5 epsilon
        self.assertTrue(self.mgr._at_mfe(tid, 19.6))    # within 0.5 pt of peak 20.0
        self.mgr._touch_peak_profit(tid, 25.0)
        self.assertFalse(self.mgr._at_mfe(tid, 24.0))
```

### Discover-test (closed session)

```bash
cd /Users/chrisgordon/Projects/IG_Agent_v25
PYTHONPATH=src .venv/bin/python3 -m pytest tests/test_stale_decay_trailing.py -x -v
PYTHONPATH=src .venv/bin/python3 -m pytest tests/test_risk_telemetry_correlation.py tests/test_telegram_notifier.py -q
```

### Commit message (suggested)

```
fix(trailing): seed MFE peak tracker on first tick so stale decay bypass is accurate
```

### Apply sequence (anti-zombie protocol)

1. Pre-development audit — confirm flat / sessions closed.
2. `PYTHONPATH=src .venv/bin/python3 -c "from system.shutdown_cleanup import mark_manual_stop; mark_manual_stop(source='operator_restart')"`
3. `kill -TERM <main.py PID>` — wait up to 30s; verify `lsof -iTCP:8080` is free.
4. Apply PATCH-001 edits; run discover-test commands above.
5. Commit and push; cold-start via desktop launcher or launchd watchdog.

---

*End PATCH-001*

---

## PATCH-002 — Multi-Rung Config-Driven Partial Closes

**Logged:** 2026-06-14  
**Audit ref:** Architecture review — extend existing partial close (avoid Volatility Bands / Dynamic Slippage)  
**Impact:** Notification/exit-layer only; no signal engine, broker entry, or trailing math changes.  
**Prerequisite:** Apply during closed session; may be combined with PATCH-001 in one maintenance window.

### Current state (live code drift)

| Location | Behaviour today |
|----------|-----------------|
| `config/config_v25.json` | `partial_close_at_r: 1.5`, `partial_close_fraction: 0.5` — **present but unused by runtime** |
| `src/system/config.py` | `partial_close_at_r`, `partial_close_fraction` accessors exist |
| `src/trading/trade_manager.py` | Uses **hardcoded** `PARTIAL_CLOSE_ATR_MULTIPLE = 1.5` and `PARTIAL_CLOSE_FRACTION = 0.5` |
| `src/data/learning_store.py` | Boolean `partial_close_done` — **one shot only** |
| `tests/test_trade_manager.py` | `test_partial_close_once_and_points` asserts single 50% bank |

Profit threshold uses **R-multiples vs entry ATR in IG points** (same semantics as today):

```python
required_profit_pts = at_r_multiple * price_delta_to_ig_points(epic, entry_atr)
```

---

### 1. Configuration exposure

#### JSON shape (`config/config_v25.json` → `trailing_stop` block)

```json
"partial_close_enabled": true,
"partial_close_rungs": [
  { "at_r_multiple": 1.5, "fraction": 0.25 },
  { "at_r_multiple": 2.5, "fraction": 0.25 }
],
"partial_close_at_r": 1.5,
"partial_close_fraction": 0.5
```

- **`partial_close_rungs`** (new, authoritative when non-empty): ordered list of rungs, ascending `at_r_multiple`.
- **`fraction`**: share of **original entry size** to close at that rung (not of remainder — predictable sizing).
- **Legacy keys retained** for backward compatibility and fallback only.

#### `src/system/config.py` — add normalizer + property

```python
@dataclass(frozen=True)
class PartialCloseRung:
    at_r_multiple: float
    fraction: float

def normalize_partial_close_rungs(trailing_stop: dict[str, Any]) -> list[PartialCloseRung]:
    """Conservative fallback: legacy single rung when rungs missing/empty."""
    raw = trailing_stop.get("partial_close_rungs")
    rungs: list[PartialCloseRung] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                at_r = float(item.get("at_r_multiple") or item.get("at_r") or 0)
                frac = float(item.get("fraction") or 0)
            except (TypeError, ValueError):
                continue
            if at_r > 0 and 0 < frac <= 1.0:
                rungs.append(PartialCloseRung(at_r, frac))
    rungs.sort(key=lambda r: r.at_r_multiple)
    if rungs:
        return rungs
    # Fallback — original 1.5R / 50% behaviour
    at_r = float(trailing_stop.get("partial_close_at_r") or 1.5)
    frac = float(trailing_stop.get("partial_close_fraction") or 0.5)
    return [PartialCloseRung(at_r, frac)]

@property
def partial_close_rungs(self) -> list[PartialCloseRung]:
    return normalize_partial_close_rungs(self.trailing_stop)
```

- **`config_validator.py`**: when `partial_close_enabled` and `partial_close_rungs` absent, do **not** inject rungs (let normalizer synthesize from legacy keys).
- **Validation guard** (in normalizer or validator): sum of `fraction` values must be **≤ 1.0**; if > 1.0, clamp last rung or log warning and truncate.

#### Remove module constants from `trade_manager.py`

Delete (or stop using):

```python
PARTIAL_CLOSE_ATR_MULTIPLE = 1.5
PARTIAL_CLOSE_FRACTION = 0.5
```

All thresholds read from `self._cfg.partial_close_rungs`.

---

### 2. Multi-tranche SQLite tracking

#### Schema migration (`learning_store.py` — `_ensure_schema` pattern)

Add columns (idempotent `ALTER TABLE` like existing `partial_close_done`):

```sql
ALTER TABLE trades ADD COLUMN partial_close_rung_index INTEGER DEFAULT 0;
ALTER TABLE trades ADD COLUMN original_size REAL;
```

- **`partial_close_rung_index`**: count of rungs **completed** (0 = none, 1 = first rung done, …).
- **`original_size`**: set once at `open_trade` to initial `size`; used for fraction math on every rung.

Backfill on migration for open rows:

```sql
UPDATE trades SET original_size = size WHERE original_size IS NULL AND closed_at IS NULL;
```

#### New store methods (replace boolean semantics)

```python
def get_partial_close_rung_index(self, trade_id: int) -> int: ...
def advance_partial_close_rung(self, trade_id: int) -> None: ...
def get_original_size(self, trade_id: int, fallback: float) -> float: ...
def all_partial_rungs_done(self, trade_id: int, total_rungs: int) -> bool: ...
```

- **Deprecate but keep** `is_partial_close_done` / `mark_partial_close_done` as thin wrappers:
  - `is_partial_close_done(id)` → `get_partial_close_rung_index(id) > 0` **only for legacy callers** OR `>= len(rungs)` for “fully scaled”.
  - Prefer new API inside `_apply_partial_close`.

#### `_apply_partial_close()` algorithm (one rung per hub tick max)

```
rungs = cfg.partial_close_rungs
idx = store.get_partial_close_rung_index(trade_id)
if idx >= len(rungs):
    return []

rung = rungs[idx]
original = store.get_original_size(trade_id, size)
close_size = min(size, original * rung.fraction)
if close_size <= 0 or profit < rung.at_r_multiple * atr_pts:
    return []

# broker partial (unchanged contract)
rest.close_position(..., size=close_size, ...)

# SQLite — preserve update_trade_size mechanics
new_size = size - close_size
store.update_trade_size(trade_id, new_size)   # existing method
store.advance_partial_close_rung(trade_id)      # idx += 1
append note: f" | Partial close rung {idx+1}/{len(rungs)} ..."

# points_engine: record banked_pts for this tranche only (unchanged pattern)
```

**Invariants (do not break):**

- At most **one rung fires per quote tick** (prevents REST/broker spam on fast hub stream).
- `close_size` capped by **current** `size` (IG min lot / prior partials).
- Never set `partial_close_done=1` until **all** rungs complete (or keep boolean synced: `done = idx >= len(rungs)`).

#### Touch points outside `trade_manager.py`

| File | Change |
|------|--------|
| `src/runtime/ig_position_sync.py` | `_confirm_partial_close`: increment rung index instead of boolean flip; respect `original_size` |
| `src/data/learning_store.py` | `open_trade`: persist `original_size=size` |
| `tests/test_deployed_fixes.py` | Allow optional `partial_close_rungs` key in config contract test |

**Do not modify:** signal engine, spread-atr circuit, correlation matrix, trailing_stop_engine math.

---

### 3. Conservative fallbacks

| Condition | Behaviour |
|-----------|-----------|
| `partial_close_rungs` missing, `[]`, or all invalid | `[PartialCloseRung(1.5, 0.5)]` from legacy keys or hard defaults |
| `partial_close_enabled: false` | `_apply_partial_close` returns immediately (unchanged) |
| `entry_atr <= 0` or `size <= 0` | No partial (unchanged) |
| `original_size` NULL on old row | Fallback to current `size` at first partial trigger |
| Broker partial fails | Return `[]`, **do not** advance rung index (unchanged fail-safe) |
| Sum of fractions > 1.0 | Clamp or reject config at startup with warning; runtime: `close_size = min(close_size, size)` |

Legacy single-partial deployments continue to work with **zero JSON changes**.

---

### Offline unit tests (add to `tests/test_trade_manager.py`)

```python
def test_partial_close_rungs_fallback_legacy_single(self) -> None:
    """Empty partial_close_rungs → 1.5R / 50% (today's behaviour)."""
    cfg = _cfg(trailing_stop={
        "partial_close_enabled": True,
        "partial_close_at_r": 1.5,
        "partial_close_fraction": 0.5,
        "partial_close_rungs": [],
    })
    ...

def test_partial_close_two_rungs_sequential(self) -> None:
    """Rung 1 @ 1.5R 25%, rung 2 @ 2.5R 25% — two ticks, size 1.0 → 0.75 → 0.50."""
    cfg = _cfg(trailing_stop={
        "partial_close_enabled": True,
        "partial_close_rungs": [
            {"at_r_multiple": 1.5, "fraction": 0.25},
            {"at_r_multiple": 2.5, "fraction": 0.25},
        ],
    })
    # tick1: px at 1.5R+ → size 0.75, rung_index 1
    # tick2: px at 2.5R+ → size 0.50, rung_index 2
    # tick3: same px → no PARTIAL CLOSE message

def test_partial_close_second_rung_not_early(self) -> None:
    """Profit at 1.5R must not fire rung 2 (@ 2.5R)."""
    ...

def test_normalize_partial_close_rungs_sorts_and_fallback(self) -> None:
    from system.config import normalize_partial_close_rungs
    assert len(normalize_partial_close_rungs({})) == 1
    assert normalize_partial_close_rungs({})[0].at_r_multiple == 1.5
```

Add config normalizer tests in `tests/test_config.py` or `tests/test_deployed_fixes.py` if present.

---

### Discover-test (closed session)

```bash
cd /Users/chrisgordon/Projects/IG_Agent_v25
PYTHONPATH=src .venv/bin/python3 -m pytest tests/test_trade_manager.py -k partial_close -x -v
PYTHONPATH=src .venv/bin/python3 -m pytest tests/test_deployed_fixes.py -k partial_close -x -v
PYTHONPATH=src .venv/bin/python3 -m pytest tests/test_stale_decay_trailing.py tests/test_profitability_improvements.py -q
```

---

### Commit message (suggested)

```
feat(exits): multi-rung config-driven partial closes with SQLite rung tracking
```

---

### Apply sequence (anti-zombie protocol)

Same as PATCH-001 §Apply sequence. Recommended order in one maintenance window:

1. PATCH-001 (`_touch_peak_profit`) — stale decay MFE fix  
2. PATCH-002 (this patch) — partial close rungs  
3. Single discover-test batch → one commit or two logical commits  
4. Cold-start via desktop launcher or launchd watchdog  

---

### Example operator config (post-patch)

Conservative 3-rung peel matching ≥3:1 RR architecture (bank early, trail remainder):

```json
"partial_close_rungs": [
  { "at_r_multiple": 1.5, "fraction": 0.25 },
  { "at_r_multiple": 2.5, "fraction": 0.25 },
  { "at_r_multiple": 3.5, "fraction": 0.25 }
]
```

Remaining 25% rides trailing stop + limit extension.

---

*End PATCH-002*
