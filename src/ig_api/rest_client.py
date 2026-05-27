"""
IG REST API client — authenticates via :class:`~system.credentials_loader.Credentials`.
"""

from __future__ import annotations

import json
import time
from typing import Any

import requests

from ig_api.auth import AuthManager, SessionTokens
from ig_api.exceptions import IGAPIError, IGAuthError, IGOrderError, RateLimitError
from system.rate_limit_manager import get_rate_limit_manager, parse_rate_limit_error
from system.credentials_loader import Credentials
from system.demo_execution_trace import trace_execution, update_demo_diagnostics
from system.demo_rest_log import log_demo_rest, mask_token


class IGRestClient:
    """Synchronous IG REST client."""

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
        self._base = (
            "https://demo-api.ig.com/gateway/deal"
            if self.account_type == "DEMO"
            else "https://api.ig.com/gateway/deal"
        )
        self._bid = 0.0
        self._offer = 0.0
        self._last_login_status: int | None = None
        self._last_auth_error: str = ""
        self._market_constraints_cache: dict[str, dict[str, Any]] = {}
        self._live_price_cache: dict[str, dict[str, Any]] = {}
        self._account_balance: float | None = None
        self._account_profit_loss: float | None = None
        self._account_available: float | None = None
        self._last_rest_ok_at: float = 0.0
        self._last_rest_ok_path: str = ""
        self._last_account_refresh_ts: float = 0.0
        self._last_stream_activity_at: float = 0.0

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
                get_rate_limit_manager().handle_http_response(r, source="login", path="/session")
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
            self._account_balance = float(info.get("balance")) if info.get("balance") is not None else None
            self._account_profit_loss = (
                float(info.get("profitLoss")) if info.get("profitLoss") is not None else None
            )
            self._account_available = (
                float(info.get("available")) if info.get("available") is not None else None
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
            log_demo_rest("Account switch skipped — already on target account", account_id=self.account_id)
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
        try:
            r = self.request("POST", "/session/refresh", headers=self._auth_headers("1"))
            if r.status_code in (200, 201):
                return self._auth.apply_login_response(
                    dict(r.headers), r.json(), preferred_account_id=self.account_id
                )
        except IGAPIError:
            pass
        return self.login()

    def ensure_session(self) -> None:
        get_rate_limit_manager().check_rest_allowed()
        if not self._auth.tokens or not self._auth.tokens.is_valid:
            self.login()

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
        self, epic: str, *, max_age_seconds: float = 300.0
    ) -> dict[str, Any]:
        """IG dealing rules for an epic (cached to limit API calls)."""
        now = time.time()
        cached = self._market_constraints_cache.get(epic)
        if cached and now - float(cached.get("ts", 0)) < max_age_seconds:
            return dict(cached["data"])

        self.ensure_session()
        r = self.request("GET", f"/markets/{epic}", headers=self._auth_headers("3"))
        if r.status_code == 401:
            self.login()
            r = self.request("GET", f"/markets/{epic}", headers=self._auth_headers("3"))
        if r.status_code == 403:
            code = parse_rate_limit_error(r.status_code, r.text)
            if code:
                get_rate_limit_manager().handle_http_response(r, source="markets", path=epic)
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
            "min_stop_distance": self._dealing_rule_value(rules, "minNormalStopOrLimitDistance"),
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

        if norm_size != size or norm_stop != stop_distance or norm_ccy != currency_code.upper():
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
    ) -> tuple[float, float]:
        """
        Fresh bid/offer for streaming/UI — short cache (default 1s).
        Dealing rules use the separate 300s constraints cache.
        """
        now = time.time()
        cached = self._live_price_cache.get(epic)
        if cached and now - float(cached.get("ts", 0)) < max_age_seconds:
            return float(cached["bid"]), float(cached["offer"])

        self.ensure_session()
        r = self.request("GET", f"/markets/{epic}", headers=self._auth_headers("3"))
        if r.status_code == 401:
            self.login()
            r = self.request("GET", f"/markets/{epic}", headers=self._auth_headers("3"))
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

    def fetch_market_snapshot(self, epic: str, *, live: bool = False) -> dict[str, Any]:
        c = self.fetch_market_constraints(epic)
        if live:
            bid, offer = self.fetch_live_prices(epic)
        else:
            bid = float(c["bid"])
            offer = float(c["offer"])
        self._bid, self._offer = bid, offer
        return {
            "epic": epic,
            "bid": bid,
            "offer": offer,
            "snapshot": {"bid": bid, "offer": offer, "marketStatus": c["market_status"]},
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

    def maybe_refresh_account_summary(
        self, *, min_interval: float = 60.0
    ) -> dict[str, float | None]:
        """Throttled GET /accounts — avoids UI/stream hammering IG rate limits."""
        from system.market_watch.calendar import background_rest_paused
        from system.rest_api_budget import RestBudgetPausedError, order_in_flight_paused

        if order_in_flight_paused("account_summary"):
            return self.get_cached_account_summary()
        if background_rest_paused("account_summary"):
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
        except Exception:
            return self.get_cached_account_summary()

    def refresh_account_summary(self) -> dict[str, float | None]:
        """Refresh balance / P&L from GET /accounts (used by stream heartbeat)."""
        self.ensure_session()
        r = self.request("GET", "/accounts", headers=self._auth_headers("1"))
        if r.status_code != 200:
            return self.get_cached_account_summary()
        for acc in r.json().get("accounts", []):
            if str(acc.get("accountId")) != self.account_id:
                continue
            bal = acc.get("balance") or {}
            try:
                self._account_balance = (
                    float(bal.get("balance")) if bal.get("balance") is not None else self._account_balance
                )
                self._account_profit_loss = (
                    float(bal.get("profitLoss"))
                    if bal.get("profitLoss") is not None
                    else self._account_profit_loss
                )
                self._account_available = (
                    float(bal.get("available"))
                    if bal.get("available") is not None
                    else self._account_available
                )
            except (TypeError, ValueError):
                pass
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

        def _fetch(version: str, psize: int) -> requests.Response:
            return self.request(
                "GET",
                path,
                headers=self._auth_headers(version),
                params={"pageSize": max(1, min(int(psize), 500))},
            )

        last_preview = ""
        for version in ("2", "1"):
            for psize in (page_size, min(page_size, 100), 50):
                r = _fetch(version, psize)
                if r.status_code == 401:
                    self.login()
                    r = _fetch(version, psize)
                if r.status_code == 200:
                    txns = list(r.json().get("transactions") or [])
                    if txns or version == "1":
                        log_demo_rest(
                            "GET history/transactions OK",
                            version=version,
                            count=len(txns),
                            path=path,
                        )
                        return txns
                last_preview = (r.text or "")[:200]

        # Shorter window retry (IG DEMO sometimes 500 on wide ranges)
        from datetime import datetime, timedelta

        from system.ig_transactions import ig_date_range_dd_mm_yyyy

        short_start, short_end = ig_date_range_dd_mm_yyyy(days_back=1)
        path2 = f"/history/transactions/{txn_type}/{quote(short_start, safe='')}/{quote(short_end, safe='')}"
        for version in ("2", "1"):
            r = self.request(
                "GET",
                path2,
                headers=self._auth_headers(version),
                params={"pageSize": 50},
            )
            if r.status_code == 200:
                txns = list(r.json().get("transactions") or [])
                log_demo_rest(
                    "GET history/transactions OK (1d window)",
                    version=version,
                    count=len(txns),
                )
                return txns

        log_demo_rest(
            "GET history/transactions failed",
            status_code=getattr(r, "status_code", 0),
            path=path,
            preview=last_preview,
        )
        return []

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
            raise IGAPIError(f"Accounts request failed: HTTP {r.status_code}", status_code=r.status_code)
        accounts = r.json().get("accounts", [])
        for acc in accounts:
            if str(acc.get("accountId")) == self.account_id:
                bal = acc.get("balance", {}).get("available")
                return float(bal) if bal is not None else 0.0
        if accounts:
            bal = accounts[0].get("balance", {}).get("available")
            return float(bal) if bal is not None else 0.0
        return 0.0

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
            if r.status_code != 200:
                return []
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
                out.append({
                    "time": snap,
                    "open": float(op.get("mid") or op.get("bid") or 0),
                    "high": float(hi.get("mid") or hi.get("ask") or hi.get("offer") or 0),
                    "low": float(lo.get("mid") or lo.get("bid") or 0),
                    "close": float(cl.get("mid") or cl.get("bid") or 0),
                    "bid_close": float(cl.get("bid") or 0),
                    "offer_close": float(cl.get("ask") or cl.get("offer") or 0),
                })
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
        if market_bid is not None and market_offer is not None and market_bid > 0 and market_offer > 0:
            bid, offer = float(market_bid), float(market_offer)
        else:
            snap = self.fetch_market_snapshot(epic)
            bid, offer = float(snap["bid"]), float(snap["offer"])
        if bid <= 0 or offer <= 0:
            return {"ok": False, "error": "Invalid market snapshot prices", "is_mock": False}

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

    def open_positions(self) -> list[dict[str, Any]]:
        self.ensure_session()
        r = self.request("GET", "/positions", headers=self._auth_headers("2"))
        if r.status_code == 401:
            self.login()
            r = self.request("GET", "/positions", headers=self._auth_headers("2"))
        if r.status_code != 200:
            raise IGAPIError(f"Positions request failed: HTTP {r.status_code}", status_code=r.status_code)
        return r.json().get("positions", [])

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
        self.ensure_session()
        size, stop_distance, limit_distance, currency_code = self.normalize_order_params(
            epic,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            currency_code=currency_code,
        )
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

        url = f"{self._base}/positions/otc"
        log_demo_rest(
            "POST /positions/otc — place order",
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
            "/positions/otc",
            headers=self._auth_headers("2"),
            json=payload,
        )
        body_preview = (r.text or "")[:500]
        log_demo_rest(
            "POST /positions/otc — response",
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
        """
        row = self.find_open_position(deal_id)
        if not row:
            return False
        pos = row.get("position") or {}
        has_stop = float(pos.get("stopLevel") or 0) > 0 or float(pos.get("stopDistance") or 0) > 0
        has_limit = float(pos.get("limitLevel") or 0) > 0 or float(pos.get("limitDistance") or 0) > 0
        if has_stop and has_limit:
            return True

        add_stop = None
        add_limit = None
        if not has_stop and float(stop_distance) > 0:
            add_stop = float(stop_distance)
        if not has_limit and float(limit_distance) > 0:
            add_limit = float(limit_distance)
        if add_stop is None and add_limit is None:
            return True

        try:
            self.update_position_stops(
                deal_id,
                stop_distance=add_stop,
                limit_distance=add_limit,
            )
            log_demo_rest(
                "PUT /positions/otc — attach stops",
                deal_id=deal_id,
                epic=epic,
                stop_distance=add_stop,
                limit_distance=add_limit,
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
                set_deal_reference=(
                    set_exit_deal_reference if guarded else None
                ),
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
            "/positions/otc",
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
                "/positions/otc",
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
                        data["verified_closed"] = self.count_open_positions(epic_use) == 0
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
                        affected[0].get("reason")
                        or affected[0].get("status")
                        or ""
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
            f"/positions/otc/{deal_id}",
            headers=self._auth_headers("2"),
            json=payload,
        )
        if r.status_code not in (200, 201):
            raise IGAPIError(f"Update stops failed: HTTP {r.status_code}", status_code=r.status_code)
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
            raise IGAuthError(f"{context}: HTTP {r.status_code} — {body}", status_code=r.status_code)
        raise IGAPIError(f"{context}: HTTP {r.status_code} — {body}", status_code=r.status_code)

    def request(
        self,
        method: str,
        path: str,
        *,
        auth_required: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        from system.rest_api_budget import RestBudgetPausedError, get_rest_api_budget

        mgr = get_rate_limit_manager()
        if auth_required:
            try:
                get_rest_api_budget().acquire(label=f"{method} {path}")
            except RestBudgetPausedError as exc:
                raise IGAPIError(f"REST deferred ({exc})") from exc
        else:
            mgr.check_rest_allowed()

        url = path if path.startswith("http") else f"{self._base}{path}"
        timeout = kwargs.pop("timeout", self.timeout_seconds)
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                r = self._session.request(method, url, timeout=timeout, **kwargs)
                if parse_rate_limit_error(r.status_code, r.text):
                    mgr.handle_http_response(r, source="REST", path=path)
                if auth_required and r.status_code == 401 and attempt < self.max_retries:
                    log_demo_rest("HTTP 401 — refreshing session", path=path)
                    self.login()
                    if "headers" in kwargs:
                        ver = kwargs["headers"].get("VERSION", "3")
                        kwargs["headers"] = self._auth_headers(str(ver))
                    continue
                if r.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    time.sleep(self.retry_delay_seconds * attempt)
                    continue
                if 200 <= r.status_code < 300:
                    self.record_rest_success(path)
                return r
            except RateLimitError:
                raise
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                last_exc = e
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay_seconds * attempt)
                    continue
                raise IGAPIError(f"Network error: {e}") from e

        raise IGAPIError(f"Request failed after retries: {last_exc}")
