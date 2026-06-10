"""Log IG account binding for DEMO/LIVE credentials (no secrets)."""

from __future__ import annotations

from typing import Any

from system.credentials_loader import Credentials
from system.engine_log import log_engine


def log_configured_account(credentials: Credentials) -> None:
    """Emit which account id/type the agent will use."""
    log_engine(
        "IG credentials: "
        f"account_id={credentials.masked_account_id()} "
        f"type={credentials.account_type} "
        f"user={credentials.masked_username()}"
    )


def verify_account_on_broker(rest_client: Any, credentials: Credentials) -> dict[str, Any]:
    """
    Compare credentials.json account_id to GET /accounts.

    GTSHL = spread bet (IG TARGET); Z6BAH4 = CFD demo-deal — agent must use CFD epic set.
    """
    log_configured_account(credentials)
    configured = str(credentials.ig_account_id or "").strip().upper()
    out: dict[str, Any] = {
        "configured_account_id": configured,
        "configured_account_type": credentials.account_type,
        "accounts": [],
        "match": False,
        "preferred_name": "",
    }
    try:
        rest_client.ensure_session()
        r = rest_client.request("GET", "/accounts", headers=rest_client._auth_headers("1"))
        if r.status_code != 200:
            log_engine(f"IG accounts lookup failed: HTTP {r.status_code}")
            return out
        for acc in r.json().get("accounts") or []:
            aid = str(acc.get("accountId") or "").strip().upper()
            name = str(acc.get("accountName") or "")
            acc_type = str(acc.get("accountType") or "")
            out["accounts"].append(
                {"account_id": aid, "account_name": name, "account_type": acc_type}
            )
            if aid == configured:
                out["match"] = True
                out["preferred_name"] = name
    except Exception as exc:
        log_engine(f"IG accounts lookup error: {type(exc).__name__}: {exc}")
        return out

    if out["match"]:
        log_engine(
            f"IG account verified: {configured} ({out.get('preferred_name') or 'ok'}) "
            f"on {credentials.account_type}"
        )
    else:
        ids = ", ".join(a["account_id"] for a in out["accounts"]) or "none"
        log_engine(
            f"IG account MISMATCH: credentials id {configured} not in broker list [{ids}]"
        )
    return out
