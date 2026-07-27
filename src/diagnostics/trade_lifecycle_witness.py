"""Trade lifecycle witness + loss autopsy (read-only diagnostics).

Reconstructs start→finish timelines from journal / ML outcomes / learning DB /
autopsy JSON. Classifies losing closes as:

* **APP** — application failed to honour configured policy (excluded epic,
  Instant/micro while disabled, CFD while A2 paused, macro claim with
  sub-minute hold, etc.)
* **LOGIC** — stamps show policy path was followed; loss came from exit math
  (soft_loss / trail / LTR / broker) on an allowed epic
* **UNKNOWN** — evidence gap (missing ML / hold / regime / engine attribution)

Never places orders, never mutates bleed locks / pause markers.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Policy thresholds (desk Path A / macro carve)
# ---------------------------------------------------------------------------

NIKKEI_EPIC = "IX.D.NIKKEI.IFM.IP"
DOW_EPIC = "IX.D.DOW.IFM.IP"
DEFAULT_EXCLUDED = (
    "IX.D.NIKKEI.IFM.IP",
    "CS.D.GBPUSD.CFD.IP",
    "IX.D.DAX.IFM.IP",
    "CS.D.CRUDE.CFD.IP",
)

# Hold ≪ macro intent while Path A / MACRO_SENTINEL claimed
MACRO_MIN_HOLD_SEC = 150.0  # min_hold_before_trail_sec
MACRO_INTENT_HOLD_SEC = 180.0  # LTR arm window
MICRO_HOLD_BREACH_SEC = 60.0  # supervisor MICRO_HOLD median floor

SB_ACCOUNT = "Z6BAH3"
CFD_ACCOUNT = "Z6BAH4"

MICRO_ORIGIN_TOKENS = (
    "INSTANT",
    "MICRO",
    "ENGINE_B_MICRO",
    "CORE_B",
    "SCALPER",
    "micro_gbp_exit",
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PolicyBreach:
    code: str
    detail: str
    severity: str = "fail"  # fail | watch


@dataclass
class LifecycleEvent:
    stage: str
    ts: str | None
    detail: str
    source: str


@dataclass
class TradeLifecycle:
    deal_id: str
    timestamp: str | None = None
    direction: str | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    pnl_gbp: float | None = None
    account_id: str | None = None
    product_type: str | None = None
    engine_origin: str | None = None
    exit_reason: str | None = None
    hold_sec: float | None = None
    style: str | None = None
    ml_score_at_entry: float | None = None
    market_regime: str | None = None
    epic: str | None = None
    market: str | None = None
    size: float | None = None
    opened_at: str | None = None
    closed_at: str | None = None
    confidence: float | None = None
    notes_excerpt: str | None = None
    events: list[LifecycleEvent] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    evidence_gaps: list[str] = field(default_factory=list)
    policy_breaches: list[PolicyBreach] = field(default_factory=list)
    loss_class: str | None = None  # APP | LOGIC | UNKNOWN
    loss_class_reason: str | None = None
    exit_authority: str | None = None
    hold_sec_trusted: bool = False
    ml_score_trusted: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class PolicyContext:
    exclude_from_hot_path: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDED))
    sb_disable_instant_micro: bool = True
    sb_disable_core_b_micro: bool = True
    a2_cfd_paused: bool = False
    path_a_claimed: bool = True
    macro_min_hold_sec: float = MACRO_MIN_HOLD_SEC
    micro_hold_breach_sec: float = MICRO_HOLD_BREACH_SEC
    # Agent soft-loss cut in GBP (risk_per_trade_gbp * soft_loss_ratio). A loss
    # materially beyond this on a broker-closed trade means our stack never cut.
    soft_loss_gbp: float = 2.95
    # Tolerance before calling it a supervision failure rather than slippage.
    soft_loss_overrun_ratio: float = 1.5


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def default_data_root() -> Path:
    try:
        from system.paths import data_dir

        return Path(data_dir())
    except Exception:
        return Path(__file__).resolve().parents[1] / "data" / "v31-production"


def load_policy_context(data_root: Path | None = None, config_path: Path | None = None) -> PolicyContext:
    root = data_root or default_data_root()
    ctx = PolicyContext()
    cfg_path = config_path
    if cfg_path is None:
        proj = Path(__file__).resolve().parents[2]
        cand = proj / "config" / "config_v31_demo_throughput.json"
        if cand.is_file():
            cfg_path = cand
    if cfg_path and cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            dual = cfg.get("dual_core") or {}
            excl = dual.get("exclude_from_hot_path") or []
            if excl:
                ctx.exclude_from_hot_path = [str(e) for e in excl]
            if "sb_disable_instant_micro" in dual:
                ctx.sb_disable_instant_micro = bool(dual["sb_disable_instant_micro"])
            if "sb_disable_core_b_micro" in dual:
                ctx.sb_disable_core_b_micro = bool(dual["sb_disable_core_b_micro"])
            mr = cfg.get("micro_risk") or {}
            if mr.get("min_hold_before_trail_sec") is not None:
                ctx.macro_min_hold_sec = float(mr["min_hold_before_trail_sec"])
            if mr.get("risk_per_trade_gbp") and mr.get("soft_loss_ratio"):
                ctx.soft_loss_gbp = float(mr["risk_per_trade_gbp"]) * float(
                    mr["soft_loss_ratio"]
                )
        except Exception:
            pass

    a2 = root / "state_cfd" / "a2_entries_paused.json"
    if a2.is_file():
        try:
            raw = json.loads(a2.read_text(encoding="utf-8"))
            # Historical day: if marker exists with A2_SB_ONLY mode, treat CFD
            # entries during that day as paused even if active flipped later.
            mode = str(raw.get("mode") or "")
            if raw.get("active") is True or mode in ("A2_SB_ONLY", "OPERATOR_HALT_BLEED"):
                # For autopsy of the bleed day, operator intent was CFD paused.
                # Prefer explicit active; else use mode + date match.
                if raw.get("active") is True:
                    ctx.a2_cfd_paused = True
                elif str(raw.get("date") or "") or mode == "A2_SB_ONLY":
                    ctx.a2_cfd_paused = True
        except Exception:
            pass

    ctx.path_a_claimed = bool(ctx.sb_disable_instant_micro and ctx.sb_disable_core_b_micro)
    return ctx


# ---------------------------------------------------------------------------
# Parsers / loaders
# ---------------------------------------------------------------------------


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _s(v: Any) -> str | None:
    t = str(v or "").strip()
    return t or None


def _parse_ts(raw: Any) -> datetime | None:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        if " " in s and "T" not in s:
            s = s.replace(" ", "T", 1)
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def infer_epic_from_market_price(
    *,
    epic: str | None,
    market: str | None,
    entry_price: float | None,
) -> tuple[str | None, list[str]]:
    """Correct obvious learning-DB epic/market mismatches.

    Observed 2026-07-24: many rows have market='Wall Street' + DOW prices (~51k)
    but epic='IX.D.NIKKEI.IFM.IP'. Prefer market + price band over raw epic.
    Returns (epic, notes).
    """
    notes: list[str] = []
    e = (epic or "").strip() or None
    m = (market or "").strip().upper()
    px = entry_price

    def _dowish(p: float | None) -> bool:
        return p is not None and 40000.0 <= p <= 56000.0

    def _nikish(p: float | None) -> bool:
        return p is not None and 56000.0 < p <= 120000.0

    # Market name wins when it conflicts with epic
    if m in ("WALL STREET", "WALL ST", "DOW", "US 30") or "WALL STREET" in m:
        if e and "NIKKEI" in e.upper():
            notes.append("epic_corrected_wall_street_mislabelled_nikkei")
            e = DOW_EPIC
        elif not e:
            e = DOW_EPIC
            notes.append("epic_inferred_from_market_wall_street")
    elif m in ("JAPAN 225", "NIKKEI", "JAPAN225") or "JAPAN" in m:
        if e and "DOW" in e.upper() and _nikish(px):
            notes.append("epic_corrected_japan_mislabelled_dow")
            e = NIKKEI_EPIC
        elif not e:
            e = NIKKEI_EPIC
            notes.append("epic_inferred_from_market_japan")

    # Price band backup when market blank / still contradictory
    if e and "NIKKEI" in e.upper() and _dowish(px):
        notes.append("epic_corrected_nikkei_label_dow_price")
        e = DOW_EPIC
    elif e and "DOW" in e.upper() and _nikish(px):
        notes.append("epic_corrected_dow_label_nikkei_price")
        e = NIKKEI_EPIC
    elif not e and _dowish(px):
        e = DOW_EPIC
        notes.append("epic_inferred_from_dow_price_band")
    elif not e and _nikish(px):
        e = NIKKEI_EPIC
        notes.append("epic_inferred_from_nikkei_price_band")

    return e, notes


def _hold_from_open_close(opened: Any, closed: Any) -> float | None:
    a = _parse_ts(opened)
    b = _parse_ts(closed)
    if a is None or b is None:
        return None
    sec = (b - a).total_seconds()
    if sec < 0 or sec > 6 * 3600:
        # Reject absurd spans (stale opened_at phantoms)
        return None
    return sec


def _row_score(row: dict[str, Any]) -> int:
    s = 0
    if _s(row.get("HoldSec")):
        s += 3
    eo = _s(row.get("EngineOrigin")) or ""
    if eo and eo != "broker_attached":
        s += 3
    if _s(row.get("MarketRegime")):
        s += 1
    if _s(row.get("MlScoreAtEntry")):
        s += 1
    er = _s(row.get("ExitReason")) or ""
    if er and er != "broker_attached":
        s += 1
    if _s(row.get("Style")):
        s += 1
    return s


def load_journal_rows(
    data_root: Path,
    *,
    day: str | None = None,
    deal_id: str | None = None,
    last_n: int | None = None,
) -> list[dict[str, Any]]:
    path = data_root / "metrics" / "daily_journal.csv"
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            did = _s(row.get("DealID")) or ""
            if not did or did.startswith("BENCHMARK"):
                continue
            ts = _s(row.get("Timestamp")) or ""
            if day and not ts.startswith(day):
                continue
            if deal_id:
                aliases = _deal_aliases(deal_id)
                if did not in aliases and not any(did.endswith(a) or a.endswith(did) for a in aliases if len(a) >= 8):
                    # loose match on trailing 8
                    tail = deal_id[-8:] if len(deal_id) >= 8 else deal_id
                    if tail not in did and did[-8:] != tail:
                        continue
            out.append(dict(row))
    if last_n and not deal_id and not day:
        out = out[-int(last_n) :]
    elif last_n and day and not deal_id:
        out = out[-int(last_n) :]
    return out


def dedupe_journal_by_deal(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for row in rows:
        did = _s(row.get("DealID")) or ""
        if not did:
            continue
        prev = best.get(did)
        if prev is None or _row_score(row) > _row_score(prev):
            best[did] = row
    return best


def _deal_aliases(deal_id: str) -> set[str]:
    d = str(deal_id or "").strip()
    out = {d}
    if len(d) >= 8:
        out.add(d[-8:])
    # journal sometimes prefixes DIAAAAX
    if d.startswith("DI") and len(d) > 10:
        out.add(d[2:])
        out.add(d[-15:] if len(d) >= 15 else d)
    return {x for x in out if x}


def load_ml_outcomes_index(data_root: Path) -> dict[str, dict[str, Any]]:
    path = data_root / "metrics" / "ml_trade_outcomes.jsonl"
    idx: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return idx
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                did = _s(row.get("deal_id") or row.get("DealID"))
                if not did:
                    continue
                idx[did] = row
                if len(did) >= 8:
                    idx.setdefault(did[-8:], row)
    except OSError:
        pass
    return idx


def load_learning_index(data_root: Path, *, day: str | None = None) -> dict[str, dict[str, Any]]:
    db = data_root / "learning_db.sqlite3"
    if not db.is_file():
        # bridge may point elsewhere
        try:
            from system.paths import data_dir

            db = Path(data_dir()) / "learning_db.sqlite3"
        except Exception:
            pass
    if not db.is_file():
        return {}
    idx: dict[str, dict[str, Any]] = {}
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as con:
            con.row_factory = sqlite3.Row
            sql = """
                SELECT ig_deal_id, deal_reference, epic, market, side, size, entry, exit,
                       pnl_points, opened_at, closed_at, confidence, notes,
                       account_id, product_type, engine_origin, result
                FROM trades
                WHERE ig_deal_id IS NOT NULL
            """
            params: list[Any] = []
            if day:
                sql += " AND closed_at LIKE ?"
                params.append(f"{day}%")
            sql += " ORDER BY id DESC LIMIT 5000"
            for r in con.execute(sql, params):
                d = dict(r)
                did = _s(d.get("ig_deal_id")) or ""
                ref = _s(d.get("deal_reference")) or ""
                if did:
                    idx[did] = d
                    if len(did) >= 8:
                        idx.setdefault(did[-8:], d)
                if ref:
                    idx.setdefault(ref, d)
                    if len(ref) >= 8:
                        idx.setdefault(ref[-8:], d)
    except Exception:
        return idx
    return idx


def load_autopsy(data_root: Path, deal_id: str) -> dict[str, Any] | None:
    aut_dir = data_root / "autopsy"
    if not aut_dir.is_dir():
        return None
    for alias in _deal_aliases(deal_id):
        cand = aut_dir / f"{alias}.json"
        if cand.is_file():
            try:
                return json.loads(cand.read_text(encoding="utf-8"))
            except Exception:
                continue
    # suffix scan (small dir)
    tail = deal_id[-8:] if len(deal_id) >= 8 else deal_id
    for p in aut_dir.glob(f"*{tail}.json"):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Reconstruct + classify
# ---------------------------------------------------------------------------


def _is_micro_origin(origin: str | None, style: str | None = None) -> bool:
    blob = f"{origin or ''} {style or ''}".upper()
    return any(tok.upper() in blob for tok in MICRO_ORIGIN_TOKENS)


def classify_policy_breaches(lc: TradeLifecycle, ctx: PolicyContext) -> list[PolicyBreach]:
    """Pure classifier — unit-tested."""
    from diagnostics.stamp_provenance import (
        EXIT_AUTHORITY_BROKER,
        classify_exit_authority,
    )

    breaches: list[PolicyBreach] = []
    epic = lc.epic or ""
    origin = (lc.engine_origin or "").strip()
    account = (lc.account_id or "").strip()
    product = (lc.product_type or "").upper()
    style = (lc.style or "").lower()
    hold = lc.hold_sec

    excluded = set(ctx.exclude_from_hot_path)
    if epic and epic in excluded:
        breaches.append(
            PolicyBreach(
                code="EXCLUDED_EPIC",
                detail=f"epic={epic} is in exclude_from_hot_path",
            )
        )
    elif epic and ("NIKKEI" in epic.upper() or epic == NIKKEI_EPIC):
        breaches.append(
            PolicyBreach(
                code="EXCLUDED_EPIC",
                detail=f"Nikkei traded (desk hot-path exclusion): {epic}",
            )
        )

    if ctx.path_a_claimed and account == SB_ACCOUNT and _is_micro_origin(origin, style):
        if ctx.sb_disable_instant_micro or ctx.sb_disable_core_b_micro:
            breaches.append(
                PolicyBreach(
                    code="INSTANT_MICRO_WHILE_DISABLED",
                    detail=f"SB Path A carve on but engine/style looks micro: origin={origin!r} style={style!r}",
                )
            )

    macro_claimed = style == "macro" or origin.upper() == "MACRO_SENTINEL"
    if ctx.path_a_claimed and macro_claimed:
        if hold is not None and hold < ctx.micro_hold_breach_sec:
            breaches.append(
                PolicyBreach(
                    code="HOLD_LT_MACRO_INTENT",
                    detail=(
                        f"hold_sec={hold} ≪ macro intent "
                        f"(breach<{ctx.micro_hold_breach_sec}s, trail_arm={ctx.macro_min_hold_sec}s)"
                    ),
                )
            )
        elif hold is not None and hold < ctx.macro_min_hold_sec and origin.upper() == "MACRO_SENTINEL":
            breaches.append(
                PolicyBreach(
                    code="HOLD_LT_MACRO_INTENT",
                    detail=f"MACRO_SENTINEL hold_sec={hold} < min_hold_before_trail={ctx.macro_min_hold_sec}",
                    severity="watch",
                )
            )

    if lc.ml_score_at_entry is None:
        breaches.append(
            PolicyBreach(
                code="MISSING_ML_STAMP",
                detail="MlScoreAtEntry absent after journal+ml_outcomes+autopsy join",
                severity="watch",
            )
        )

    from diagnostics.stamp_provenance import is_placeholder_regime

    if is_placeholder_regime(lc.market_regime):
        breaches.append(
            PolicyBreach(
                code="MISSING_REGIME_STAMP",
                detail="MarketRegime blank or placeholder (UNKNOWN)",
                severity="watch",
            )
        )

    if hold is None:
        breaches.append(
            PolicyBreach(
                code="MISSING_HOLD_STAMP",
                detail="HoldSec unavailable (journal + learning open/close)",
                severity="watch",
            )
        )

    # Supervision failure: the broker closed the trade for materially more than
    # our own soft-loss cut, so the agent stack had a chance to act and did not.
    if (
        lc.exit_authority == EXIT_AUTHORITY_BROKER
        and lc.pnl_gbp is not None
        and lc.pnl_gbp < 0
    ):
        overrun_limit = ctx.soft_loss_gbp * ctx.soft_loss_overrun_ratio
        if abs(lc.pnl_gbp) > overrun_limit:
            breaches.append(
                PolicyBreach(
                    code="RISK_STACK_DID_NOT_CUT",
                    detail=(
                        f"broker closed at £{lc.pnl_gbp:.2f}; agent soft-loss cut is "
                        f"£-{ctx.soft_loss_gbp:.2f} (overrun limit £-{overrun_limit:.2f})"
                    ),
                )
            )

    cfd_like = product == "CFD" or account == CFD_ACCOUNT
    if ctx.a2_cfd_paused and cfd_like and (lc.pnl_gbp is not None):
        # Any CFD close while A2 pause intent was active
        breaches.append(
            PolicyBreach(
                code="CFD_ENTRY_WHILE_A2_PAUSED",
                detail=f"CFD/Z6BAH4 close while A2 pause marker present (account={account} product={product})",
            )
        )

    # Broker attached stop closed the trade and our risk stack never fired a
    # cut. Distinct from RISK_STACK_DID_NOT_CUT (overrun past soft-loss×1.5):
    # these are the residual SUPERVISION_GAP APP class (~15 losers on Jul 24).
    hard_codes = {b.code for b in breaches if b.severity == "fail"}
    authority = lc.exit_authority or classify_exit_authority(
        exit_reason=lc.exit_reason or "",
        engine_origin=lc.engine_origin or "",
    )
    if (
        authority == EXIT_AUTHORITY_BROKER
        and lc.pnl_gbp is not None
        and lc.pnl_gbp < 0
        and "RISK_STACK_DID_NOT_CUT" not in hard_codes
    ):
        breaches.append(
            PolicyBreach(
                code="SUPERVISION_GAP",
                detail=(
                    "broker_attached_stop exited; agent GBP/virtual/trail stack "
                    "never fired (loss within soft-loss overrun band or unattributed)"
                ),
            )
        )

    return breaches


def classify_loss(lc: TradeLifecycle, ctx: PolicyContext) -> tuple[str, str]:
    """Return (APP|LOGIC|UNKNOWN, reason)."""
    from diagnostics.stamp_provenance import (
        EXIT_AUTHORITY_BROKER,
        classify_exit_authority,
        hold_is_measurable,
        ml_score_is_usable,
    )

    hard = [b for b in lc.policy_breaches if b.severity == "fail"]
    hard_codes = {b.code for b in hard}
    app_codes = {
        "EXCLUDED_EPIC",
        "INSTANT_MICRO_WHILE_DISABLED",
        "HOLD_LT_MACRO_INTENT",
        "CFD_ENTRY_WHILE_A2_PAUSED",
        "RISK_STACK_DID_NOT_CUT",
        "SUPERVISION_GAP",
    }
    if hard_codes & app_codes:
        codes = sorted(hard_codes & app_codes)
        return "APP", "POLICY_BREACH: " + ", ".join(codes)

    authority = lc.exit_authority or classify_exit_authority(
        exit_reason=lc.exit_reason or "",
        engine_origin=lc.engine_origin or "",
    )
    row = {
        "hold_sec": lc.hold_sec,
        "exit_reason": lc.exit_reason,
        "engine_origin": lc.engine_origin,
        "ml_score_at_entry": lc.ml_score_at_entry,
    }
    hold_usable = hold_is_measurable(row)
    ml_usable = ml_score_is_usable(row)

    gaps = set(lc.evidence_gaps)
    watch = {b.code for b in lc.policy_breaches if b.severity == "watch"}
    stamp_gap = bool(
        gaps
        or ("MISSING_ML_STAMP" in watch)
        or ("MISSING_HOLD_STAMP" in watch)
        or (not lc.engine_origin)
        or (lc.engine_origin == "broker_attached" and not lc.exit_reason)
    )
    # broker_attached-only with no epic/hold → UNKNOWN
    if stamp_gap and not lc.epic:
        return "UNKNOWN", "evidence gap: no epic + incomplete journal stamps"
    if stamp_gap and not hold_usable and not ml_usable:
        return "UNKNOWN", "evidence gap: no trustworthy HoldSec or MlScoreAtEntry"

    # The broker's attached stop closing the position means our own supervision
    # stack (GBP exit / virtual stop / trail) never acted. That is a supervision
    # defect, not a strategy decision, so it must not be scored as LOGIC.
    if authority == EXIT_AUTHORITY_BROKER:
        return (
            "APP",
            "POLICY_BREACH: SUPERVISION_GAP",
        )

    exit_l = (lc.exit_reason or "").lower()
    logic_exit = any(
        tok in exit_l
        for tok in (
            "soft_loss",
            "trail",
            "long_trade",
            "long_runner",
            "ltr",
            "dynamic_limit",
            "open_position",
            "gbp_exit",
        )
    )
    excluded = set(ctx.exclude_from_hot_path)
    epic_ok = bool(lc.epic) and lc.epic not in excluded and "NIKKEI" not in (lc.epic or "").upper()
    if epic_ok and (logic_exit or lc.engine_origin not in (None, "", "broker_attached")):
        # Only a *measured* short hold indicates micro masquerade. A zero hold
        # recovered from a sync artifact is unmeasured, not a fast scalp.
        if (
            hold_usable
            and lc.hold_sec is not None
            and lc.hold_sec < ctx.micro_hold_breach_sec
            and ctx.path_a_claimed
        ):
            return "APP", "short hold under Path A on allowed epic (micro masquerade)"
        return "LOGIC", "policy path stamps consistent; loss from exit/risk math"

    if epic_ok and ml_usable and hold_usable:
        return "LOGIC", "trustworthy stamps on allowed epic; treat as strategy loss"

    return "UNKNOWN", "insufficient attribution to separate APP vs LOGIC"


def reconstruct_lifecycle(
    deal_id: str,
    *,
    data_root: Path | None = None,
    ctx: PolicyContext | None = None,
    journal_row: dict[str, Any] | None = None,
    ml_index: dict[str, dict[str, Any]] | None = None,
    learning_index: dict[str, dict[str, Any]] | None = None,
) -> TradeLifecycle:
    root = data_root or default_data_root()
    policy = ctx or load_policy_context(root)
    lc = TradeLifecycle(deal_id=deal_id)

    # Journal
    jrow = journal_row
    if jrow is None:
        rows = load_journal_rows(root, deal_id=deal_id)
        if rows:
            jrow = max(rows, key=_row_score)
    if jrow:
        lc.sources.append("daily_journal")
        lc.timestamp = _s(jrow.get("Timestamp"))
        lc.direction = _s(jrow.get("Direction"))
        lc.entry_price = _f(jrow.get("EntryPrice"))
        lc.exit_price = _f(jrow.get("ExitPrice"))
        lc.pnl_gbp = _f(jrow.get("RealizedPnL_GBP"))
        lc.account_id = _s(jrow.get("AccountID"))
        lc.product_type = _s(jrow.get("ProductType"))
        lc.engine_origin = _s(jrow.get("EngineOrigin"))
        lc.exit_reason = _s(jrow.get("ExitReason"))
        lc.hold_sec = _f(jrow.get("HoldSec"))
        lc.style = _s(jrow.get("Style"))
        lc.ml_score_at_entry = _f(jrow.get("MlScoreAtEntry"))
        lc.market_regime = _s(jrow.get("MarketRegime"))
        lc.events.append(
            LifecycleEvent(
                stage="journal_close",
                ts=lc.timestamp,
                detail=f"pnl={lc.pnl_gbp} origin={lc.engine_origin} exit={lc.exit_reason}",
                source="daily_journal",
            )
        )

    # ML outcomes
    ml_idx = ml_index if ml_index is not None else load_ml_outcomes_index(root)
    ml = None
    for alias in _deal_aliases(deal_id):
        if alias in ml_idx:
            ml = ml_idx[alias]
            break
    if ml:
        lc.sources.append("ml_trade_outcomes")
        if lc.ml_score_at_entry is None:
            lc.ml_score_at_entry = _f(ml.get("ml_score_at_entry") if ml.get("ml_score_at_entry") is not None else ml.get("ml_score"))
        if not lc.market_regime:
            lc.market_regime = _s(ml.get("market_regime") or ml.get("regime"))
        if lc.hold_sec is None:
            lc.hold_sec = _f(ml.get("hold_sec") if ml.get("hold_sec") is not None else ml.get("hold_duration_seconds"))
        if not lc.epic:
            lc.epic = _s(ml.get("epic"))
        if not lc.engine_origin:
            lc.engine_origin = _s(ml.get("engine_origin"))
        if not lc.exit_reason:
            lc.exit_reason = _s(ml.get("exit_reason"))
        if lc.pnl_gbp is None:
            lc.pnl_gbp = _f(ml.get("pnl"))
        if not lc.account_id:
            lc.account_id = _s(ml.get("account"))
        if not lc.style:
            lc.style = _s(ml.get("style"))
        lc.events.append(
            LifecycleEvent(
                stage="ml_outcome",
                ts=None,
                detail=f"ml={lc.ml_score_at_entry} regime={lc.market_regime} hold={lc.hold_sec}",
                source="ml_trade_outcomes",
            )
        )

    # Learning DB
    lidx = learning_index if learning_index is not None else load_learning_index(root)
    learn = None
    for alias in _deal_aliases(deal_id):
        if alias in lidx:
            learn = lidx[alias]
            break
    if learn is None and len(deal_id) >= 8 and deal_id[-8:] in lidx:
        learn = lidx[deal_id[-8:]]
    if learn:
        lc.sources.append("learning_db")
        if not lc.epic:
            lc.epic = _s(learn.get("epic"))
        if not lc.market:
            lc.market = _s(learn.get("market"))
        if lc.size is None:
            lc.size = _f(learn.get("size"))
        if lc.opened_at is None:
            lc.opened_at = _s(learn.get("opened_at"))
        if lc.closed_at is None:
            lc.closed_at = _s(learn.get("closed_at"))
        if lc.confidence is None:
            lc.confidence = _f(learn.get("confidence"))
        if lc.direction is None:
            lc.direction = _s(learn.get("side"))
        if lc.entry_price is None:
            lc.entry_price = _f(learn.get("entry"))
        if lc.exit_price is None:
            lc.exit_price = _f(learn.get("exit"))
        if lc.account_id is None:
            lc.account_id = _s(learn.get("account_id"))
        if lc.product_type is None:
            lc.product_type = _s(learn.get("product_type"))
        if lc.engine_origin is None:
            lc.engine_origin = _s(learn.get("engine_origin"))
        if lc.pnl_gbp is None:
            pts = _f(learn.get("pnl_points"))
            sz = _f(learn.get("size"))
            if pts is not None and sz is not None:
                lc.pnl_gbp = pts * sz
        if lc.hold_sec is None:
            lc.hold_sec = _hold_from_open_close(learn.get("opened_at"), learn.get("closed_at"))
        notes = _s(learn.get("notes"))
        if notes:
            lc.notes_excerpt = notes[:240]
        lc.events.append(
            LifecycleEvent(
                stage="learning_row",
                ts=lc.closed_at,
                detail=f"epic={lc.epic} size={lc.size} open={lc.opened_at} close={lc.closed_at}",
                source="learning_db",
            )
        )

    # Autopsy JSON
    aut = load_autopsy(root, deal_id)
    if aut:
        lc.sources.append("autopsy_json")
        if lc.ml_score_at_entry is None:
            lc.ml_score_at_entry = _f(
                aut.get("ml_score_at_entry")
                if aut.get("ml_score_at_entry") is not None
                else aut.get("confidence_at_entry")
            )
        if not lc.market_regime:
            lc.market_regime = _s(aut.get("regime_at_entry") or aut.get("market_regime"))
        if not lc.exit_reason:
            lc.exit_reason = _s(aut.get("exit_reason"))
        if lc.pnl_gbp is None:
            lc.pnl_gbp = _f(aut.get("pnl_gbp"))
        if not lc.epic:
            lc.epic = _s(aut.get("epic"))
        if lc.opened_at is None:
            lc.opened_at = _s(aut.get("entry_time"))
        if lc.closed_at is None:
            lc.closed_at = _s(aut.get("exit_time"))
        if lc.hold_sec is None:
            lc.hold_sec = _hold_from_open_close(aut.get("entry_time"), aut.get("exit_time"))
        if lc.size is None:
            lc.size = _f(aut.get("size"))
        lc.events.append(
            LifecycleEvent(
                stage="autopsy",
                ts=lc.closed_at,
                detail=f"exit={lc.exit_reason} ml={lc.ml_score_at_entry} regime={lc.market_regime}",
                source="autopsy_json",
            )
        )

    # Correct epic using market + price (learning DB has known Wall Street→Nikkei mislabels)
    corrected, epic_notes = infer_epic_from_market_price(
        epic=lc.epic,
        market=lc.market,
        entry_price=lc.entry_price,
    )
    if corrected and corrected != lc.epic:
        lc.events.append(
            LifecycleEvent(
                stage="epic_correction",
                ts=None,
                detail=f"{lc.epic} → {corrected} ({', '.join(epic_notes)})",
                source="infer_epic_from_market_price",
            )
        )
        lc.epic = corrected
    elif corrected and not lc.epic:
        lc.epic = corrected
    for n in epic_notes:
        if n not in lc.evidence_gaps:
            lc.evidence_gaps.append(n)

    # Evidence gaps
    from diagnostics.stamp_provenance import is_placeholder_regime

    if lc.ml_score_at_entry is None:
        lc.evidence_gaps.append("missing_ml_score_at_entry")
    if is_placeholder_regime(lc.market_regime):
        lc.evidence_gaps.append("missing_market_regime")
        # Do not let placeholder "UNKNOWN" masquerade as a stamped regime.
        if lc.market_regime and str(lc.market_regime).strip().upper() == "UNKNOWN":
            lc.market_regime = None
    if lc.hold_sec is None:
        lc.evidence_gaps.append("missing_hold_sec")
    if not lc.epic:
        lc.evidence_gaps.append("missing_epic")
    if not lc.engine_origin or lc.engine_origin == "broker_attached":
        lc.evidence_gaps.append("weak_engine_attribution")
    if not lc.exit_reason or lc.exit_reason == "broker_attached":
        lc.evidence_gaps.append("weak_exit_reason")

    # Signal/gate synthetic events
    lc.events.insert(
        0,
        LifecycleEvent(
            stage="entry_signal",
            ts=lc.opened_at or lc.timestamp,
            detail=(
                f"epic={lc.epic} ml={lc.ml_score_at_entry} regime={lc.market_regime} "
                f"path={'PathA/macro' if (lc.engine_origin or '').upper()=='MACRO_SENTINEL' or (lc.style or '').lower()=='macro' else (lc.engine_origin or 'unknown')}"
            ),
            source="reconstructed",
        ),
    )
    lc.events.append(
        LifecycleEvent(
            stage="fill",
            ts=lc.opened_at or lc.timestamp,
            detail=f"side={lc.direction} size={lc.size} entry={lc.entry_price} account={lc.account_id} product={lc.product_type}",
            source="reconstructed",
        ),
    )
    lc.events.append(
        LifecycleEvent(
            stage="exit",
            ts=lc.closed_at or lc.timestamp,
            detail=f"reason={lc.exit_reason} hold_sec={lc.hold_sec} pnl_gbp={lc.pnl_gbp}",
            source="reconstructed",
        ),
    )

    from diagnostics.stamp_provenance import (
        classify_exit_authority,
        hold_is_measurable,
        ml_score_is_usable,
    )

    lc.exit_authority = classify_exit_authority(
        exit_reason=lc.exit_reason or "",
        engine_origin=lc.engine_origin or "",
    )
    _prov_row = {
        "hold_sec": lc.hold_sec,
        "exit_reason": lc.exit_reason,
        "engine_origin": lc.engine_origin,
        "ml_score_at_entry": lc.ml_score_at_entry,
    }
    lc.hold_sec_trusted = hold_is_measurable(_prov_row)
    lc.ml_score_trusted = ml_score_is_usable(_prov_row)

    lc.policy_breaches = classify_policy_breaches(lc, policy)
    if lc.pnl_gbp is not None and lc.pnl_gbp < 0:
        lc.loss_class, lc.loss_class_reason = classify_loss(lc, policy)
    return lc


def hold_bucket(hold: float | None) -> str:
    if hold is None:
        return "unknown"
    if hold < 10:
        return "<10s"
    if hold < 60:
        return "10-60s"
    if hold < 180:
        return "1-3m"
    if hold < 600:
        return "3-10m"
    return ">=10m"


def normalize_exit_reason(reason: str | None) -> str:
    r = (reason or "").strip()
    if not r:
        return "unstamped"
    low = r.lower()
    if "soft_loss" in low:
        return "soft_loss"
    if "trail" in low:
        return "trail"
    if "long_trade" in low or "long_runner" in low or "ltr" in low:
        return "ltr"
    if "broker_attached" in low:
        return "broker_attached"
    if "broker" in low or "ig_transaction" in low or "manual/external" in low:
        return "broker"
    if "micro_gbp" in low or "gbp_exit" in low:
        return "gbp_exit"
    if "open_position" in low:
        return "open_position_actions"
    return r[:48]


# ---------------------------------------------------------------------------
# Loss autopsy report
# ---------------------------------------------------------------------------


GOLDEN_PATH_A = """
### Golden path — correct Path A / MACRO_SENTINEL trade

A fundamentals-correct SB macro trade should look like:

1. **Epic** on ranked / hot path (DOW preferred; Gold/EUR/FTSE when promoted) — **never** Nikkei while `exclude_from_hot_path` contains `IX.D.NIKKEI.IFM.IP`.
2. **EngineOrigin** = `MACRO_SENTINEL` (Path A carve: Instant + Core-B micro **hard off** on SB).
3. **Stamps at entry**: `MlScoreAtEntry` present (selectivity / ElasticGate band), `MarketRegime` present.
4. **HoldSec** ≥ ~150s before trail arm, and typically ≥ 180s if LTR / Trend-Retention engages — **not** sub-10s scalp.
5. **Exit** attributed: `soft_loss` / trail / LTR / supervised GBP stack — not an anonymous `broker_attached` stub.
6. **Account** = Z6BAH3 SPREADBET; CFD QUANT_SNIPER only when A2 pause is **inactive**.

Losers that violate (1)–(5) are **APP** (policy not followed). Losers that satisfy stamps + allowed epic and die on soft_loss/trail are **LOGIC** (strategy loss). Missing stamps → **UNKNOWN** (evidence gap).
""".strip()


def build_loss_autopsy(
    *,
    day: str,
    data_root: Path | None = None,
    top_n: int = 8,
    since_reopen: bool = False,
) -> dict[str, Any]:
    root = data_root or default_data_root()
    ctx = load_policy_context(root)

    reopen_meta: dict[str, Any] | None = None
    reopen_path = root / "state" / "operator_reopen_witness.json"
    reopen_epoch: float | None = None
    if reopen_path.is_file():
        try:
            reopen_meta = json.loads(reopen_path.read_text(encoding="utf-8"))
            reopen_epoch = float(reopen_meta.get("reopened_at_epoch") or 0) or None
        except Exception:
            reopen_meta = None

    journal_rows = load_journal_rows(root, day=day)
    by_deal = dedupe_journal_by_deal(journal_rows)
    ml_idx = load_ml_outcomes_index(root)
    learn_idx = load_learning_index(root, day=day)

    # Also include learning-only losers missing from journal
    for key, row in list(learn_idx.items()):
        if len(key) < 10:
            continue  # skip short alias keys for primary iteration
        did = _s(row.get("ig_deal_id")) or ""
        if not did:
            continue
        # Prefer full DI* journal ids when present
        full = None
        for jid in by_deal:
            if jid.endswith(did) or did.endswith(jid[-8:] if len(jid) >= 8 else jid):
                full = jid
                break
        deal_key = full or (did if did.startswith("DI") else f"DIAAAAX{did}" if len(did) < 20 else did)
        if deal_key not in by_deal and did not in by_deal:
            # synthesize minimal journal-like row from learning
            pts = _f(row.get("pnl_points"))
            sz = _f(row.get("size"))
            pnl = pts * sz if pts is not None and sz is not None else None
            if pnl is None or pnl >= 0:
                continue
            closed = _s(row.get("closed_at")) or ""
            if day and day not in closed:
                continue
            by_deal[deal_key] = {
                "Timestamp": closed.replace(" ", "T") + ("Z" if "Z" not in closed and "+" not in closed else ""),
                "DealID": deal_key,
                "Direction": _s(row.get("side")),
                "EntryPrice": row.get("entry"),
                "ExitPrice": row.get("exit"),
                "RealizedPnL_GBP": pnl,
                "AccountID": row.get("account_id"),
                "ProductType": row.get("product_type"),
                "EngineOrigin": row.get("engine_origin"),
                "ExitReason": "",
                "HoldSec": "",
                "Style": "",
                "MlScoreAtEntry": "",
                "MarketRegime": "",
            }

    lifecycles: list[TradeLifecycle] = []
    for did, jrow in by_deal.items():
        lc = reconstruct_lifecycle(
            did,
            data_root=root,
            ctx=ctx,
            journal_row=jrow,
            ml_index=ml_idx,
            learning_index=learn_idx,
        )
        if since_reopen and reopen_epoch:
            ts = _parse_ts(lc.timestamp or lc.closed_at)
            if ts is None or ts.timestamp() < reopen_epoch:
                continue
        lifecycles.append(lc)

    losers = [lc for lc in lifecycles if (lc.pnl_gbp is not None and lc.pnl_gbp < 0)]
    winners = [lc for lc in lifecycles if (lc.pnl_gbp is not None and lc.pnl_gbp > 0)]
    flat = [lc for lc in lifecycles if (lc.pnl_gbp is not None and abs(lc.pnl_gbp) < 1e-9)]

    for lc in losers:
        if not lc.loss_class:
            lc.loss_class, lc.loss_class_reason = classify_loss(lc, ctx)

    by_exit: Counter[str] = Counter(normalize_exit_reason(lc.exit_reason) for lc in losers)
    by_epic: Counter[str] = Counter((lc.epic or "unknown") for lc in losers)
    by_hold: Counter[str] = Counter(
        hold_bucket(lc.hold_sec) if lc.hold_sec_trusted else "unmeasured" for lc in losers
    )
    by_class: Counter[str] = Counter((lc.loss_class or "UNKNOWN") for lc in losers)
    by_account: Counter[str] = Counter((lc.account_id or "unknown") for lc in losers)
    by_authority: Counter[str] = Counter((lc.exit_authority or "unknown") for lc in losers)
    authority_pnl: dict[str, float] = {}
    for lc in losers:
        key = lc.exit_authority or "unknown"
        authority_pnl[key] = round(authority_pnl.get(key, 0.0) + (lc.pnl_gbp or 0.0), 2)

    # APP subtype rollup — operator-actionable (beyond APP/LOGIC/UNKNOWN)
    # Multi-label counts (a loser may contribute to several codes) plus a
    # single primary subtype per loser for exclusive £ attribution.
    _PRIMARY_PRIORITY = (
        "EXCLUDED_EPIC",
        "CFD_ENTRY_WHILE_A2_PAUSED",
        "RISK_STACK_DID_NOT_CUT",
        "HOLD_LT_MACRO_INTENT",
        "INSTANT_MICRO_WHILE_DISABLED",
        "SUPERVISION_GAP",
        "APP_OTHER",
        "UNKNOWN_GAP",
    )
    app_subtypes: dict[str, dict[str, float | int]] = {}
    app_subtypes_primary: dict[str, dict[str, float | int]] = {}
    for lc in losers:
        codes = [
            b.code
            for b in (lc.policy_breaches or [])
            if getattr(b, "severity", "") == "fail"
        ]
        if not codes and (lc.loss_class or "") == "APP":
            reason = str(lc.loss_class_reason or "")
            if "SUPERVISION_GAP" in reason:
                codes = ["SUPERVISION_GAP"]
            elif "RISK_STACK" in reason:
                codes = ["RISK_STACK_DID_NOT_CUT"]
            else:
                codes = ["APP_OTHER"]
        for code in codes or (
            ["UNKNOWN_GAP"] if (lc.loss_class or "") == "UNKNOWN" else []
        ):
            bucket = app_subtypes.setdefault(code, {"count": 0, "pnl_gbp": 0.0})
            bucket["count"] = int(bucket["count"]) + 1
            bucket["pnl_gbp"] = round(
                float(bucket["pnl_gbp"]) + float(lc.pnl_gbp or 0.0), 2
            )
        primary = next((c for c in _PRIMARY_PRIORITY if c in codes), None)
        if primary is None and (lc.loss_class or "") == "UNKNOWN":
            primary = "UNKNOWN_GAP"
        if primary is None and (lc.loss_class or "") == "APP":
            primary = "APP_OTHER"
        if primary:
            pb = app_subtypes_primary.setdefault(
                primary, {"count": 0, "pnl_gbp": 0.0}
            )
            pb["count"] = int(pb["count"]) + 1
            pb["pnl_gbp"] = round(
                float(pb["pnl_gbp"]) + float(lc.pnl_gbp or 0.0), 2
            )

    top = sorted(losers, key=lambda x: x.pnl_gbp or 0.0)[: int(top_n)]

    net = sum(lc.pnl_gbp or 0.0 for lc in lifecycles)
    loss_net = sum(lc.pnl_gbp or 0.0 for lc in losers)

    # Evidence gap summary
    from diagnostics.stamp_provenance import is_placeholder_regime

    gap_n = sum(1 for lc in losers if lc.evidence_gaps)
    ml_missing = sum(1 for lc in losers if lc.ml_score_at_entry is None)
    hold_missing = sum(1 for lc in losers if lc.hold_sec is None)
    regime_missing = sum(1 for lc in losers if is_placeholder_regime(lc.market_regime))

    report: dict[str, Any] = {
        "day": day,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "data_root": str(root),
        "since_reopen": since_reopen,
        "reopen_witness": reopen_meta,
        "policy_context": asdict(ctx),
        "summary": {
            "closes": len(lifecycles),
            "winners": len(winners),
            "losers": len(losers),
            "flat": len(flat),
            "net_gbp": round(net, 2),
            "loss_net_gbp": round(loss_net, 2),
            "wr_pct": round(100.0 * len(winners) / len(lifecycles), 1) if lifecycles else None,
            "by_loss_class": dict(by_class),
            "by_exit_reason": dict(by_exit),
            "by_epic": dict(by_epic),
            "by_hold_bucket": dict(by_hold),
            "by_account": dict(by_account),
            "by_exit_authority": dict(by_authority),
            "exit_authority_pnl_gbp": authority_pnl,
            "app_subtypes": app_subtypes,
            "app_subtypes_primary": app_subtypes_primary,
            "evidence_gaps": {
                "losers_with_any_gap": gap_n,
                "missing_ml_stamp": ml_missing,
                "missing_hold_sec": hold_missing,
                "missing_regime": regime_missing,
                "untrusted_hold_stamp": sum(1 for lc in losers if not lc.hold_sec_trusted),
                "untrusted_ml_stamp": sum(1 for lc in losers if not lc.ml_score_trusted),
            },
        },
        "golden_path_a": GOLDEN_PATH_A,
        "top_losers": [lc.to_dict() for lc in top],
        # Compact all-loser list for shadow_loss_loop (no event spam).
        "losers": [_loser_compact(lc) for lc in losers],
        "fundamentals_followed": _fundamentals_verdict(by_class, by_epic, ctx),
    }
    try:
        from diagnostics.direction_quality import score_direction_quality
        from system.paths import data_dir as _dd

        journal = Path(_dd()) / "metrics" / "daily_journal.csv"
        if not journal.is_file():
            journal = root / "metrics" / "daily_journal.csv"
        report["direction_quality"] = score_direction_quality(
            [_loser_compact(lc) for lc in losers],
            journal_path=journal,
        )
    except Exception as exc:
        report["direction_quality"] = {
            "verdict": f"unavailable: {type(exc).__name__}",
            "error": str(exc),
        }
    return report


def _loser_compact(lc: TradeLifecycle) -> dict[str, Any]:
    """Fields needed for APP/LOGIC split + ML shadow counterfactual."""
    return {
        "deal_id": lc.deal_id,
        "timestamp": lc.timestamp,
        "direction": lc.direction,
        "pnl_gbp": lc.pnl_gbp,
        "account_id": lc.account_id,
        "product_type": lc.product_type,
        "engine_origin": lc.engine_origin,
        "exit_reason": lc.exit_reason,
        "hold_sec": lc.hold_sec,
        "style": lc.style,
        "ml_score_at_entry": lc.ml_score_at_entry,
        "market_regime": lc.market_regime,
        "epic": lc.epic,
        "market": lc.market,
        "size": lc.size,
        "confidence": lc.confidence,
        "loss_class": lc.loss_class,
        "loss_class_reason": lc.loss_class_reason,
        "exit_authority": lc.exit_authority,
        "hold_sec_trusted": lc.hold_sec_trusted,
        "ml_score_trusted": lc.ml_score_trusted,
        "evidence_gaps": list(lc.evidence_gaps or []),
        "policy_breach_codes": [
            b.code for b in (lc.policy_breaches or []) if getattr(b, "severity", "") == "fail"
        ],
    }


def _fundamentals_verdict(
    by_class: Counter[str],
    by_epic: Counter[str],
    ctx: PolicyContext,
) -> dict[str, Any]:
    app = int(by_class.get("APP", 0))
    logic = int(by_class.get("LOGIC", 0))
    unknown = int(by_class.get("UNKNOWN", 0))
    total = app + logic + unknown
    nikkei = sum(v for k, v in by_epic.items() if "NIKKEI" in k.upper())
    followed = False
    if total == 0:
        verdict = "NO_LOSSES"
    elif app >= max(logic, 1) and app >= unknown:
        verdict = "NO — APP policy breaches dominate losers"
    elif unknown > app and unknown >= logic:
        verdict = "UNCLEAR — evidence gaps dominate (stamps missing)"
    elif logic > app and logic >= unknown:
        verdict = "PARTIAL — most losers look like strategy/exit math (LOGIC)"
        followed = True
    else:
        verdict = "MIXED — APP breaches and evidence gaps both material"
    return {
        "verdict": verdict,
        "fundamentals_followed": followed,
        "app": app,
        "logic": logic,
        "unknown": unknown,
        "nikkei_losers": nikkei,
        "path_a_claimed": ctx.path_a_claimed,
        "excluded": list(ctx.exclude_from_hot_path),
    }


def render_loss_autopsy_markdown(report: dict[str, Any]) -> str:
    day = report["day"]
    s = report["summary"]
    fund = report["fundamentals_followed"]
    lines: list[str] = []
    lines.append(f"# Loss Autopsy — {day}")
    lines.append("")
    lines.append(f"- Generated: `{report['generated_at']}`")
    lines.append(f"- Data root: `{report['data_root']}`")
    lines.append(f"- Mode: **READ-ONLY** (no orders, no bleed-lock mutation, no agent restart)")
    if report.get("since_reopen"):
        lines.append("- Window: **since operator reopen witness**")
    else:
        lines.append("- Window: **calendar day** (all closes)")
    rw = report.get("reopen_witness") or {}
    if rw:
        lines.append(
            f"- Reopen witness: `{rw.get('reopened_at')}` day_net_at_reopen=`{rw.get('day_net_at_reopen_gbp')}` "
            f"(sibling owns reopen; this report does not resume trading)"
        )
    lines.append("")
    lines.append("## Pre-dev audit (read-only)")
    lines.append("")
    lines.append("| Check | Result |")
    lines.append("|---|---|")
    lines.append("| Book / sessions | Autopsy uses journal+learning; no flatten/restart |")
    lines.append("| Bleed locks | Not touched (sibling owns reopen) |")
    lines.append("| Active PIDs | Left alone |")
    lines.append("")
    lines.append("## Verdict — were fundamentals followed?")
    lines.append("")
    lines.append(f"**{fund['verdict']}**")
    lines.append("")
    lines.append(
        f"- APP={fund['app']} · LOGIC={fund['logic']} · UNKNOWN={fund['unknown']} · "
        f"Nikkei losers={fund['nikkei_losers']}"
    )
    lines.append(f"- Path A carve claimed (SB Instant/micro off): `{fund['path_a_claimed']}`")
    lines.append(f"- Excluded hot-path: `{', '.join(fund['excluded'])}`")
    lines.append("")
    dq = report.get("direction_quality") or {}
    if dq:
        lines.append("## Direction quality (BUY/SELL from inception)")
        lines.append("")
        lines.append(f"**{dq.get('verdict')}**")
        lines.append("")
        lines.append(
            f"- Sides: `{dq.get('side_counts')}` · priced={dq.get('priced')} · "
            f"adverse_to_side={dq.get('adverse_to_side')} · "
            f"favorable_but_lost={dq.get('favorable_price_but_lost')}"
        )
        lines.append(
            f"- Weak/missing ML among adverse: `{dq.get('weak_ml_among_adverse')}` · "
            f"wire_inversion_suspected=`{dq.get('wire_inversion_suspected')}`"
        )
        lines.append(
            f"- Adverse pts median — SELL `{dq.get('sell_adverse_pts_median')}` · "
            f"BUY `{dq.get('buy_adverse_pts_median')}`"
        )
        lines.append("")
        lines.append(
            "_If wire_inversion_suspected is false, the side chosen matched the loss "
            "(SELL into a rise / BUY into a drop). Focus ML floor + APP stack, not a "
            "BUY↔SELL swap._"
        )
        lines.append("")
    lines.append("### Classification key")
    lines.append("")
    lines.append("| Class | Meaning |")
    lines.append("|---|---|")
    lines.append("| **APP** | App violated configured policy (excluded epic, micro while disabled, CFD under A2 pause, hold≪macro intent) |")
    lines.append("| **LOGIC** | Stamps show allowed path; loss from soft_loss/trail/LTR/broker exit math |")
    lines.append("| **UNKNOWN** | Evidence gap — missing ML/hold/regime/engine attribution; cannot certify fundamentals |")
    lines.append("")
    lines.append("## Day summary")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|---|---:|")
    lines.append(f"| Closes (deduped) | {s['closes']} |")
    lines.append(f"| Winners / Losers / Flat | {s['winners']} / {s['losers']} / {s['flat']} |")
    lines.append(f"| Win rate | {s['wr_pct']}% |")
    lines.append(f"| Net £ | {s['net_gbp']} |")
    lines.append(f"| Loss-leg £ | {s['loss_net_gbp']} |")
    lines.append("")
    lines.append("### Losers by class")
    lines.append("")
    lines.append("| Class | n |")
    lines.append("|---|---:|")
    for k, v in sorted((s.get("by_loss_class") or {}).items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| {k} | {v} |")
    lines.append("")
    app_st = s.get("app_subtypes_primary") or s.get("app_subtypes") or {}
    if app_st:
        lines.append("### APP / supervision subtypes (actionable, primary)")
        lines.append("")
        lines.append("| Subtype | n | PnL £ |")
        lines.append("|---|---:|---:|")
        for k, row in sorted(
            app_st.items(),
            key=lambda kv: (-int(kv[1].get("count") or 0), kv[0]),
        ):
            lines.append(
                f"| `{k}` | {row.get('count', 0)} | {row.get('pnl_gbp', 0)} |"
            )
        lines.append("")
        lines.append(
            "_Primary = exclusive per loser (priority: excluded → CFD/A2 → risk-stack "
            "→ hold≪macro → supervision gap). RISK_STACK / SUPERVISION_GAP → "
            "unmonitored grace flatten. CFD_ENTRY_WHILE_A2_PAUSED → keep A2 hard-block. "
            "EXCLUDED_EPIC → hot-path gate._"
        )
        lines.append("")
    lines.append("### Losers by exit reason (normalized)")
    lines.append("")
    lines.append("| Exit | n |")
    lines.append("|---|---:|")
    for k, v in sorted((s.get("by_exit_reason") or {}).items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| `{k}` | {v} |")
    lines.append("")
    lines.append("### Losers by epic")
    lines.append("")
    lines.append("| Epic | n |")
    lines.append("|---|---:|")
    for k, v in sorted((s.get("by_epic") or {}).items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| `{k}` | {v} |")
    lines.append("")
    lines.append("### Losers by hold bucket")
    lines.append("")
    lines.append("| Hold | n |")
    lines.append("|---|---:|")
    for k, v in sorted((s.get("by_hold_bucket") or {}).items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append("### Evidence gaps (losers)")
    lines.append("")
    eg = s.get("evidence_gaps") or {}
    lines.append("| Gap | n |")
    lines.append("|---|---:|")
    lines.append(f"| Any gap | {eg.get('losers_with_any_gap')} |")
    lines.append(f"| Missing MlScoreAtEntry | {eg.get('missing_ml_stamp')} |")
    lines.append(f"| Missing HoldSec | {eg.get('missing_hold_sec')} |")
    lines.append(f"| Missing MarketRegime | {eg.get('missing_regime')} |")
    if eg.get("missing_ml_stamp") or eg.get("missing_hold_sec"):
        lines.append("")
        lines.append(
            "> **Evidence gap (explicit):** many losers lack native journal ML/Hold/Regime stamps. "
            "Epic join uses learning DB + market/price correction (Wall Street rows mislabelled "
            "as Nikkei are remapped to DOW). `MarketRegime` is almost never stamped today. "
            "`broker_attached` rows without engine/hold/ML may be **UNKNOWN** unless an APP "
            "breach is still proven (true Nikkei, CFD under A2 pause, hold≪macro under Path A)."
        )
    lines.append("")
    lines.append(GOLDEN_PATH_A)
    lines.append("")
    lines.append("### Golden path vs today's losers")
    lines.append("")
    lines.append("| Expectation (Path A) | What losers did |")
    lines.append("|---|---|")
    nikkei = fund.get("nikkei_losers") or 0
    hold_b = s.get("by_hold_bucket") or {}
    short = int(hold_b.get("<10s") or 0) + int(hold_b.get("10-60s") or 0)
    dow_n = int((s.get("by_epic") or {}).get(DOW_EPIC) or 0)
    lines.append(
        f"| Hot-path epic (DOW / ranked) | DOW losers≈**{dow_n}**; true Nikkei losers=**{nikkei}** (excluded→APP) |"
    )
    lines.append(
        f"| Hold ≥ 150–180s macro | **{short}** losers with hold <60s — micro masquerade under Path A (**APP**) |"
    )
    lines.append(
        f"| Stamped ML + regime + engine | missing ML={eg.get('missing_ml_stamp')}, "
        f"regime={eg.get('missing_regime')}, hold={eg.get('missing_hold_sec')} |"
    )
    lines.append(
        f"| Exit soft_loss/trail/LTR attributed | exit mix dominated by "
        f"`{max((s.get('by_exit_reason') or {'unstamped': 0}).items(), key=lambda kv: kv[1])[0]}` |"
    )
    lines.append("")
    lines.append("## Top losers — lifecycle deep dive")
    lines.append("")
    for i, lc in enumerate(report.get("top_losers") or [], 1):
        lines.append(
            f"### {i}. `{lc.get('deal_id')}` · £{lc.get('pnl_gbp')} · "
            f"**{lc.get('loss_class') or 'UNKNOWN'}**"
        )
        lines.append("")
        lines.append(f"- Reason: {lc.get('loss_class_reason')}")
        lines.append(
            f"- Epic: `{lc.get('epic')}` · Account: `{lc.get('account_id')}` · "
            f"Product: `{lc.get('product_type')}` · Size: `{lc.get('size')}`"
        )
        lines.append(
            f"- Engine: `{lc.get('engine_origin')}` · Style: `{lc.get('style')}` · "
            f"Exit: `{lc.get('exit_reason')}`"
        )
        lines.append(
            f"- ML@entry: `{lc.get('ml_score_at_entry')}` · Regime: `{lc.get('market_regime')}` · "
            f"HoldSec: `{lc.get('hold_sec')}`"
        )
        lines.append(
            f"- Side: `{lc.get('direction')}` entry=`{lc.get('entry_price')}` "
            f"exit=`{lc.get('exit_price')}`"
        )
        lines.append(f"- Opened: `{lc.get('opened_at')}` · Closed: `{lc.get('closed_at') or lc.get('timestamp')}`")
        lines.append(f"- Sources: {', '.join(lc.get('sources') or [])}")
        gaps = lc.get("evidence_gaps") or []
        if gaps:
            lines.append(f"- Evidence gaps: {', '.join(gaps)}")
        breaches = lc.get("policy_breaches") or []
        if breaches:
            lines.append("- POLICY_BREACH flags:")
            for b in breaches:
                lines.append(f"  - `{b.get('code')}` ({b.get('severity')}): {b.get('detail')}")
        lines.append("- Timeline:")
        for ev in lc.get("events") or []:
            lines.append(
                f"  - `{ev.get('stage')}` @ `{ev.get('ts')}` — {ev.get('detail')} _{ev.get('source')}_"
            )
        if lc.get("notes_excerpt"):
            lines.append(f"- Notes: {lc.get('notes_excerpt')}")
        lines.append("")

    lines.append("## Findings (operator handoff)")
    lines.append("")
    findings = _bullet_findings(report)
    for f in findings:
        lines.append(f"- {f}")
    lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```bash")
    lines.append(
        "PYTHONPATH=src IG_AGENT_CONFIG=config/config_v31_demo_throughput.json \\"
    )
    lines.append(
        f"  .venv/bin/python3 scripts/trade_lifecycle_witness.py --loss-autopsy --day {day} --write"
    )
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _bullet_findings(report: dict[str, Any]) -> list[str]:
    s = report["summary"]
    fund = report["fundamentals_followed"]
    by_epic = s.get("by_epic") or {}
    nikkei = sum(v for k, v in by_epic.items() if "NIKKEI" in str(k).upper())
    dow = sum(v for k, v in by_epic.items() if "DOW" in str(k).upper())
    hold_b = s.get("by_hold_bucket") or {}
    short = int(hold_b.get("<10s") or 0) + int(hold_b.get("10-60s") or 0)
    eg = s.get("evidence_gaps") or {}
    by_class = s.get("by_loss_class") or {}
    out = [
        f"Fundamentals followed? **{fund['verdict']}** "
        f"(APP={fund['app']}, LOGIC={fund['logic']}, UNKNOWN={fund['unknown']}).",
        f"Day net ≈ £{s['net_gbp']} on {s['closes']} deduped closes "
        f"(WR {s['wr_pct']}%, losers={s['losers']}).",
        f"After epic correction (Wall Street/DOW prices mislabelled Nikkei in learning DB): "
        f"DOW losers≈{dow}, true Nikkei losers≈{nikkei} (excluded → APP).",
        f"Hold profile: {short} losers held <60s — incompatible with Path A / macro intent (150–180s); "
        f"exits dominated by `{max((s.get('by_exit_reason') or {'unstamped':0}).items(), key=lambda kv: kv[1])[0]}`.",
        f"Evidence gap: missing ML={eg.get('missing_ml_stamp')}, "
        f"HoldSec={eg.get('missing_hold_sec')}, Regime={eg.get('missing_regime')} "
        f"(UNKNOWN={by_class.get('UNKNOWN', 0)} when stamps block APP/LOGIC proof).",
    ]
    return out


def write_loss_autopsy(
    *,
    day: str,
    data_root: Path | None = None,
    top_n: int = 8,
    since_reopen: bool = False,
    write_json: bool = True,
) -> tuple[Path, Path | None, dict[str, Any]]:
    root = data_root or default_data_root()
    report = build_loss_autopsy(
        day=day, data_root=root, top_n=top_n, since_reopen=since_reopen
    )
    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"loss_autopsy_{day}.md"
    md_path.write_text(render_loss_autopsy_markdown(report), encoding="utf-8")
    json_path: Path | None = None
    if write_json:
        json_path = reports_dir / f"loss_autopsy_{day}.json"
        json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return md_path, json_path, report
