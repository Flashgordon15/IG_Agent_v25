"""Shared rules for agent-only learning labels (exclude IG sync imports)."""

from __future__ import annotations

import re
from typing import Any

_IG_SETUP_RE = re.compile(r"^(?:IG[\|_]|IMPORT)", re.IGNORECASE)
_AGENT_SOURCES = frozenset({"strategy", "shadow", "agent"})


def is_ig_import_setup_key(setup_key: str | None) -> bool:
    sk = str(setup_key or "").strip()
    if not sk:
        return False
    return bool(_IG_SETUP_RE.search(sk))


def is_agent_learning_row(row: dict[str, Any] | Any) -> bool:
    """True when a closed trade row should influence setup stats / registry."""
    if hasattr(row, "keys"):
        keys = row.keys()
        setup_key = str(row["setup_key"] if "setup_key" in keys else "")
        source = str(row["source"] if "source" in keys else "strategy")
        dry_run = row["dry_run"] if "dry_run" in keys else 0
    else:
        setup_key = str(row.get("setup_key") or "")
        source = str(row.get("source") or "strategy")
        dry_run = row.get("dry_run", 0)
    if is_ig_import_setup_key(setup_key):
        return False
    src = source.lower().strip()
    if src and src not in _AGENT_SOURCES and src not in ("", "strategy"):
        if src in ("ig_import", "ig|imported", "ig_imported", "ig transaction history"):
            return False
    if dry_run in (1, True, "1", "true"):
        return False
    return True


def agent_trades_sql_clause(*, table_alias: str = "") -> str:
    """SQL fragment excluding IG-import setup keys and non-strategy sources."""
    prefix = f"{table_alias}." if table_alias else ""
    return f"""
        {prefix}setup_key IS NOT NULL
        AND TRIM({prefix}setup_key) != ''
        AND UPPER({prefix}setup_key) NOT LIKE 'IG|%'
        AND UPPER({prefix}setup_key) NOT LIKE 'IG\\_%' ESCAPE '\\'
        AND UPPER({prefix}setup_key) NOT IN ('IG_IMPORT', 'IG|IMPORTED')
        AND ({prefix}source IS NULL OR {prefix}source IN ('strategy', 'shadow', 'agent'))
        AND COALESCE({prefix}dry_run, 0) = 0
    """
