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

---

## PATCH-003 — Tranche-Driven Stop Coordination

**Logged:** 2026-06-14  
**Audit ref:** Architecture review — coordinate remaining stop with partial-close rungs (PATCH-002 follow-on)  
**Impact:** Exit-layer only — fires immediately after a successful partial close tranche; no signal engine, entry sizing, or REST budget changes.  
**Prerequisite:** PATCH-002 (multi-rung partial close + `partial_close_rung_index`) must be live. Apply during closed session per `.cursorrules` audit.

### Problem

After PATCH-002 banks profit at 1.5R / 2.5R / 3.5R, the **remaining volume** still relies on the generic breakeven/trailing path on subsequent ticks. That leaves a gap: the core book can sit on a wide initial stop while partial profits are already banked. Operators want **immediate, tiered stop tightening** keyed to the rung that just fired — without new SQLite columns or loosening risk.

### Design goals

| Goal | Approach |
|------|----------|
| Tie stop floor to executed rung | Read **0-based `rung_idx`** at partial-close fire time (before `advance_partial_close_rung`) |
| No new migrations | Reuse `partial_close_rung_index` (already incremented post-close) + optional in-memory idempotency set |
| Tighten-only | Never move stop away from market; mirror `eval_breakeven_stop` / trailing backwards-reject semantics |
| Same-tick coordination | Apply stop floor **inside** `_apply_partial_close()` **after** size/rung SQLite updates, **before** trailing on the same quote tick |
| Broker sync | Rely on existing `_sync_stop_to_ig` at end of `update_from_quote` when `stop` moved |

### Tier map (default — matches live 3-rung config)

| Executed rung (`rung_idx`) | Profit gate (existing) | Stop coordination for **remaining** size |
|----------------------------|------------------------|---------------------------------------------|
| **0** (Rung 1 @ 1.5R) | `at_r_multiple: 1.5` | Lock at **Break-Even + 1 IG point** |
| **1** (Rung 2 @ 2.5R) | `at_r_multiple: 2.5` | Lock at **+1.5R** (entry ± 1.5 × `entry_atr` in price space) |
| **2+** (Rung 3 @ 3.5R) | `at_r_multiple: 3.5` | **No additional tranche floor** — trailing + limit extension only (conservative default) |

Rung 3 intentionally has no extra floor so the final 25% can ride the adaptive trail without over-constraining.

---

### 1. Configuration exposure (optional JSON — conservative fallback)

Add to `config/config_v25.json` → `trailing_stop` block (all keys optional; hardcoded defaults when absent):

```json
"tranche_stop_coordination_enabled": true,
"tranche_stop_tiers": [
  {
    "after_rung_index": 0,
    "mode": "breakeven_plus",
    "offset_ig_points": 1.0
  },
  {
    "after_rung_index": 1,
    "mode": "lock_at_r",
    "at_r_multiple": 1.5
  }
]
```

#### `src/system/config.py` (minimal)

```python
@dataclass(frozen=True)
class TrancheStopTier:
    after_rung_index: int   # 0 = first rung (1.5R peel)
    mode: str               # "breakeven_plus" | "lock_at_r"
    offset_ig_points: float = 1.0
    at_r_multiple: float = 0.0

def normalize_tranche_stop_tiers(trailing_stop: dict) -> list[TrancheStopTier]:
    if not trailing_stop.get("tranche_stop_coordination_enabled", True):
        return []
    raw = trailing_stop.get("tranche_stop_tiers")
    if isinstance(raw, list) and raw:
        ...  # parse + sort by after_rung_index
    # Conservative default matching table above
    return [
        TrancheStopTier(0, "breakeven_plus", offset_ig_points=1.0),
        TrancheStopTier(1, "lock_at_r", at_r_multiple=1.5),
    ]

@property
def tranche_stop_tiers(self) -> list[TrancheStopTier]:
    return normalize_tranche_stop_tiers(self.trailing_stop)
```

When `tranche_stop_coordination_enabled: false`, entire feature is a no-op (fail-safe).

---

### 2. Core algorithm — `_apply_tranche_stop_coordination()`

**New private method** on `TradeManager` in `src/trading/trade_manager.py`.

**Call site:** end of `_apply_partial_close()`, immediately after `advance_partial_close_rung()` and notes commit, **before** return:

```python
coord_msgs = self._apply_tranche_stop_coordination(
    market=market,
    side=side,
    trade_id=trade_id,
    entry=entry,
    px=px,
    entry_atr=entry_atr,
    epic=epic,
    executed_rung_idx=rung_idx,   # 0-based index of rung that JUST fired
    ig_deal=ig_deal,
)
# append coord_msgs to return list / log / on_alert
```

#### Pseudocode

```
tiers = cfg.tranche_stop_tiers
tier = first t where t.after_rung_index == executed_rung_idx
if tier is None:
    return []

current_stop = store.get_stop(trade_id) or initial_stop_from_caller
proposed = _tranche_lock_stop_price(side, entry, entry_atr, epic, tier)

if not _stop_tightens(side, current_stop, proposed):
    log_engine("TRANCHE STOP skipped — already tighter")
    return []

proposed = clamp_stop_to_broker_minimum(...)   # reuse dealing_constraints
proposed = _round_stop_level(proposed, epic)

if not _stop_tightens(side, current_stop, proposed):  # re-check after clamp
    return []

store.update_stop(trade_id, proposed, note=f" | Tranche stop tier {executed_rung_idx+1} ...")
self._tranche_stop_applied.add((trade_id, executed_rung_idx))   # in-memory idempotency
return [f"TRANCHE STOP | {market} | tier {executed_rung_idx+1} | stop {proposed:.5f}"]
```

#### Stop price resolution — `_tranche_lock_stop_price()`

```python
def _tranche_lock_stop_price(
    self,
    side: str,
    entry: float,
    entry_atr: float,
    epic: str,
    tier: TrancheStopTier,
) -> float:
    side_u = side.upper()
    if tier.mode == "breakeven_plus":
        offset_price = self._offset_price(epic, tier.offset_ig_points)
        # Same semantics as capital_recycle_breakeven_stop / eval_breakeven_stop
        return (entry + offset_price) if side_u == "BUY" else (entry - offset_price)
    if tier.mode == "lock_at_r":
        r = float(tier.at_r_multiple)
        delta = float(entry_atr) * r
        return (entry + delta) if side_u == "BUY" else (entry - delta)
    raise ValueError(f"unknown tranche stop mode: {tier.mode}")
```

**Rung 1 (BE+1):** equivalent to calling `eval_breakeven_stop` with `trigger=0`, `profit` already ≥ 1.5R, and `offset=1` IG point — but invoked **unconditionally** after partial close (profit gate already passed).

**Rung 2 (+1.5R):** hard floor at entry ± 1.5×ATR — “aggressive compress” means the stop jumps to a locked profit level rather than waiting for trail trigger.

#### Conservative directional guard — `_stop_tightens()`

Extract once; reuse everywhere tranche logic proposes a stop:

```python
def _stop_tightens(side: str, current: float, proposed: float) -> bool:
    side_u = str(side or "").upper()
    if side_u == "BUY":
        return proposed > current + tolerance
    if side_u == "SELL":
        return proposed < current - tolerance
    return False
```

Use `self._stop_tolerance(epic)` for `tolerance`. **Never** call `update_stop` when `_stop_tightens` is false — same invariant as:

```104:119:src/execution/trailing_stop_engine.py
def eval_breakeven_stop(ev: BreakevenEval) -> float | None:
    ...
    if side == "BUY":
        be_stop = entry + offset
        return be_stop if stop < be_stop else None
    if side == "SELL":
        be_stop = entry - offset
        return be_stop if stop > be_stop else None
```

Trailing path already logs backwards rejection at `_apply_trailing` — tranche path must **silently skip** rather than widen.

---

### 3. Schemaless tracking (no new SQLite columns)

| Mechanism | Role |
|-----------|------|
| **`partial_close_rung_index`** (PATCH-002) | Durable record of how many rungs completed; used on **rehydrate** (below) |
| **`executed_rung_idx` argument** | Exact tier to apply on the tick partial close fires (`0` → BE+1, `1` → +1.5R) |
| **`self._tranche_stop_applied: set[tuple[int, int]]`** | In-memory `(trade_id, after_rung_index)` — prevents double-apply if `_apply_partial_close` retried same tick |
| **Notes append** | Audit trail in existing `trades.notes` via `update_stop` note string |

#### Cold-start rehydrate (optional, conservative)

On first `update_from_quote` for an open trade where `partial_close_rung_index > 0` and stop is looser than tier requires:

```python
def _rehydrate_tranche_stop_floors(self, trade_id, side, entry, entry_atr, epic, ...):
    idx = store.get_partial_close_rung_index(trade_id)
    for tier in cfg.tranche_stop_tiers:
        if tier.after_rung_index < idx:   # rung already completed in SQLite
            self._apply_tranche_stop_coordination(..., executed_rung_idx=tier.after_rung_index, ...)
```

Idempotent because `_stop_tightens` skips if already tight. **Do not** rehydrate on every tick — gate with `_tranche_stop_applied` or a once-per-trade flag in memory; on restart, one-shot rehydrate per trade is enough.

Clear `_tranche_stop_applied` entries in `TradeManager` when trade closes (mirror `_capital_recycle_applied` / `_peak_profit_pts` cleanup).

---

### 4. Interaction with existing exit stack (same quote tick)

Current order in `update_from_quote` (PATCH-002 live):

```
capital_recycle → partial_close → [NEW: tranche stop inside partial_close]
→ breakeven → trailing → limit_extension → _sync_stop_to_ig
```

| Component | Interaction |
|-----------|-------------|
| **Partial close** | Tranche stop runs only after successful broker/SQLite partial |
| **Breakeven** | May no-op if tranche already moved stop above BE trigger — safe (tighten-only) |
| **Trailing** | Sees raised `stop` from SQLite; `eval_trailing_stop` cannot propose lower stop |
| **Stale decay** | Unchanged — compression applies to trail **distance**, not tranche floor |
| **Stop dispatch** | End-of-tick `_sync_stop_to_ig` pushes tightened stop when `broker_managed` |

**Do not** invoke `eval_trailing_stop` inside tranche coordination — set an explicit floor price. Trailing continues to manage further tightening on later ticks.

---

### 5. Files to touch (maintenance window only)

| File | Change |
|------|--------|
| `src/trading/trade_manager.py` | `_apply_tranche_stop_coordination`, `_tranche_lock_stop_price`, `_stop_tightens`; hook at end of `_apply_partial_close`; optional `_rehydrate_tranche_stop_floors`; init/cleanup for `_tranche_stop_applied` |
| `src/system/config.py` | `TrancheStopTier`, `normalize_tranche_stop_tiers`, property accessor |
| `config/config_v25.json` | Optional `tranche_stop_*` keys under `trailing_stop` |
| `tests/test_trade_manager.py` | New `TrancheStopCoordinationTests` class (below) |
| `tests/test_deployed_fixes.py` | Optional key-presence check for `tranche_stop_coordination_enabled` |

**Do not modify:** signal engine, correlation matrix, REST budget, `learning_store` schema, broker entry path.

---

### 6. Offline unit tests — append to `tests/test_trade_manager.py`

Use `trade_manager_test_config()` / `_cfg()` with PATCH-002 rungs + coordination enabled. `skip_ig_synced_exits=True`, mock REST.

```python
class TrancheStopCoordinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LearningStore(...)
        self.store.connect()
        self.mgr = TradeManager(_cfg(trailing_stop={
            "partial_close_enabled": True,
            "tranche_stop_coordination_enabled": True,
            "partial_close_rungs": [
                {"at_r_multiple": 1.5, "fraction": 0.25},
                {"at_r_multiple": 2.5, "fraction": 0.25},
            ],
            "tranche_stop_tiers": [
                {"after_rung_index": 0, "mode": "breakeven_plus", "offset_ig_points": 1.0},
                {"after_rung_index": 1, "mode": "lock_at_r", "at_r_multiple": 1.5},
            ],
        }), self.store, skip_ig_synced_exits=True)

    def test_rung1_partial_close_locks_be_plus_one(self) -> None:
        """After rung 1 @ 1.5R, stop must move to entry + 1 IG pt (BUY)."""
        entry, atr = 100.0, 20.0
        tid = _open_trade(self.store, entry=entry, stop=80.0)
        self.store.set_v25_entry_meta(tid, entry_atr=atr, trail_distance=35.0)
        px = entry + 1.5 * atr + 0.5
        self.mgr.update_from_quote("Japan 225", "IX.D.NIKKEI.IFM.IP", Quote(..., px, px+1))
        stop = float(self.store.get_stop(tid))
        expected = entry + self.mgr._offset_price("IX.D.NIKKEI.IFM.IP", 1.0)
        self.assertAlmostEqual(stop, expected, places=3)
        self.assertEqual(self.store.get_partial_close_rung_index(tid), 1)

    def test_rung2_partial_close_locks_one_point_five_r(self) -> None:
        """After rung 2 @ 2.5R, stop must be at entry + 1.5*ATR (BUY), not below."""
        entry, atr = 100.0, 20.0
        tid = _open_trade(self.store, entry=entry, stop=80.0)
        self.store.set_v25_entry_meta(tid, entry_atr=atr, trail_distance=35.0)
        # Fire rung 1
        px1 = entry + 1.5 * atr + 0.5
        self.mgr.update_from_quote(...)
        # Fire rung 2
        px2 = entry + 2.5 * atr + 0.5
        self.mgr.update_from_quote(...)
        stop = float(self.store.get_stop(tid))
        self.assertAlmostEqual(stop, entry + 1.5 * atr, places=3)

    def test_tranche_stop_never_loosens(self) -> None:
        """If stop already above BE+1, rung 1 coordination is a no-op."""
        entry, atr = 100.0, 20.0
        tid = _open_trade(self.store, entry=entry, stop=entry + 5.0)  # already tight
        ...
        stop_before = float(self.store.get_stop(tid))
        self.mgr.update_from_quote(...)  # partial at 1.5R
        self.assertAlmostEqual(float(self.store.get_stop(tid)), stop_before, places=5)

    def test_sell_side_tightens_downward(self) -> None:
        """SELL: rung 1 → entry - 1 IG pt; never raises stop."""
        tid = _open_sell_trade(self.store, entry=100.0, stop=110.0)
        ...

    def test_coordination_disabled_is_noop(self) -> None:
        mgr = TradeManager(_cfg(trailing_stop={
            "partial_close_enabled": True,
            "tranche_stop_coordination_enabled": False,
            ...
        }), ...)
        # partial close fires but stop unchanged from pre-partial value

    def test_rehydrate_applies_missing_floor_after_restart(self) -> None:
        """Simulate partial_close_rung_index=1 in SQLite with wide stop — one-shot rehydrate tightens."""
        tid = _open_trade(...)
        self.store.conn.execute(
            "UPDATE trades SET partial_close_rung_index=1 WHERE id=?", (tid,)
        )
        self.store.conn.commit()
        mgr2 = TradeManager(...)  # fresh instance, empty _tranche_stop_applied
        mgr2.update_from_quote(...)
        self.assertGreater(float(self.store.get_stop(tid)), 80.0)  # BUY tightened
```

Add pure unit tests for `_stop_tightens` and `_tranche_lock_stop_price` if extracted as module-level helpers in `tests/test_trailing_stop_engine.py` or kept as `TradeManager` method tests.

---

### 7. Conservative fallbacks

| Condition | Behaviour |
|-----------|-----------|
| `tranche_stop_coordination_enabled: false` | No stop change after partial close |
| No tier for `executed_rung_idx` | No-op (rung 3 default) |
| `entry_atr <= 0` on `lock_at_r` tier | Skip tier; log warning |
| Broker partial failed (early return) | Tranche stop **not** applied |
| `_stop_tightens` false after clamp | Skip; do not `update_stop` |
| Legacy single partial (`partial_close_done` only) | Treat as `rung_index >= 1`; apply tier 0 only on rehydrate if enabled |

---

### 8. Discover-test (closed session)

```bash
cd /Users/chrisgordon/Projects/IG_Agent_v25
PYTHONPATH=src .venv/bin/python3 -m pytest tests/test_trade_manager.py -k "tranche_stop or partial_close" -x -v
PYTHONPATH=src .venv/bin/python3 -m pytest tests/test_stale_decay_trailing.py tests/test_deployed_fixes.py -q
PYTHONPATH=src .venv/bin/python3 -m unittest discover -s tests
```

---

### 9. Commit message (suggested)

```
feat(exits): tranche-driven stop coordination after partial close rungs
```

---

### 10. Apply sequence (anti-zombie protocol)

Same as PATCH-001 §Apply sequence. Recommended order:

1. PATCH-001 + PATCH-002 already live on agent  
2. Apply PATCH-003 edits + unit tests  
3. Full discover-test batch  
4. Single commit; cold-start via `./scripts/start_agent_background.sh` after audit  

**Live agent (PID 3967) must not be touched during blueprint-only phase.**

---

### Example operator config (post-patch)

Extends PATCH-002 three-rung peel with coordinated stop floors:

```json
"trailing_stop": {
  "partial_close_enabled": true,
  "partial_close_rungs": [
    { "at_r_multiple": 1.5, "fraction": 0.25 },
    { "at_r_multiple": 2.5, "fraction": 0.25 },
    { "at_r_multiple": 3.5, "fraction": 0.25 }
  ],
  "tranche_stop_coordination_enabled": true,
  "tranche_stop_tiers": [
    { "after_rung_index": 0, "mode": "breakeven_plus", "offset_ig_points": 1.0 },
    { "after_rung_index": 1, "mode": "lock_at_r", "at_r_multiple": 1.5 }
  ]
}
```

**Behaviour summary:** Bank 25% at 1.5R → remaining stop jumps to BE+1; bank another 25% at 2.5R → stop locks at +1.5R; final 25% trails normally toward 3.5R peel.

---

*End PATCH-003*
