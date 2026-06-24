"""
IG REST API client — authenticates via :class:`~system.credentials_loader.Credentials`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import requests

from ig_api.auth import AuthManager, SessionTokens
from ig_api.endpoints import position_otc, position_otc_list, update_position
from ig_api.exceptions import IGAPIError, IGAuthError, IGOrderError, RateLimitError
from system.credentials_loader import Credentials
from system.demo_execution_trace import trace_execution, update_demo_diagnostics
from system.demo_rest_log import log_demo_rest, mask_token
from system.engine_log import log_engine
from system.rate_limit_manager import get_rate_limit_manager, parse_rate_limit_error


IG_DEMO_GATEWAY = "https://demo-api.ig.com/gateway/deal"
IG_LIVE_GATEWAY = "https://api.ig.com/gateway/deal"
IG_DEMO_HOST = "demo-api.ig.com"


def normalize_ig_gateway_url(url: str, *, demo: bool = True) -> str:
    """
    Sanitize broker base URLs — reject malformed ``://://`` / ``demo-api.://`` strings.
    Always returns the verified IG gateway endpoint.
    """
    raw = str(url or "").strip()
    broken = (
        "://://" in raw
        or ".://" in raw
        or raw.startswith("https://://")
        or raw.startswith("http://://")
        or not raw
    )
    if broken or "ig.com" not in raw.lower():
        return IG_DEMO_GATEWAY if demo else IG_LIVE_GATEWAY
    if "demo-api" in raw.lower() or demo:
        if raw.rstrip("/") == f"https://{IG_DEMO_HOST}":
            return IG_DEMO_GATEWAY
        if "/gateway/deal" not in raw:
            return IG_DEMO_GATEWAY
        return raw.rstrip("/") if raw.startswith("https://") else IG_DEMO_GATEWAY
    if "/gateway/deal" not in raw:
        return IG_LIVE_GATEWAY
    return raw.rstrip("/") if raw.startswith("https://") else IG_LIVE_GATEWAY


def ig_demo_gateway_reachable(base: str | None = None) -> bool:
    """True when base resolves to the verified IG Demo REST host."""
    return IG_DEMO_HOST in str(base or IG_DEMO_GATEWAY).lower()

# Phase-4 shadow tracer / validate_order_schema — IG gateway token bucket (1.5s).
_IG_VALIDATE_THROTTLE_SEC = 1.5
_validate_throttle_lock = threading.Lock()
_validate_last_network_mono = 0.0
_validate_last_ok: dict[str, Any] | None = None


def _validate_throttle_cached_ok(payload: dict[str, Any]) -> dict[str, Any]:
    """Return last ROUTE_OPEN or RAM-only pass — no duplicate IG REST ping."""
    global _validate_last_ok
    with _validate_throttle_lock:
        if _validate_last_ok and _validate_last_ok.get("ok"):
            out = dict(_validate_last_ok)
            out["payload"] = payload
            out["throttled"] = True
            return out
    return {
        "ok": True,
        "category": "ROUTE_OPEN",
        "error": "",
        "http_status": 200,
        "payload": payload,
        "throttled": True,
        "detail": "schema_ok_ram_only — IG validate throttled 1500ms",
    }


def _validate_throttle_try_acquire() -> bool:
    """True when a network validation ping to the IG gateway may proceed."""
    global _validate_last_network_mono
    now = time.monotonic()
    with _validate_throttle_lock:
        if _validate_last_network_mono > 0.0:
            if (now - _validate_last_network_mono) < _IG_VALIDATE_THROTTLE_SEC:
                return False
        _validate_last_network_mono = now
        return True


def _validate_throttle_record_ok(result: dict[str, Any]) -> None:
    global _validate_last_ok
    if result.get("ok"):
        with _validate_throttle_lock:
            _validate_last_ok = {
                "ok": True,
                "category": "ROUTE_OPEN",
                "error": "",
                "http_status": int(result.get("http_status") or 200),
            }


class IGRestClient:
    """Synchronous IG REST client."""

    TOKEN_MAX_AGE_SECONDS = 18000  # 5h — hard ceiling before IG token expiry
    TOKEN_HEARTBEAT_REFRESH_SECONDS = 45 * 60  # silent refresh after 45 minutes

    def __init__(
        self,
        credentials: Credentials,
        *,
        account_id: str | None = None,
        timeout_seconds: float = 45.0,
        max_retries: int = 3,
        retry_delay_seconds: float = 2.5,
    ) -> None:
        self.credentials = credentials
        self.account_type = credentials.account_type
        self.account_id = account_id or credentials.ig_account_id
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self._auth = AuthManager(
            credentials.ig_api_key,
            credentials.ig_username,
            credentials.ig_password,
        )
        self._session = requests.Session()
        if self.account_type == "DEMO":
            self._base = normalize_ig_gateway_url(IG_DEMO_GATEWAY, demo=True)
        else:
            self._base = normalize_ig_gateway_url(IG_LIVE_GATEWAY, demo=False)
        self._bid = 0.0
        self._offer = 0.0
        self._last_login_status: int | None = None
        self._last_auth_error: str = ""
        self._market_constraints_cache: dict[str, dict[str, Any]] = {}
        self._live_price_cache: dict[str, dict[str, Any]] = {}
        self._account_balance: float | None = None
        self._account_profit_loss: float | None = None
        self._account_available: float | None = None
        self._last_accounts_raw_payload: dict[str, Any] | None = None
        self._last_rest_ok_at: float = 0.0
        self._last_rest_ok_path: str = ""
        self._last_account_refresh_ts: float = 0.0
        self._last_stream_activity_at: float = 0.0
        self._token_created_at: float = 0.0
        self._session_refresh_in_progress: bool = False

    @property
    def session(self) -> SessionTokens | None:
        return self._auth.tokens

    def record_rest_success(self, path: str) -> None:
        """Mark last successful IG REST response (for UI REST OK indicator)."""
        self._last_rest_ok_at = time.time()
        self._last_rest_ok_path = str(path or "")[:48]

    def touch_stream_activity(self) -> None:
        """Mark live quote activity (stream/hub) — not an IG REST call."""
        self._last_stream_activity_at = time.time()

    def stream_activity_age_seconds(self) -> float | None:
        if self._last_stream_activity_at <= 0:
            return None
        return time.time() - self._last_stream_activity_at

    def rest_ok_age_seconds(self) -> float | None:
        if self._last_rest_ok_at <= 0:
            return None
        return time.time() - self._last_rest_ok_at

    def rest_ok_label(self, *, stale_after: float = 30.0) -> str:
        age = self.rest_ok_age_seconds()
        if age is None:
            return ""
        if age <= stale_after:
            return f"REST OK {age:.1f}s"
        return f"REST OK {age:.0f}s ago"

    def _touch_token_created(self) -> None:
        self._token_created_at = time.time()

    def token_age_seconds(self) -> float:
        if self._token_created_at <= 0:
            return 0.0
        return max(0.0, time.time() - self._token_created_at)

    @staticmethod
    def _session_path_protected(path: str) -> bool:
        p = (path or "").rstrip("/")
        return p.endswith("/session") or p.endswith("/session/refresh")

    def _log_auth_failure_critical(self) -> None:
        log_engine("CRITICAL: IG authentication failed — check credentials")

    @staticmethod
    def _auth_failure_response(path: str) -> requests.Response:
        resp = requests.Response()
        resp.status_code = 401
        resp.url = path
        resp._content = b'{"errorCode":"error.security.auth-failure"}'
        return resp

    def _refresh_session_tokens(self) -> bool:
        """POST /session/refresh or full login — bypasses request() to avoid recursion."""
        if self._session_refresh_in_progress:
            return False
        self._session_refresh_in_progress = True
        try:
            if not self._auth.tokens or not self._auth.tokens.is_valid:
                self.login()
                return bool(self._auth.tokens and self._auth.tokens.is_valid)
            url = f"{self._base}/session/refresh"
            r = self._session.request(
                "POST",
                url,
                headers=self._auth_headers("1"),
                timeout=self.timeout_seconds,
            )
            if r.status_code in (200, 201):
                self._auth.apply_login_response(
                    dict(r.headers),
                    r.json(),
                    preferred_account_id=self.account_id,
                )
                self._touch_token_created()
                self.record_rest_success("/session/refresh")
                return True
            self.login()
            return bool(self._auth.tokens and self._auth.tokens.is_valid)
        except IGAuthError:
            self._log_auth_failure_critical()
            return False
        except Exception:
            self._log_auth_failure_critical()
            return False
        finally:
            self._session_refresh_in_progress = False

    def proactive_refresh_if_needed(self) -> bool:
        """Refresh when token age exceeds 45-minute heartbeat (before REST calls)."""
        if not self._auth.tokens or not self._auth.tokens.is_valid:
            return False
        age = self.token_age_seconds()
        if age <= self.TOKEN_HEARTBEAT_REFRESH_SECONDS:
            return False
        minutes = age / 60.0
        if self._refresh_session_tokens():
            log_engine(f"IG session refreshed — silent handshake after {minutes:.0f}m")
            return True
        return False

    def _safe_relogin(self) -> bool:
        try:
            self.login()
            return bool(self._auth.tokens and self._auth.tokens.is_valid)
        except (IGAuthError, Exception):
            return False

    def _evict_session_token_cache(self) -> None:
        """Delete in-memory tokens and HTTP session cookie residue."""
        self._auth.clear()
        self._token_created_at = 0.0
        self._session_refresh_in_progress = False
        try:
            self._session.cookies.clear()
        except Exception:
            pass
        for path in self._token_cache_file_paths():
            try:
                if path.is_file():
                    path.unlink()
                    log_engine(f"IG REST: evicted stale token cache {path}")
            except OSError as exc:
                log_engine(
                    f"IG REST: token cache eviction skipped {path}: "
                    f"{type(exc).__name__}: {exc}"
                )
        try:
            from system.ig_rest_session import clear_shared_rest_client

            clear_shared_rest_client()
        except Exception:
            pass

    @staticmethod
    def _token_cache_file_paths() -> list[Any]:
        paths: list[Any] = []
        try:
            from pathlib import Path

            from system.paths import data_dir, logs_dir

            for base in (data_dir(), logs_dir()):
                paths.append(Path(base) / "ig_session_tokens.json")
                paths.append(Path(base) / "ig_rest_session.json")
        except Exception:
            pass
        return paths

    def _token_eviction_reauth(self) -> bool:
        """
        Token Eviction Loop — purge cached session, sleep 2s, clean handshake.
        Keeps REST budget silent during re-auth (no refresh recursion).
        """
        self._evict_session_token_cache()
        time.sleep(2.0)
        try:
            url = f"{self._base}/session"
            body = self._auth.login_body(self.account_id)
            headers = self._auth.login_headers()
            r = self._session.request(
                "POST",
                url,
                json=body,
                headers=headers,
                timeout=self.timeout_seconds,
            )
            if r.status_code not in (200, 201):
                self._log_auth_failure_critical()
                return False
            resp_body = r.json() if r.text else {}
            self._auth.apply_login_response(
                dict(r.headers),
                resp_body,
                preferred_account_id=self.account_id,
            )
            self._touch_token_created()
            self.record_rest_success("/session")
            log_engine("IG REST: token eviction re-auth handshake complete")
            return bool(self._auth.tokens and self._auth.tokens.is_valid)
        except Exception as exc:
            log_engine(
                f"IG REST: token eviction re-auth failed: {type(exc).__name__}: {exc}"
            )
            return False

    def _log_auth_state(self, label: str) -> None:
        tok = self._auth.tokens
        update_demo_diagnostics(
            endpoint=self._base,
            account_id=self.account_id,
            rest_login_endpoint=f"{self._base}/session",
            rest_login_status_code=self._last_login_status,
            cst_token=mask_token(tok.cst if tok else None),
            security_token=mask_token(tok.security_token if tok else None),
        )
        log_demo_rest(
            label,
            base_url=self._base,
            account_type=self.account_type,
            account_id=self.account_id,
            login_status=self._last_login_status,
            cst=mask_token(tok.cst if tok else None),
            xst=mask_token(tok.security_token if tok else None),
        )

    def login(self) -> SessionTokens:
        get_rate_limit_manager().check_rest_allowed()
        url = f"{self._base}/session"
        body = self._auth.login_body(self.account_id)
        headers = self._auth.login_headers()

        log_demo_rest(
            "POST /session — login attempt",
            url=url,
            account_type=self.account_type,
            identifier_mask=mask_token(self.credentials.ig_username, 2),
            payload_keys=list(body.keys()),
            headers_present=list(headers.keys()),
        )
        update_demo_diagnostics(
            rest_login_endpoint=url,
            rest_login_payload_masked=f"identifier={mask_token(self.credentials.ig_username, 2)} accountId={self.account_id}",
        )

        try:
            r = self._session.request(
                "POST",
                url,
                headers=headers,
                json=body,
                timeout=self.timeout_seconds,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            self._last_auth_error = str(e)
            log_demo_rest("POST /session — network error", error=str(e))
            raise IGAuthError(f"IG login network error: {e}") from e

        self._last_login_status = r.status_code
        update_demo_diagnostics(rest_login_status_code=r.status_code)

        log_demo_rest(
            "POST /session — response",
            status_code=r.status_code,
            response_preview=(r.text or "")[:400],
        )

        if r.status_code not in (200, 201):
            self._last_auth_error = r.text or f"HTTP {r.status_code}"
            code = parse_rate_limit_error(r.status_code, r.text)
            if code:
                get_rate_limit_manager().handle_http_response(
                    r, source="login", path="/session"
                )
            update_demo_diagnostics(rest_status=f"login failed HTTP {r.status_code}")
            raise IGAuthError(
                f"IG login failed: HTTP {r.status_code} — {(r.text or '')[:300]}",
                status_code=r.status_code,
            )

        try:
            resp_body = r.json()
        except Exception:
            resp_body = {}

        tokens = self._auth.apply_login_response(
            dict(r.headers),
            resp_body,
            preferred_account_id=self.account_id,
        )
        if self.account_id:
            tokens.account_id = self.account_id

        from system.account_currency import set_account_currency_from_session

        set_account_currency_from_session(resp_body)
        self._switch_to_configured_account(tokens)
        info = resp_body.get("accountInfo") or {}
        try:
            self._account_balance = (
                float(info.get("balance")) if info.get("balance") is not None else None
            )
            self._account_profit_loss = (
                float(info.get("profitLoss"))
                if info.get("profitLoss") is not None
                else None
            )
            self._account_available = (
                float(info.get("available"))
                if info.get("available") is not None
                else None
            )
        except (TypeError, ValueError):
            pass
        self.record_rest_success("/session")
        self._log_auth_state("DEMO credentials validated — login success")
        update_demo_diagnostics(rest_status="authenticated")

        trace_execution(
            "REST",
            "IGRestClient.login",
            decision="authentication success",
            params={
                "account_type": self.account_type,
                "account_id": self.account_id,
                "base_url": self._base,
            },
        )
        self._touch_token_created()
        return tokens

    def probe_login_once(self) -> dict[str, Any]:
        """
        Single POST /session for safe API readiness checks.

        No retries, no account switch, no rate-limit manager activation.
        """
        url = f"{self._base}/session"
        body = self._auth.login_body(self.account_id)
        headers = self._auth.login_headers()

        log_demo_rest(
            "PROBE POST /session — safe readiness check (single attempt)",
            url=url,
            account_type=self.account_type,
        )
        update_demo_diagnostics(
            rest_login_endpoint=url,
            rest_login_payload_masked=(
                f"identifier={mask_token(self.credentials.ig_username, 2)} "
                f"accountId={self.account_id}"
            ),
        )

        try:
            r = self._session.request(
                "POST",
                url,
                headers=headers,
                json=body,
                timeout=self.timeout_seconds,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            self._last_login_status = None
            log_demo_rest("PROBE POST /session — network error", error=str(e))
            return {
                "ok": False,
                "url": url,
                "status_code": None,
                "error_code": "",
                "body": str(e),
                "cst": "",
                "security_token": "",
            }

        self._last_login_status = r.status_code
        text = r.text or ""
        error_code = ""
        try:
            data = json.loads(text)
            error_code = str(data.get("errorCode", ""))
        except (json.JSONDecodeError, TypeError):
            pass

        log_demo_rest(
            "PROBE POST /session — response",
            url=url,
            status_code=r.status_code,
            error_code=error_code or None,
            response_preview=text[:400],
        )
        update_demo_diagnostics(rest_login_status_code=r.status_code)

        if r.status_code not in (200, 201):
            return {
                "ok": False,
                "url": url,
                "status_code": r.status_code,
                "error_code": error_code,
                "body": text[:500],
                "cst": "",
                "security_token": "",
            }

        try:
            resp_body = r.json()
        except Exception:
            resp_body = {}

        tokens = self._auth.apply_login_response(
            dict(r.headers),
            resp_body,
            preferred_account_id=self.account_id,
        )
        cst = mask_token(tokens.cst)
        xst = mask_token(tokens.security_token)
        log_demo_rest(
            "PROBE POST /session — success",
            url=url,
            status_code=r.status_code,
            cst=cst,
            xst=xst,
        )
        return {
            "ok": True,
            "url": url,
            "status_code": r.status_code,
            "error_code": "",
            "body": text[:200],
            "cst": cst,
            "security_token": xst,
        }

    def _switch_to_configured_account(self, tokens: SessionTokens) -> None:
        if not self.account_id:
            return
        if tokens.account_id == self.account_id:
            log_demo_rest(
                "Account switch skipped — already on target account",
                account_id=self.account_id,
            )
            return
        try:
            r = self._session.request(
                "PUT",
                f"{self._base}/session",
                headers=self._auth_headers("1"),
                json={"accountId": self.account_id, "defaultAccount": False},
                timeout=self.timeout_seconds,
            )
            log_demo_rest(
                "PUT /session — account switch",
                status_code=r.status_code,
                account_id=self.account_id,
                body_preview=(r.text or "")[:200],
            )
            if r.status_code in (200, 201):
                tokens.account_id = self.account_id
            elif r.status_code == 403:
                log_demo_rest(
                    "PUT /session — account switch rate-limited or denied; continuing with session account",
                    session_account=tokens.account_id,
                )
        except Exception as e:
            log_demo_rest("PUT /session — account switch error", error=str(e))

    def refresh_session(self) -> SessionTokens:
        if not self._auth.tokens:
            return self.login()
        if self._refresh_session_tokens():
            tok = self._auth.tokens
            if tok:
                return tok
        raise IGAuthError("IG session refresh failed")

    def ensure_session(self) -> None:
        get_rate_limit_manager().check_rest_allowed()
        if not self._auth.tokens or not self._auth.tokens.is_valid:
            self.login()

    def end_session(self) -> None:
        """DELETE /session — release IG tokens on graceful shutdown."""
        if not self._auth.tokens:
            return
        try:
            r = self.request("DELETE", "/session")
            log_demo_rest(
                "DELETE /session — logout",
                status_code=r.status_code,
                body_preview=(r.text or "")[:200],
            )
        except Exception as e:
            log_demo_rest("DELETE /session — logout error", error=str(e))
        finally:
            self._auth.clear()

    @staticmethod
    def _dealing_rule_value(rules: dict[str, Any], key: str) -> float:
        entry = rules.get(key, {})
        if isinstance(entry, dict):
            try:
                return float(entry.get("value", 0))
            except (TypeError, ValueError):
                return 0.0
        try:
            return float(entry)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _instrument_currency(instrument: dict[str, Any]) -> str:
        currencies = instrument.get("currencies") or []
        for c in currencies:
            if isinstance(c, dict) and c.get("isDefault"):
                return str(c.get("code", "")).upper()
        if currencies and isinstance(currencies[0], dict):
            return str(currencies[0].get("code", "")).upper()
        return str(instrument.get("currency", "USD")).upper()

    def fetch_market_constraints(
        self,
        epic: str,
        *,
        max_age_seconds: float = 300.0,
        budget_priority: bool = False,
    ) -> dict[str, Any]:
        """IG dealing rules for an epic (cached to limit API calls)."""
        now = time.time()
        cached = self._market_constraints_cache.get(epic)
        if cached and now - float(cached.get("ts", 0)) < max_age_seconds:
            return dict(cached["data"])

        try:
            get_rate_limit_manager().check_rest_allowed()
        except Exception:
            if cached:
                return dict(cached["data"])
            return {}

        self.ensure_session()
        r = self.request(
            "GET",
            f"/markets/{epic}",
            headers=self._auth_headers("3"),
            budget_priority=budget_priority,
        )
        if r.status_code == 401:
            self.login()
            r = self.request(
                "GET",
                f"/markets/{epic}",
                headers=self._auth_headers("3"),
                budget_priority=budget_priority,
            )
        if r.status_code == 403:
            code = parse_rate_limit_error(r.status_code, r.text)
            if code:
                get_rate_limit_manager().handle_http_response(
                    r, source="markets", path=epic
                )
            self._raise_auth_or_api(r, "Market constraints")
        if r.status_code != 200:
            raise IGAPIError(
                f"Market constraints failed: HTTP {r.status_code}",
                status_code=r.status_code,
            )

        body = r.json()
        snap = body.get("snapshot", {})
        rules = body.get("dealingRules", {})
        instrument = body.get("instrument", {})
        data = {
            "epic": epic,
            "market_status": str(snap.get("marketStatus", "")),
            "min_deal_size": self._dealing_rule_value(rules, "minDealSize"),
            "min_stop_distance": self._dealing_rule_value(
                rules, "minNormalStopOrLimitDistance"
            ),
            "min_controlled_stop_distance": self._dealing_rule_value(
                rules, "minControlledRiskStopDistance"
            ),
            "min_step_distance": self._dealing_rule_value(rules, "minStepDistance"),
            "currency_code": self._instrument_currency(instrument),
            "bid": float(snap.get("bid", 0)),
            "offer": float(snap.get("offer", 0)),
        }
        self._market_constraints_cache[epic] = {"ts": now, "data": data}
        log_demo_rest("Market constraints", **data)
        return data

    def normalize_order_params(
        self,
        epic: str,
        *,
        size: float,
        stop_distance: float,
        limit_distance: float | None,
        currency_code: str,
    ) -> tuple[float, float, float | None, str]:
        """Clamp size/stops/currency to IG dealing rules for the epic."""
        c = self.fetch_market_constraints(epic)
        status = c["market_status"]
        if status not in ("TRADEABLE", "EDITS_ONLY"):
            raise IGOrderError(
                f"Market {epic} not tradeable (status={status})",
                status_code=400,
            )

        min_deal = max(float(c["min_deal_size"]), 0.01)
        min_stop = max(float(c["min_stop_distance"]), 1.0)
        norm_size = max(float(size), min_deal)
        norm_stop = max(float(stop_distance), min_stop)
        norm_limit: float | None
        if limit_distance is not None and float(limit_distance) > 0:
            norm_limit = max(float(limit_distance), norm_stop)
        else:
            norm_limit = None

        instr_ccy = str(c["currency_code"] or "USD").upper()
        norm_ccy = instr_ccy
        if currency_code.upper() != instr_ccy:
            log_demo_rest(
                "Order currency adjusted to match instrument",
                epic=epic,
                requested=currency_code,
                using=instr_ccy,
            )

        if (
            norm_size != size
            or norm_stop != stop_distance
            or norm_ccy != currency_code.upper()
        ):
            log_demo_rest(
                "Order params adjusted for IG dealing rules",
                epic=epic,
                size_before=size,
                size_after=norm_size,
                stop_before=stop_distance,
                stop_after=norm_stop,
                min_deal=min_deal,
                min_stop=min_stop,
            )
        return norm_size, norm_stop, norm_limit, norm_ccy

    def fetch_live_prices(
        self,
        epic: str,
        *,
        max_age_seconds: float = 5.0,
        constraints_fallback_seconds: float = 60.0,
        budget_priority: bool = False,
    ) -> tuple[float, float]:
        """
        Fresh bid/offer for streaming/UI — short live cache (default 5s).

        When constraints were fetched recently, reuse snapshot bid/offer instead
        of a second GET /markets/{epic} (same payload, separate cache keys).
        """
        now = time.time()
        cached = self._live_price_cache.get(epic)
        if cached and now - float(cached.get("ts", 0)) < max_age_seconds:
            return float(cached["bid"]), float(cached["offer"])

        constraints_entry = self._market_constraints_cache.get(epic)
        if constraints_entry:
            age = now - float(constraints_entry.get("ts", 0))
            if age < max(float(constraints_fallback_seconds), max_age_seconds):
                data = constraints_entry.get("data") or {}
                bid = float(data.get("bid", 0))
                offer = float(data.get("offer", 0))
                if bid > 0 and offer > 0:
                    self._live_price_cache[epic] = {
                        "ts": now,
                        "bid": bid,
                        "offer": offer,
                    }
                    self._bid, self._offer = bid, offer
                    return bid, offer

        self.ensure_session()
        r = self.request(
            "GET",
            f"/markets/{epic}",
            headers=self._auth_headers("3"),
            budget_priority=budget_priority,
        )
        if r.status_code == 401:
            self.login()
            r = self.request(
                "GET",
                f"/markets/{epic}",
                headers=self._auth_headers("3"),
                budget_priority=budget_priority,
            )
        if r.status_code != 200:
            if cached:
                return float(cached["bid"]), float(cached["offer"])
            raise IGAPIError(
                f"Live price fetch failed: HTTP {r.status_code}",
                status_code=r.status_code,
            )

        snap = r.json().get("snapshot", {})
        bid = float(snap.get("bid", 0))
        offer = float(snap.get("offer", 0))
        self._live_price_cache[epic] = {"ts": now, "bid": bid, "offer": offer}
        self._bid, self._offer = bid, offer
        if epic in self._market_constraints_cache:
            data = dict(self._market_constraints_cache[epic]["data"])
            data["bid"] = bid
            data["offer"] = offer
            self._market_constraints_cache[epic]["data"] = data
        return bid, offer

    def fetch_market_snapshot(
        self, epic: str, *, live: bool = False, budget_priority: bool = False
    ) -> dict[str, Any]:
        c = self.fetch_market_constraints(epic, budget_priority=budget_priority)
        if live:
            bid, offer = self.fetch_live_prices(epic, budget_priority=budget_priority)
        else:
            bid = float(c["bid"])
            offer = float(c["offer"])
        self._bid, self._offer = bid, offer
        return {
            "epic": epic,
            "bid": bid,
            "offer": offer,
            "snapshot": {
                "bid": bid,
                "offer": offer,
                "marketStatus": c["market_status"],
            },
            "constraints": c,
        }

    def set_quote(self, bid: float, offer: float) -> None:
        self._bid, self._offer = bid, offer

    def get_cached_account_summary(self) -> dict[str, float | None]:
        """Last known balance/equity from login (no extra API call)."""
        return {
            "balance": self._account_balance,
            "profit_loss": self._account_profit_loss,
            "available": self._account_available,
        }

    def get_last_accounts_raw_payload(self) -> dict[str, Any] | None:
        """Last GET /accounts JSON body — for drawdown debug logging."""
        return dict(self._last_accounts_raw_payload) if self._last_accounts_raw_payload else None

    def maybe_refresh_account_summary(
        self, *, min_interval: float = 60.0
    ) -> dict[str, float | None]:
        """Throttled GET /accounts — avoids UI/stream hammering IG rate limits."""
        from system.broker_status_cache import (
            record_broker_sync_failure,
            should_serve_from_cache,
        )
        from system.market_watch.calendar import background_rest_paused
        from system.rest_api_budget import RestBudgetPausedError, order_in_flight_paused

        endpoint = "account_summary"
        if should_serve_from_cache(endpoint):
            return self.get_cached_account_summary()
        if order_in_flight_paused(endpoint):
            return self.get_cached_account_summary()
        if background_rest_paused(endpoint):
            return self.get_cached_account_summary()
        interval = max(60.0, float(min_interval))
        now = time.time()
        if now - self._last_account_refresh_ts < interval:
            return self.get_cached_account_summary()
        self._last_account_refresh_ts = now
        try:
            return self.refresh_account_summary()
        except RestBudgetPausedError:
            return self.get_cached_account_summary()
        except Exception as exc:
            record_broker_sync_failure(endpoint, exc)
            return self.get_cached_account_summary()

    def get_accounts(self) -> dict[str, Any]:
        """Pre-flight — GET /accounts on IG DEMO or LIVE gateway."""
        from ig_api.exceptions import IGAPIError

        self.ensure_session()
        r = self.request("GET", "/accounts", headers=self._auth_headers("1"))
        if r.status_code != 200:
            raise IGAPIError(
                f"GET /accounts failed: HTTP {r.status_code}",
                status_code=r.status_code,
            )
        body = r.json()
        if not isinstance(body, dict):
            raise IGAPIError("GET /accounts returned non-object payload")
        return body

    def refresh_account_summary(self) -> dict[str, float | None]:
        """Refresh balance / P&L from GET /accounts (used by stream heartbeat)."""
        from system.broker_status_cache import (
            record_broker_sync_failure,
            write_broker_status_cache,
        )

        endpoint = "account_summary"
        try:
            get_rate_limit_manager().check_rest_allowed()
        except Exception as exc:
            record_broker_sync_failure(endpoint, exc)
            return self.get_cached_account_summary()
        self.ensure_session()
        r = self.request("GET", "/accounts", headers=self._auth_headers("1"))
        if r.status_code != 200:
            record_broker_sync_failure(endpoint, http_status=r.status_code)
            return self.get_cached_account_summary()
        body = r.json()
        if isinstance(body, dict):
            status_token = str(body.get("marketStatus") or body.get("status") or "").upper()
            if status_token == "CLOSED":
                write_broker_status_cache(endpoint, "CLOSED", detail="accounts CLOSED")
        self._last_accounts_raw_payload = body if isinstance(body, dict) else {"raw": body}
        from system.balance_pnl_decimal import decimal_to_float, money_decimal

        for acc in body.get("accounts", []):
            if str(acc.get("accountId")) != self.account_id:
                continue
            bal = acc.get("balance") or {}
            balance_d = money_decimal(bal.get("balance"), field="accounts.balance")
            upl_d = money_decimal(bal.get("profitLoss"), field="accounts.profitLoss")
            avail_d = money_decimal(bal.get("available"), field="accounts.available")
            if balance_d is not None:
                self._account_balance = decimal_to_float(balance_d)
            if upl_d is not None:
                self._account_profit_loss = decimal_to_float(upl_d)
            if avail_d is not None:
                self._account_available = decimal_to_float(avail_d)
            break
        self._last_account_refresh_ts = time.time()
        return self.get_cached_account_summary()

    def fetch_transactions(
        self,
        from_date: str,
        to_date: str,
        *,
        transaction_type: str = "ALL_DEAL",
        page_size: int = 500,
    ) -> list[dict[str, Any]]:
        """
        IG transaction history — source of truth for closed-trade P&L.

        Path: /history/transactions/{transactionType}/{fromDate}/{toDate}
        Dates must be dd-mm-yyyy (see IG Labs API reference).
        """
        from urllib.parse import quote

        from system.ig_transactions import coerce_to_ig_path_date

        self.ensure_session()
        txn_type = str(transaction_type or "ALL_DEAL").upper()
        start_raw = coerce_to_ig_path_date(from_date)
        end_raw = coerce_to_ig_path_date(to_date)
        start = quote(start_raw, safe="")
        end = quote(end_raw, safe="")
        path = f"/history/transactions/{txn_type}/{start}/{end}"

        def _fetch(version: str) -> requests.Response:
            return self.request(
                "GET",
                path,
                headers=self._auth_headers(version),
                params={"pageSize": max(1, min(int(page_size), 500))},
            )

        # Two attempts only: API version 2 (preferred), then version 1 fallback.
        # The previous nested loop (2 versions × 3 page_sizes + short-window retry)
        # could fire 8 REST calls per invocation, rapidly exhausting the hard cap.
        last_preview = ""
        for version in ("2", "1"):
            r = _fetch(version)
            if r.status_code == 401:
                self.login()
                r = _fetch(version)
            if r.status_code == 200:
                txns = list(r.json().get("transactions") or [])
                log_demo_rest(
                    "GET history/transactions OK",
                    version=version,
                    count=len(txns),
                    path=path,
                )
                return txns
            last_preview = (r.text or "")[:200]

        log_demo_rest(
            "GET history/transactions failed",
            status_code=getattr(r, "status_code", 0),
            path=path,
            preview=last_preview,
        )
        return []

    def fetch_transaction_history(
        self,
        hours: float = 24.0,
        *,
        transaction_type: str = "ALL_DEAL",
        page_size: int = 500,
    ) -> list[dict[str, Any]]:
        """
        Closed-deal ledger for the account — convenience wrapper over
        ``GET /history/transactions/{transactionType}/{fromDate}/{toDate}``.

        ``hours`` selects the lookback window; path dates are dd-mm-yyyy with
        enough calendar span to cover the requested period (IG path is day-granular).
        """
        from datetime import datetime, timedelta

        lookback_h = max(1.0, float(hours))
        end = datetime.now()
        days_back = max(1, int((lookback_h + 23.0) // 24.0))
        start = end - timedelta(days=days_back)
        return self.fetch_transactions(
            start.strftime("%d-%m-%Y"),
            end.strftime("%d-%m-%Y"),
            transaction_type=transaction_type,
            page_size=page_size,
        )

    def fetch_account_activity(
        self,
        from_date: str,
        to_date: str,
    ) -> list[dict[str, Any]]:
        """IG account activity — local date/time per deal (matches IG Trading UI)."""
        from urllib.parse import quote

        from system.ig_transactions import coerce_to_ig_path_date

        self.ensure_session()
        start = quote(coerce_to_ig_path_date(from_date), safe="")
        end = quote(coerce_to_ig_path_date(to_date), safe="")
        path = f"/history/activity/{start}/{end}"

        r = self.request("GET", path, headers=self._auth_headers("1"))
        if r.status_code == 401:
            self.login()
            r = self.request("GET", path, headers=self._auth_headers("1"))
        if r.status_code != 200:
            log_demo_rest(
                "GET history/activity failed",
                status_code=r.status_code,
                preview=(r.text or "")[:200],
            )
            return []
        activities = list(r.json().get("activities") or [])
        log_demo_rest(
            "GET history/activity OK",
            count=len(activities),
            path=path,
        )
        return activities

    def fetch_account_balance(self) -> float:
        self.ensure_session()
        r = self.request("GET", "/accounts", headers=self._auth_headers("1"))
        if r.status_code != 200:
            raise IGAPIError(
                f"Accounts request failed: HTTP {r.status_code}",
                status_code=r.status_code,
            )
        body = r.json()
        self._last_accounts_raw_payload = body if isinstance(body, dict) else {"accounts": body}
        accounts = body.get("accounts", []) if isinstance(body, dict) else []
        from system.balance_pnl_decimal import decimal_to_float, money_decimal

        for acc in accounts:
            if str(acc.get("accountId")) == self.account_id:
                bal = acc.get("balance", {}) or {}
                balance_d = money_decimal(bal.get("balance"), field="fetch.balance")
                if balance_d is not None:
                    return decimal_to_float(balance_d)
        if accounts:
            bal = accounts[0].get("balance", {}) or {}
            balance_d = money_decimal(bal.get("balance"), field="fetch.balance.fallback")
            if balance_d is not None:
                return decimal_to_float(balance_d)
        return 0.0

    def fetch_client_sentiment(self, epic: str) -> float:
        """IG client sentiment long % (0-100); 50.0 on error."""
        self.ensure_session()
        try:
            r = self.request(
                "GET",
                f"/clientsentiment/{epic}",
                headers=self._auth_headers("1"),
            )
            if r.status_code != 200:
                return 50.0
            body = r.json()
            if isinstance(body, dict) and "clientSentiment" in body:
                body = body["clientSentiment"] or body
            return float(body.get("longPositionPercentage", 50.0))
        except Exception:
            return 50.0

    def fetch_price_history(
        self,
        epic: str,
        *,
        resolution: str = "MINUTE_5",
        num_points: int = 288,
    ) -> list[dict]:
        """Fetch historical OHLCV bars from IG REST API.

        Returns a list of dicts with keys: time (ISO str), open, high, low, close,
        bid_close, offer_close.  Returns an empty list on error so callers can
        fall back to synthetic warmup.

        IG endpoint: GET /prices/{epic}/{resolution}/{numPoints}
        API version header: 1
        """
        self.ensure_session()
        try:
            r = self.request(
                "GET",
                f"/prices/{epic}/{resolution}/{num_points}",
                headers=self._auth_headers("1"),
            )
        except IGAPIError as exc:
            msg = str(exc).lower()
            if (
                "hard_rate_cap" in msg
                or "rest deferred" in msg
                or "rate_limit" in msg
            ):
                try:
                    from signals.signal_engine import mark_rest_rate_limit_local_fallback

                    mark_rest_rate_limit_local_fallback(epic=epic)
                except Exception:
                    pass
            return []
        if r.status_code != 200:
            try:
                from trading.ohlc_bootstrap import (
                    is_ig_historical_allowance_error,
                    mark_historical_allowance_lockout,
                )

                if is_ig_historical_allowance_error(r.status_code, r.text):
                    mark_historical_allowance_lockout(source="fetch_price_history")
            except Exception:
                pass
            return []
        try:
            body = r.json()
            prices = body.get("prices") or body.get("allowance") or []
            if not isinstance(prices, list):
                return []
            out = []
            for p in prices:
                snap = p.get("snapshotTime") or p.get("snapshotTimeUTC") or ""
                op = p.get("openPrice", {}) or {}
                hi = p.get("highPrice", {}) or {}
                lo = p.get("lowPrice", {}) or {}
                cl = p.get("closePrice", {}) or {}
                out.append(
                    {
                        "time": snap,
                        "open": float(op.get("mid") or op.get("bid") or 0),
                        "high": float(
                            hi.get("mid") or hi.get("ask") or hi.get("offer") or 0
                        ),
                        "low": float(lo.get("mid") or lo.get("bid") or 0),
                        "close": float(cl.get("mid") or cl.get("bid") or 0),
                        "bid_close": float(cl.get("bid") or 0),
                        "offer_close": float(cl.get("ask") or cl.get("offer") or 0),
                    }
                )
            return [b for b in out if b["close"] > 0]
        except Exception:
            return []

    def validate_demo_order_routing(
        self,
        *,
        epic: str,
        dry_run: bool = True,
        size: float = 1.0,
        market_bid: float | None = None,
        market_offer: float | None = None,
        skip_balance_check: bool = False,
    ) -> dict[str, Any]:
        from ig_api.mock_clients import MockIGRest

        if isinstance(self, MockIGRest):
            return {"ok": False, "error": "Mock REST client detected", "is_mock": True}

        if self.account_type != "DEMO":
            return {
                "ok": False,
                "error": f"Account type is {self.account_type}, expected DEMO",
                "is_mock": False,
            }

        if "demo-api.ig.com" not in self._base:
            return {
                "ok": False,
                "error": f"Not a DEMO REST base URL: {self._base}",
                "is_mock": False,
            }

        self.ensure_session()
        if (
            market_bid is not None
            and market_offer is not None
            and market_bid > 0
            and market_offer > 0
        ):
            bid, offer = float(market_bid), float(market_offer)
        else:
            snap = self.fetch_market_snapshot(epic)
            bid, offer = float(snap["bid"]), float(snap["offer"])
        if bid <= 0 or offer <= 0:
            return {
                "ok": False,
                "error": "Invalid market snapshot prices",
                "is_mock": False,
            }

        balance = 0.0
        account_found = bool(self.account_id)
        if skip_balance_check:
            if not account_found:
                return {
                    "ok": False,
                    "error": "No account_id configured for DEMO routing",
                    "is_mock": False,
                }
        else:
            balance = self.fetch_account_balance()
            account_found = bool(self.account_id)
            if not account_found:
                return {
                    "ok": False,
                    "error": "No account_id configured for DEMO routing",
                    "is_mock": False,
                }

        from system.config_loader import get_config

        cfg = get_config()
        return {
            "ok": True,
            "is_mock": False,
            "dry_run": dry_run,
            "base_url": self._base,
            "account_id": self.account_id,
            "epic": epic,
            "bid": bid,
            "offer": offer,
            "balance": balance,
            "message": "DEMO routing validated; no order submitted",
        }

    def validate_order_schema(
        self,
        payload: dict[str, Any],
        *,
        full_session_ping: bool = False,
    ) -> dict[str, Any]:
        """
        Non-destructive order schema + session validation — no POST /positions/otc.

        Used by the Target Shadow Tracer dry-run loop on Thread B.
        """
        from ig_api.mock_clients import MockIGRest

        if isinstance(self, MockIGRest):
            return {
                "ok": False,
                "category": "AUTH_EXPIRY",
                "error": "Mock REST client — live route blocked",
                "http_status": 0,
                "payload": payload,
            }

        required = ("epic", "direction", "size", "orderType", "stopDistance")
        for key in required:
            if key not in payload or payload[key] in (None, ""):
                return {
                    "ok": False,
                    "category": "SCHEMA_INVALID",
                    "error": f"missing required field: {key}",
                    "http_status": 0,
                    "payload": payload,
                }

        try:
            age = float(payload.get("quote_age_sec") or 0)
            max_age = float(os.environ.get("IG_QUOTE_MAX_AGE_SEC", "45") or 45)
            if age > max_age:
                return {
                    "ok": False,
                    "category": "REGIME_MISMATCH",
                    "error": f"quote_age_sec={age:.1f} exceeds {max_age:.0f}s safety lock",
                    "http_status": 0,
                    "payload": payload,
                }
        except (TypeError, ValueError):
            pass

        needs_network = full_session_ping or not (
            getattr(self.session, "is_valid", False) if self.session else False
        )
        if needs_network and ig_demo_gateway_reachable(self._base):
            if not _validate_throttle_try_acquire():
                return _validate_throttle_cached_ok(payload)

        try:
            from ig_api.exceptions import RateLimitError
            from system.rate_limit_manager import get_rate_limit_manager

            get_rate_limit_manager().check_rest_allowed()
        except Exception as exc:
            return {
                "ok": False,
                "category": "RATE_WALL",
                "error": str(exc),
                "http_status": 429,
                "payload": payload,
            }

        try:
            self.ensure_session()
        except Exception as exc:
            return {
                "ok": False,
                "category": "AUTH_EXPIRY",
                "error": str(exc),
                "http_status": 401,
                "payload": payload,
            }

        if full_session_ping:
            try:
                r = self.request("GET", "/accounts", headers=self._auth_headers("1"))
                status = int(getattr(r, "status_code", 0) or 0)
                if status in (401, 403):
                    return {
                        "ok": False,
                        "category": "AUTH_EXPIRY",
                        "error": f"session ping HTTP {status}",
                        "http_status": status,
                        "payload": payload,
                    }
                if status >= 400:
                    return {
                        "ok": False,
                        "category": "SCHEMA_INVALID",
                        "error": f"session ping HTTP {status}",
                        "http_status": status,
                        "payload": payload,
                    }
            except Exception as exc:
                text = str(exc).lower()
                if "401" in text or "403" in text or "auth" in text:
                    cat = "AUTH_EXPIRY"
                elif "429" in text or "rate" in text:
                    cat = "RATE_WALL"
                else:
                    cat = "SCHEMA_INVALID"
                return {
                    "ok": False,
                    "category": cat,
                    "error": str(exc),
                    "http_status": 0,
                    "payload": payload,
                }

            try:
                size = float(payload.get("size") or 0)
                if size > 0:
                    balance = float(self.fetch_account_balance() or 0)
                    min_notional = size * 50.0
                    if balance > 0 and balance < min_notional:
                        return {
                            "ok": False,
                            "category": "MARGIN_LOCK",
                            "error": f"balance {balance:.2f} < min_notional {min_notional:.2f}",
                            "http_status": 0,
                            "payload": payload,
                        }
            except Exception as exc:
                text = str(exc).lower()
                if "margin" in text or "insufficient" in text:
                    return {
                        "ok": False,
                        "category": "MARGIN_LOCK",
                        "error": str(exc),
                        "http_status": 0,
                        "payload": payload,
                    }

        result = {
            "ok": True,
            "category": "ROUTE_OPEN",
            "error": "",
            "http_status": 200,
            "payload": payload,
        }
        _validate_throttle_record_ok(result)
        return result

    def open_positions(self) -> list[dict[str, Any]]:
        self.ensure_session()
        last_status = 0
        for path in (position_otc_list(), "/positions"):
            r = self.request("GET", path, headers=self._auth_headers("2"))
            if r.status_code == 401:
                self.login()
                r = self.request("GET", path, headers=self._auth_headers("2"))
            last_status = int(r.status_code)
            if r.status_code == 200:
                return r.json().get("positions", [])
        raise IGAPIError(
            f"Positions request failed: HTTP {last_status}",
            status_code=last_status,
        )

    def fetch_open_positions(self, epic: str | None = None) -> list[dict[str, Any]]:
        """BrokerAdapter-protocol alias for open_positions() with optional epic filter."""
        positions = self.open_positions()
        if epic is None:
            return positions
        return [p for p in positions if p.get("market", {}).get("epic") == epic]

    def count_open_positions(self, epic: str | None = None) -> int:
        n = 0
        for item in self.open_positions():
            market = item.get("market", {})
            position = item.get("position", {})
            if float(position.get("size", 0)) <= 0:
                continue
            if epic is None or market.get("epic") == epic:
                n += 1
        return n

    def has_open_position(self, epic: str) -> bool:
        return self.count_open_positions(epic) > 0

    def place_market_order(
        self,
        *,
        epic: str,
        direction: str,
        size: float,
        stop_distance: float,
        limit_distance: float | None = None,
        currency_code: str = "GBP",
    ) -> dict[str, Any]:
        from system.engine_log import log_engine

        try:
            from system.node_profile import is_shadow_node
        except Exception:
            def is_shadow_node() -> bool:  # type: ignore[misc]
                return False

        if is_shadow_node() or os.environ.get("IG_AGENT_SHADOW_DESK", "").strip() == "1":
            try:
                from apex.hardening import is_execution_frozen

                if is_execution_frozen():
                    log_engine(
                        f"[SHADOW DESK] Execution frozen (network degraded) — "
                        f"blocked {direction} {epic}"
                    )
                    return {
                        "dealReference": "MOCK_SHADOW_FROZEN",
                        "shadow": True,
                        "status": "EXECUTION_FROZEN",
                    }
            except Exception:
                pass
            deal_ref = "MOCK_SHADOW_ENTRY"
            log_engine(
                f"[SHADOW DESK] Order Intercepted epic={epic} {direction} "
                f"size={size} stop={stop_distance} — no IG REST dispatch"
            )
            try:
                import sqlite3
                from pathlib import Path

                db = Path(os.environ.get("IG_TRIAGE_DB", "src/analytics/triage_v30.db"))
                db.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(str(db)) as conn:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS shadow_orders (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            token TEXT NOT NULL,
                            epic TEXT,
                            direction TEXT,
                            size REAL,
                            stop_distance REAL,
                            created_at TEXT DEFAULT (datetime('now'))
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO shadow_orders(token, epic, direction, size, stop_distance)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (deal_ref, epic, direction.upper(), float(size), float(stop_distance)),
                    )
                    conn.commit()
            except Exception as exc:
                log_engine(
                    f"[SHADOW DESK] ledger write failed: {type(exc).__name__}: {exc}"
                )
            trace_execution(
                "REST",
                "IGRestClient.place_market_order",
                decision="SHADOW intercept",
                params={"dealReference": deal_ref, "epic": epic},
            )
            return {"dealReference": deal_ref, "shadow": True, "status": "MOCK_SHADOW_ENTRY"}

        self.ensure_session()
        micro_lot = False
        try:
            from system.soak_live_fire import soak_mode_enabled
            from trading.live_production_probe import live_probe_enabled

            micro_lot = (soak_mode_enabled() or live_probe_enabled()) and float(size) >= 0.1
        except Exception:
            micro_lot = False

        if micro_lot:
            size = float(size)
            stop_distance = max(float(stop_distance), 5.0)
            if limit_distance is not None:
                limit_distance = max(float(limit_distance), stop_distance)
            currency_code = str(currency_code or "USD").upper()
        else:
            size, stop_distance, limit_distance, currency_code = (
                self.normalize_order_params(
                    epic,
                    size=size,
                    stop_distance=stop_distance,
                    limit_distance=limit_distance,
                    currency_code=currency_code,
                )
            )

        from execution.ig_rest_traffic_governor import consume_positions_otc_transmit_slot

        allowed, governor_reason = consume_positions_otc_transmit_slot(
            epic=epic,
            label="POST /v1/positions/otc — place_market_order",
        )
        if not allowed:
            raise IGOrderError(governor_reason, status_code=429)

        payload: dict[str, Any] = {
            "epic": epic,
            "expiry": "-",
            "direction": direction.upper(),
            "size": float(size),
            "orderType": "MARKET",
            "guaranteedStop": False,
            "forceOpen": True,
            "currencyCode": currency_code,
            "stopDistance": float(stop_distance),
        }
        if limit_distance is not None and float(limit_distance) > 0:
            payload["limitDistance"] = float(limit_distance)

        url = f"{self._base}{position_otc()}"
        log_demo_rest(
            "POST /v1/positions/otc — place order",
            url=url,
            account_id=self.account_id,
            payload=payload,
        )
        trace_execution(
            "REST",
            "IGRestClient.place_market_order",
            decision="POST order",
            params={"url": url, "account_id": self.account_id, "payload": payload},
        )

        r = self.request(
            "POST",
            position_otc(),
            headers=self._auth_headers("2"),
            json=payload,
            budget_priority=True,
        )
        body_preview = (r.text or "")[:500]
        log_demo_rest(
            "POST /v1/positions/otc — response",
            status_code=r.status_code,
            body=body_preview,
        )

        if r.status_code in (401, 403):
            self._raise_auth_or_api(r, "Order placement")
        if r.status_code not in (200, 201):
            trace_execution(
                "REST",
                "IGRestClient.place_market_order",
                decision=f"FAILED HTTP {r.status_code}",
                params={"response_body": body_preview},
            )
            raise IGOrderError(
                f"Order failed: HTTP {r.status_code} — {body_preview}",
                status_code=r.status_code,
            )

        data = r.json()
        trace_execution(
            "REST",
            "IGRestClient.place_market_order",
            decision="success",
            params={"response": data, "dealReference": data.get("dealReference")},
        )
        return data

    def place_limit_entry_atomic(
        self,
        *,
        epic: str,
        direction: str,
        size: float,
        level: float,
        stop_distance: float,
        limit_distance: float | None = None,
        currency_code: str = "GBP",
    ) -> dict[str, Any]:
        """
        LIMIT entry at touch price with stopDistance + limitDistance in one POST payload.
        BUY at offer (ask); SELL at bid — marketable limit for immediate fill without slippage.
        """
        self.ensure_session()
        size, stop_distance, limit_distance, currency_code = (
            self.normalize_order_params(
                epic,
                size=size,
                stop_distance=stop_distance,
                limit_distance=limit_distance,
                currency_code=currency_code,
            )
        )

        from execution.ig_rest_traffic_governor import consume_positions_otc_transmit_slot

        allowed, governor_reason = consume_positions_otc_transmit_slot(
            epic=epic,
            label="POST /v1/positions/otc — atomic limit entry",
        )
        if not allowed:
            raise IGOrderError(governor_reason, status_code=429)

        payload: dict[str, Any] = {
            "epic": epic,
            "expiry": "-",
            "direction": direction.upper(),
            "size": float(size),
            "orderType": "LIMIT",
            "level": float(level),
            "guaranteedStop": False,
            "forceOpen": True,
            "currencyCode": currency_code,
            "stopDistance": float(stop_distance),
        }
        if limit_distance is not None and float(limit_distance) > 0:
            payload["limitDistance"] = float(limit_distance)

        url = f"{self._base}{position_otc()}"
        log_demo_rest(
            "POST /v1/positions/otc — atomic limit entry",
            url=url,
            account_id=self.account_id,
            payload=payload,
        )
        trace_execution(
            "REST",
            "IGRestClient.place_limit_entry_atomic",
            decision="POST limit at touch",
            params={"url": url, "payload": payload},
        )

        r = self.request(
            "POST",
            position_otc(),
            headers=self._auth_headers("2"),
            json=payload,
        )
        body_preview = (r.text or "")[:500]
        log_demo_rest(
            "POST /positions/otc — atomic limit response",
            status_code=r.status_code,
            body=body_preview,
        )

        if r.status_code in (401, 403):
            self._raise_auth_or_api(r, "Limit entry placement")
        if r.status_code not in (200, 201):
            trace_execution(
                "REST",
                "IGRestClient.place_limit_entry_atomic",
                decision=f"FAILED HTTP {r.status_code}",
                params={"response_body": body_preview},
            )
            raise IGOrderError(
                f"Limit entry failed: HTTP {r.status_code} — {body_preview}",
                status_code=r.status_code,
            )

        data = r.json()
        trace_execution(
            "REST",
            "IGRestClient.place_limit_entry_atomic",
            decision="success",
            params={"response": data, "dealReference": data.get("dealReference")},
        )
        return data

    def position_protection_status(self, deal_id: str) -> bool:
        """True when open deal has both stop and limit protection attached."""
        row = self.find_open_position(deal_id)
        if not row:
            return False
        pos = row.get("position") or {}
        has_stop = float(pos.get("stopLevel") or 0) > 0 or float(
            pos.get("stopDistance") or 0
        ) > 0
        has_limit = float(pos.get("limitLevel") or 0) > 0 or float(
            pos.get("limitDistance") or 0
        ) > 0
        return has_stop and has_limit

    def cancel_all_working_orders(self, epic: str | None = None) -> int:
        """Cancel pending working orders; optional epic filter."""
        self.ensure_session()
        r = self.request("GET", "/workingorders", headers=self._auth_headers("2"))
        if r.status_code != 200:
            return 0
        cancelled = 0
        for item in r.json().get("workingOrders", []):
            market = item.get("market") or {}
            pos = item.get("workingOrderData") or item.get("workingOrder") or {}
            deal_id = str(
                pos.get("dealId")
                or pos.get("dealID")
                or item.get("dealId")
                or ""
            ).strip()
            item_epic = str(market.get("epic") or pos.get("epic") or "")
            if epic and item_epic != epic:
                continue
            if not deal_id:
                continue
            try:
                dr = self.request(
                    "DELETE",
                    f"/workingorders/otc/{deal_id}",
                    headers=self._auth_headers("2"),
                )
                if dr.status_code in (200, 201, 204):
                    cancelled += 1
            except Exception:
                pass
        return cancelled

    def flatten_all_positions(self) -> int:
        """Emergency flatten — market close every open position."""
        from system.config_loader import get_config

        cfg = get_config()
        ccy = cfg.currency_code
        closed = 0
        for item in self.open_positions():
            market = item.get("market") or {}
            pos = item.get("position") or {}
            did = str(pos.get("dealId") or "")
            side = str(pos.get("direction") or "BUY").upper()
            size = float(pos.get("size") or 0)
            epic = str(market.get("epic") or "")
            if not did or size <= 0:
                continue
            close_dir = "SELL" if side == "BUY" else "BUY"
            try:
                self.close_position(
                    did,
                    direction=close_dir,
                    size=size,
                    epic=epic or None,
                    currency_code=ccy,
                    verify=True,
                )
                closed += 1
            except Exception:
                pass
        return closed

    def find_open_position(self, deal_id: str) -> dict[str, Any] | None:
        """Return raw IG positions entry for dealId, or None."""
        want = str(deal_id).strip()
        if not want:
            return None
        for item in self.open_positions():
            pos = item.get("position") or {}
            if str(pos.get("dealId") or pos.get("dealID") or "") == want:
                return item
        return None

    def get_position_otc(self, deal_id: str) -> dict[str, Any] | None:
        """GET /positions/otc/{dealId} — verify OTC position; fallback to list scan."""
        want = str(deal_id).strip()
        if not want:
            return None
        try:
            self.ensure_session()
            r = self.request(
                "GET",
                update_position(want),
                headers=self._auth_headers("2"),
                budget_priority=True,
            )
            if r.status_code == 200:
                body = r.json()
                if isinstance(body, dict) and body:
                    return body
            if r.status_code == 404:
                return None
        except Exception:
            pass
        return self.find_open_position(want)

    def ensure_protective_stops(
        self,
        deal_id: str,
        *,
        epic: str,
        stop_distance: float,
        limit_distance: float,
    ) -> bool:
        """
        Attach missing stop and/or limit when IG shows an open deal without full protection.

        PUT /positions/otc/{dealId} requires absolute stopLevel/limitLevel — not stopDistance.
        We derive the absolute levels from the open position's level and direction.
        """
        row = self.find_open_position(deal_id)
        if not row:
            return False
        pos = row.get("position") or {}
        mkt = row.get("market") or {}
        has_stop = (
            float(pos.get("stopLevel") or 0) > 0
            or float(pos.get("stopDistance") or 0) > 0
        )
        has_limit = (
            float(pos.get("limitLevel") or 0) > 0
            or float(pos.get("limitDistance") or 0) > 0
        )
        if has_stop and has_limit:
            return True

        direction = str(pos.get("direction") or "").upper()
        entry = float(pos.get("level") or pos.get("openLevel") or 0)
        if entry <= 0:
            return False

        from system.pnl_math import ig_points_to_price_delta, pip_size_for_epic

        is_fx = pip_size_for_epic(epic) is not None
        stop_level = None
        limit_level = None
        stop_dist = None
        limit_dist = None

        if is_fx:
            # FX CFD: IG stopDistance/limitDistance are in points (pips), not price units.
            # Using entry ± distance as absolute stopLevel produces invalid levels (~46 on 1.34).
            if not has_stop and float(stop_distance) > 0:
                stop_dist = float(stop_distance)
            if not has_limit and float(limit_distance) > 0:
                limit_dist = float(limit_distance)
        else:
            # Index/commodity: convert IG points to price delta, then absolute level.
            if not has_stop and float(stop_distance) > 0:
                delta = ig_points_to_price_delta(epic, float(stop_distance))
                stop_level = (
                    (entry + delta) if direction == "SELL" else (entry - delta)
                )
            if not has_limit and float(limit_distance) > 0:
                delta = ig_points_to_price_delta(epic, float(limit_distance))
                limit_level = (
                    (entry - delta) if direction == "SELL" else (entry + delta)
                )

        if (
            stop_level is None
            and limit_level is None
            and stop_dist is None
            and limit_dist is None
        ):
            return True

        try:
            self.update_position_stops(
                deal_id,
                stop_level=stop_level,
                limit_level=limit_level,
                stop_distance=stop_dist,
                limit_distance=limit_dist,
            )
            log_demo_rest(
                "PUT /positions/otc — attach stops"
                + (" (FX distances)" if is_fx else " (absolute levels)"),
                deal_id=deal_id,
                epic=epic,
                direction=direction,
                entry=entry,
                stop_level=stop_level,
                limit_level=limit_level,
                stop_distance=stop_dist,
                limit_distance=limit_dist,
            )
            return True
        except Exception as e:
            log_demo_rest(
                "PUT /positions/otc — attach stops failed",
                deal_id=deal_id,
                error=str(e),
            )
            return False

    def is_position_open(self, deal_id: str) -> bool:
        return self.find_open_position(deal_id) is not None

    def flatten_epic_positions(
        self,
        epic: str,
        *,
        currency_code: str | None = None,
        max_rounds: int = 6,
    ) -> int:
        """
        Close every open position on epic (MARKET). Returns number of closes attempted.
        """
        from system.config_loader import get_config

        cfg = get_config()
        ccy = currency_code or cfg.currency_code
        closed = 0
        for _ in range(max_rounds):
            targets: list[tuple[str, str, float]] = []
            for item in self.open_positions():
                market = item.get("market") or {}
                if market.get("epic") != epic:
                    continue
                pos = item.get("position") or {}
                did = str(pos.get("dealId") or "")
                side = str(pos.get("direction") or "BUY").upper()
                size = float(pos.get("size") or 0)
                if did and size > 0:
                    targets.append((did, side, size))
            if not targets:
                break
            for did, side, size in targets:
                close_dir = "SELL" if side == "BUY" else "BUY"
                self.close_position(
                    did,
                    direction=close_dir,
                    size=size,
                    epic=epic,
                    currency_code=ccy,
                    verify=True,
                )
                closed += 1
                time.sleep(1.5)
        return closed

    def close_position(
        self,
        deal_id: str,
        *,
        direction: str,
        size: float,
        epic: str | None = None,
        currency_code: str | None = None,
        verify: bool = True,
    ) -> dict[str, Any]:
        """
        Close an open OTC position.

        Uses DELETE /positions/otc with MARKET (IG rejects LIMIT+level on many CFDs).
        On failure, nets via MARKET with forceOpen=false, then verifies the deal closed.
        """
        from execution.exit_inflight import (
            clear_exit,
            set_exit_deal_reference,
            try_begin_exit,
        )
        from execution.pending_order_reconcile import (
            ORDER_TYPE_EXIT,
            mark_pending,
            resolve_pending,
        )

        epic_key = (epic or "").strip()
        guarded = bool(epic_key)
        if guarded and not try_begin_exit(epic_key):
            return {
                "skipped": True,
                "reason": f"Exit already in flight for {epic_key} — skipped duplicate",
                "verified_closed": False,
            }
        try:
            data = self._do_close_position(
                deal_id,
                direction=direction,
                size=size,
                epic=epic,
                currency_code=currency_code,
                verify=verify,
                set_deal_reference=(set_exit_deal_reference if guarded else None),
                guarded_epic=epic_key if guarded else "",
            )
            if guarded and bool(data.get("verified_closed")):
                resolve_pending(epic_key, reason="exit confirmed by broker")
            return data
        except Exception:
            if guarded:
                mark_pending(
                    epic_key,
                    side=str(direction or "").upper(),
                    order_type=ORDER_TYPE_EXIT,
                    deal_reference=str(deal_id or ""),
                )
            raise
        finally:
            if guarded:
                clear_exit(epic_key)

    def _do_close_position(
        self,
        deal_id: str,
        *,
        direction: str,
        size: float,
        epic: str | None = None,
        currency_code: str | None = None,
        verify: bool = True,
        set_deal_reference: Any = None,
        guarded_epic: str = "",
    ) -> dict[str, Any]:
        self.ensure_session()
        deal_id = str(deal_id).strip()
        ig_row = self.find_open_position(deal_id)
        if ig_row:
            pos = ig_row.get("position") or {}
            direction = str(pos.get("direction") or direction).upper()
            size = float(pos.get("size") or size)
            close_dir = "SELL" if direction == "BUY" else "BUY"
        else:
            close_dir = direction.upper()
        size_f = float(size)
        epic_use = epic or ""

        payload: dict[str, Any] = {
            "dealId": deal_id,
            "direction": close_dir,
            "size": size_f,
            "orderType": "MARKET",
            "timeInForce": "FILL_OR_KILL",
        }

        log_demo_rest("DELETE /positions/otc — close", deal_id=deal_id, payload=payload)
        r = self.request(
            "DELETE",
            position_otc(),
            headers=self._auth_headers("1"),
            json=payload,
        )
        body_preview = (r.text or "")[:500]
        log_demo_rest(
            "DELETE /positions/otc — response",
            status_code=r.status_code,
            body=body_preview,
        )
        if r.status_code in (200, 201):
            data = r.json()
            ref = data.get("dealReference", "")
            if ref:
                if set_deal_reference is not None and guarded_epic:
                    set_deal_reference(guarded_epic, ref)
                data["confirm"] = self.confirm_deal(ref)
            time.sleep(0.8)
            if not verify or not self.is_position_open(deal_id):
                data["verified_closed"] = True
                return data
            log_demo_rest(
                "DELETE close accepted but deal still open — retrying net close",
                deal_id=deal_id,
            )

        if epic_use:
            from system.config_loader import get_config

            cfg = get_config()
            ccy = currency_code or cfg.currency_code
            size_n, _, _, ccy_n = self.normalize_order_params(
                epic_use,
                size=size_f,
                stop_distance=float(cfg.stop_distance_points),
                limit_distance=float(cfg.limit_distance_points),
                currency_code=ccy,
            )
            net_payload: dict[str, Any] = {
                "epic": epic_use,
                "expiry": "-",
                "direction": close_dir,
                "size": size_n,
                "orderType": "MARKET",
                "guaranteedStop": False,
                "forceOpen": False,
                "currencyCode": ccy_n,
            }
            log_demo_rest(
                "POST /positions/otc — net close (forceOpen=false)",
                deal_id=deal_id,
                payload=net_payload,
            )
            r2 = self.request(
                "POST",
                position_otc(),
                headers=self._auth_headers("2"),
                json=net_payload,
            )
            log_demo_rest(
                "POST /positions/otc — net close response",
                status_code=r2.status_code,
                body=(r2.text or "")[:500],
            )
            if r2.status_code in (200, 201):
                data = r2.json()
                ref = data.get("dealReference", "")
                if ref:
                    if set_deal_reference is not None and guarded_epic:
                        set_deal_reference(guarded_epic, ref)
                    data["confirm"] = self.confirm_deal(ref)
                time.sleep(1.0)
                still_open = self.is_position_open(deal_id)
                data["verified_closed"] = not still_open if verify else True
                if verify and still_open:
                    log_demo_rest(
                        "Net close returned OK but deal still open",
                        deal_id=deal_id,
                    )
                if verify and epic_use:
                    extras = self.count_open_positions(epic_use)
                    if extras > 0:
                        log_demo_rest(
                            "Net close left open epic exposure — flattening",
                            epic=epic_use,
                            open_count=extras,
                        )
                        self.flatten_epic_positions(
                            epic_use, currency_code=ccy_n, max_rounds=3
                        )
                        data["verified_closed"] = (
                            self.count_open_positions(epic_use) == 0
                        )
                return data
            self._raise_auth_or_api(r2, "Net close position")

        if r.status_code not in (200, 201):
            self._raise_auth_or_api(r, "Close position")
        return r.json()

    def confirm_deal(
        self,
        deal_reference: str,
        *,
        max_wait_seconds: float = 15.0,
        poll_interval_seconds: float = 0.65,
    ) -> dict[str, Any]:
        self.ensure_session()
        deadline = time.time() + max_wait_seconds
        while time.time() < deadline:
            r = self.request(
                "GET",
                f"/confirms/{deal_reference}",
                headers=self._auth_headers("1"),
                budget_priority=True,
            )
            if r.status_code != 200:
                time.sleep(poll_interval_seconds)
                continue
            body = r.json()
            status = str(body.get("dealStatus", body.get("status", ""))).upper()
            if status in ("ACCEPTED", "REJECTED"):
                affected = body.get("affectedDeals") or []
                affected_reason = ""
                if affected and isinstance(affected[0], dict):
                    affected_reason = str(
                        affected[0].get("reason") or affected[0].get("status") or ""
                    )
                reason = (
                    body.get("reason")
                    or body.get("reasonCode")
                    or body.get("rejectReason")
                    or affected_reason
                    or body.get("errorCode")
                    or body.get("errorMessage")
                    or ""
                )
                result = {
                    "terminal": True,
                    "accepted": status == "ACCEPTED",
                    "rejected": status == "REJECTED",
                    "deal_id": body.get("dealId"),
                    "deal_reference": deal_reference,
                    "reason": str(reason),
                    "status": status,
                    "raw": body,
                }
                log_demo_rest(
                    "GET /confirms — deal status",
                    deal_reference=deal_reference,
                    status=status,
                    reason=reason,
                    deal_id=body.get("dealId"),
                )
                trace_execution(
                    "REST",
                    "IGRestClient.confirm_deal",
                    decision=f"dealStatus={status}",
                    params={"confirm": result},
                )
                return result
            time.sleep(poll_interval_seconds)
        return {
            "terminal": False,
            "accepted": False,
            "rejected": False,
            "deal_id": None,
            "reason": "confirm timeout",
            "status": "TIMEOUT",
        }

    def update_position_stops(
        self,
        deal_id: str,
        *,
        stop_level: float | None = None,
        limit_level: float | None = None,
        stop_distance: float | None = None,
        limit_distance: float | None = None,
    ) -> dict[str, Any]:
        self.ensure_session()
        payload: dict[str, Any] = {}
        if stop_level is not None:
            payload["stopLevel"] = stop_level
        if limit_level is not None:
            payload["limitLevel"] = limit_level
        if stop_distance is not None:
            payload["stopDistance"] = stop_distance
        if limit_distance is not None:
            payload["limitDistance"] = limit_distance
        r = self.request(
            "PUT",
            update_position(deal_id),
            headers=self._auth_headers("2"),
            json=payload,
        )
        if r.status_code not in (200, 201):
            raise IGAPIError(
                f"Update stops failed: HTTP {r.status_code}", status_code=r.status_code
            )
        return r.json()

    def _auth_headers(self, version: str = "3") -> dict[str, str]:
        return self._auth.authenticated_headers(version, account_id=self.account_id)

    @staticmethod
    def _raise_auth_or_api(r: requests.Response, context: str) -> None:
        body = (r.text or "")[:400]
        log_demo_rest(f"{context} failed", status_code=r.status_code, body=body)
        code = parse_rate_limit_error(r.status_code, body)
        if code:
            get_rate_limit_manager().handle_http_response(r, source=context)
        if r.status_code in (401, 403):
            raise IGAuthError(
                f"{context}: HTTP {r.status_code} — {body}", status_code=r.status_code
            )
        raise IGAPIError(
            f"{context}: HTTP {r.status_code} — {body}", status_code=r.status_code
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        auth_required: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        from system.rest_api_budget import RestBudgetPausedError, get_rest_api_budget

        try:
            from execution.atomic_gateway import ig_radio_silence_blocks_rest

            if auth_required and ig_radio_silence_blocks_rest(method, path):
                raise IGAPIError(
                    f"HOLD: IG_RADIO_SILENCE — passive REST blocked: {method} {path}"
                )
        except IGAPIError:
            raise
        except Exception:
            pass

        # budget_priority=True bypasses the min-interval wait and hard cap for
        # critical order-confirmation calls — must be popped before passing to
        # requests.Session.request() which does not accept this kwarg.
        budget_priority: bool = kwargs.pop("budget_priority", False)

        mgr = get_rate_limit_manager()
        if auth_required:
            try:
                get_rest_api_budget().acquire(
                    label=f"{method} {path}", priority=budget_priority
                )
            except RestBudgetPausedError as exc:
                raise IGAPIError(f"REST deferred ({exc})") from exc
        else:
            mgr.check_rest_allowed()

        url = path if path.startswith("http") else f"{self._base}{path}"
        timeout = kwargs.pop("timeout", self.timeout_seconds)
        try:
            from system.network_bounds import clamp_read_timeout

            timeout = clamp_read_timeout(method, timeout, default=self.timeout_seconds)
        except Exception:
            timeout = float(timeout or self.timeout_seconds)
        last_exc: Exception | None = None

        if auth_required and not self._session_path_protected(path):
            self.proactive_refresh_if_needed()

        relogin_done = False
        for attempt in range(1, self.max_retries + 1):
            try:
                r = self._session.request(method, url, timeout=timeout, **kwargs)
                if parse_rate_limit_error(r.status_code, r.text):
                    mgr.handle_http_response(r, source="REST", path=path)
                if auth_required and r.status_code == 401:
                    if not relogin_done:
                        relogin_done = True
                        log_demo_rest(
                            "HTTP 401 — token eviction + clean re-auth",
                            path=path,
                        )
                        if self._token_eviction_reauth():
                            if "headers" in kwargs:
                                ver = kwargs["headers"].get("VERSION", "3")
                                kwargs["headers"] = self._auth_headers(str(ver))
                            continue
                        self._log_auth_failure_critical()
                        return self._auth_failure_response(path)
                    return self._auth_failure_response(path)
                if (
                    r.status_code in (429, 500, 502, 503, 504)
                    and attempt < self.max_retries
                ):
                    time.sleep(self.retry_delay_seconds * attempt)
                    continue
                if 200 <= r.status_code < 300:
                    self.record_rest_success(path)
                return r
            except RateLimitError:
                raise
            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
            ) as e:
                last_exc = e
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay_seconds * attempt)
                    continue
                raise IGAPIError(f"Network error: {e}") from e

        raise IGAPIError(f"Request failed after retries: {last_exc}")


def get_shared_rest_client(credentials: Credentials | None = None) -> IGRestClient:
    """Compat shim — delegates to process-wide session in ``system.ig_rest_session``."""
    from system.credentials_loader import load_credentials
    from system.ig_rest_session import get_shared_rest_client as _session_client

    creds = credentials or load_credentials()
    return _session_client(creds)
