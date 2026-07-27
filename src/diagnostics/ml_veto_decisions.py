"""Durable ML veto / penalty decision log for counterfactual regret analysis.

Appends structured rows under ``data_dir()/metrics/ml_veto_decisions.jsonl``.
Counterfactual labels (``counterfactual_pnl`` / ``shadow_pnl`` / ``pnl_if_taken``)
remain null until a forward-label process fills them — never infer from taken
trade PnL.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()


def veto_decisions_path(data_root: Path | None = None) -> Path:
    if data_root is not None:
        return Path(data_root) / "metrics" / "ml_veto_decisions.jsonl"
    from system.paths import data_dir

    return Path(data_dir()) / "metrics" / "ml_veto_decisions.jsonl"


def _refuse_prod_write_under_test(path: Path) -> bool:
    if not (
        os.environ.get("IG_TEST_HARNESS", "").strip() == "1"
        or os.environ.get("IG_AGENT_PYTEST", "").strip() == "1"
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    ):
        return False
    try:
        from system.paths import project_root

        resolved = path.resolve()
        prod = (project_root() / "src" / "data" / "v31-production").resolve()
        return str(resolved).startswith(str(prod) + os.sep) or resolved == prod
    except OSError:
        return False


def _quote_bid_offer(quote: Any | None) -> tuple[float | None, float | None]:
    if quote is None:
        return None, None
    bid = offer = None
    for bid_key in ("bid", "Bid"):
        if hasattr(quote, bid_key):
            try:
                bid = float(getattr(quote, bid_key))
                break
            except (TypeError, ValueError):
                pass
        if isinstance(quote, dict) and quote.get(bid_key) is not None:
            try:
                bid = float(quote[bid_key])
                break
            except (TypeError, ValueError):
                pass
    for offer_key in ("offer", "Offer", "ask", "Ask"):
        if hasattr(quote, offer_key):
            try:
                offer = float(getattr(quote, offer_key))
                break
            except (TypeError, ValueError):
                pass
        if isinstance(quote, dict) and quote.get(offer_key) is not None:
            try:
                offer = float(quote[offer_key])
                break
            except (TypeError, ValueError):
                pass
    return bid, offer


def record_ml_veto_decision(
    *,
    veto_source: str,
    action: str,
    reason: str,
    epic: str = "",
    market: str = "",
    direction: str = "",
    setup_key: str = "",
    ml_score: float | None = None,
    rules_conf: float | None = None,
    confidence_before: float | None = None,
    confidence_after: float | None = None,
    quote: Any | None = None,
    metadata: dict[str, Any] | None = None,
    data_root: Path | None = None,
    account_id: str = "",
    signal_id: str = "",
) -> str | None:
    """Append one veto/penalty row. Returns decision_id or None on skip/fail."""
    action_l = str(action or "").strip().lower()
    if action_l not in {"veto", "penalty"}:
        action_l = "veto"
    source = str(veto_source or "unknown").strip() or "unknown"
    decision_id = str(uuid.uuid4())
    bid, offer = _quote_bid_offer(quote)
    ts = time.time()
    epic_s = str(epic or "").strip()
    direction_s = str(direction or "").strip().upper()
    setup_s = str(setup_key or "").strip()
    # Join key for autopsy / regret matching (no deal_id on pre-entry vetoes).
    join_key = "|".join(
        [
            epic_s or "-",
            direction_s or "-",
            setup_s or "-",
            f"{ts:.3f}",
        ]
    )
    row: dict[str, Any] = {
        "decision_id": decision_id,
        "ts": ts,
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        "join_key": join_key,
        "signal_id": str(signal_id or "").strip() or None,
        "account_id": str(account_id or "").strip() or None,
        "epic": epic_s,
        "market": str(market or "").strip(),
        "direction": direction_s,
        "setup_key": setup_s,
        "veto_source": source,
        "action": action_l,
        "reason": str(reason or "").strip(),
        "ml_score": float(ml_score) if ml_score is not None else None,
        "rules_conf": float(rules_conf) if rules_conf is not None else None,
        "confidence_before": (
            float(confidence_before) if confidence_before is not None else None
        ),
        "confidence_after": (
            float(confidence_after) if confidence_after is not None else None
        ),
        "bid": bid,
        "offer": offer,
        # Nullable until a forward-label / shadow process fills them.
        "counterfactual_pnl": None,
        "shadow_pnl": None,
        "pnl_if_taken": None,
        "label_status": "pending",
    }
    if metadata:
        row["metadata"] = dict(metadata)

    path = veto_decisions_path(data_root)
    if _refuse_prod_write_under_test(path):
        return decision_id
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
        return decision_id
    except Exception:
        return None
