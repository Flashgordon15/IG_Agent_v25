"""
Secure IG credentials loader — config/credentials/credentials.json only.

Never logs passwords or API keys.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from system.paths import config_dir


class CredentialsError(Exception):
    """Raised when credentials are missing or invalid."""


REQUIRED_FIELDS = (
    "ig_api_key",
    "ig_username",
    "ig_password",
    "ig_account_type",
    "ig_account_id",
)

# Legacy / alternate key names (values are never logged)
_FIELD_ALIASES: dict[str, str] = {
    "api_key": "ig_api_key",
    "username": "ig_username",
    "password": "ig_password",
    "account_type": "ig_account_type",
    "account_id": "ig_account_id",
}

CREDENTIALS_PATH = config_dir() / "credentials" / "credentials.json"


@dataclass(frozen=True)
class Credentials:
    ig_api_key: str
    ig_username: str
    ig_password: str
    ig_account_type: str
    ig_account_id: str

    @property
    def account_type(self) -> str:
        return self.ig_account_type.upper().strip()

    def masked_username(self) -> str:
        u = self.ig_username
        if len(u) <= 2:
            return "*" * len(u)
        return u[0] + "*" * (len(u) - 2) + u[-1]

    def masked_account_id(self) -> str:
        a = self.ig_account_id
        if len(a) <= 4:
            return "****"
        return "****" + a[-4:]

    def masked_summary(self) -> str:
        return (
            f"User: {self.masked_username()} | "
            f"Account: {self.masked_account_id()} | "
            f"Type: {self.account_type} | "
            f"API key: ********"
        )


@dataclass
class CredentialsStatus:
    ok: bool
    credentials: Credentials | None = None
    error: str = ""

    @property
    def loaded(self) -> bool:
        return self.ok and self.credentials is not None


def credentials_path() -> Path:
    return CREDENTIALS_PATH


def load_credentials(*, path: Path | None = None) -> Credentials:
    """
    Load and validate credentials.

    :raises CredentialsError: if file missing or required fields invalid.
    """
    status = try_load_credentials(path=path)
    if not status.ok or status.credentials is None:
        raise CredentialsError(status.error or "Invalid credentials")
    return status.credentials


def try_load_credentials(*, path: Path | None = None) -> CredentialsStatus:
    """Load credentials without raising; suitable for UI startup checks."""
    p = path or CREDENTIALS_PATH
    if not p.is_file():
        return CredentialsStatus(
            ok=False,
            error=f"Credentials file not found: {p}",
        )
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return CredentialsStatus(ok=False, error=f"Invalid JSON in credentials file: {e}")

    if not isinstance(raw, dict):
        return CredentialsStatus(ok=False, error="Credentials file must be a JSON object")

    raw = _normalize_fields(raw)
    missing = [f for f in REQUIRED_FIELDS if not _non_empty(raw.get(f))]
    if missing:
        return CredentialsStatus(
            ok=False,
            error=f"Missing or empty required fields: {', '.join(missing)}",
        )

    account_type = str(raw["ig_account_type"]).upper().strip()
    if account_type not in ("DEMO", "LIVE"):
        return CredentialsStatus(
            ok=False,
            error="ig_account_type must be DEMO or LIVE",
        )

    creds = Credentials(
        ig_api_key=str(raw["ig_api_key"]).strip(),
        ig_username=str(raw["ig_username"]).strip(),
        ig_password=str(raw["ig_password"]),
        ig_account_type=account_type,
        ig_account_id=str(raw["ig_account_id"]).strip(),
    )
    return CredentialsStatus(ok=True, credentials=creds)


def _normalize_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Map legacy keys to ig_* names without copying secret values to logs."""
    out = dict(raw)
    for legacy, canonical in _FIELD_ALIASES.items():
        if legacy in out and not _non_empty(out.get(canonical)):
            out[canonical] = out[legacy]
    return out


def _non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True
