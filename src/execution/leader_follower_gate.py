"""Leader-follower futures correlation proxy — pure in-memory micro-momentum.

No network I/O. Observes streaming mids (IG quote and optional leader epic /
high-velocity futures proxy) and vetoes BUY when the leader prints negative
micro-momentum. Divergent proxy vs IG tick direction for >3 consecutive ticks
arms a localized 5s entry veto (false-breakout guard).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

_MAX_TICKS = 256  # headroom for high-velocity futures ticks
_DEFAULT_LOOKBACK = 8
_DEFAULT_EPS = 1e-9
_DIVERGENCE_STREAK_LIMIT = 3
_DIVERGENCE_VETO_SEC = 5.0

_lock = threading.Lock()
# epic -> deque[(ts, mid)]
_mids: dict[str, deque[tuple[float, float]]] = {}
# per IG epic: consecutive divergent tick count + veto deadline
_divergence_streak: dict[str, int] = {}
_divergence_veto_until: dict[str, float] = {}
_last_ig_mid: dict[str, float] = {}
_last_proxy_mid: dict[str, float] = {}

# Default IG CFD → leader proxy key (self until a true futures feed is wired)
_DEFAULT_LEADER_MAP = {
    "IX.D.DOW.IFM.IP": "PROXY.US30",
    "IX.D.NASDAQ.Cash.IP": "PROXY.US100",
    "IX.D.NIKKEI.IFM.IP": "PROXY.JP225",
    "CS.D.CFPGOLD.CFP.IP": "PROXY.XAU",
}


def reset_leader_follower_for_tests() -> None:
    with _lock:
        _mids.clear()
        _divergence_streak.clear()
        _divergence_veto_until.clear()
        _last_ig_mid.clear()
        _last_proxy_mid.clear()


def _leader_map(cfg: Any | None) -> dict[str, str]:
    out = dict(_DEFAULT_LEADER_MAP)
    if cfg is not None and hasattr(cfg, "get"):
        try:
            block = cfg.get("leader_follower") or {}
            if isinstance(block, dict):
                custom = block.get("leader_map") or {}
                if isinstance(custom, dict):
                    out.update({str(k): str(v) for k, v in custom.items()})
        except Exception:
            pass
    return out


def observe_leader_proxy_mid(
    key: str,
    mid: float,
    *,
    now: float | None = None,
) -> None:
    """Push a mid print into the proxy ring (hot-path safe, high-velocity ready)."""
    k = str(key or "").strip()
    m = float(mid or 0)
    if not k or m <= 0:
        return
    t = time.time() if now is None else float(now)
    with _lock:
        q = _mids.get(k)
        if q is None:
            q = deque(maxlen=_MAX_TICKS)
            _mids[k] = q
        q.append((t, m))


def observe_from_ig_quote(
    epic: str,
    bid: float,
    offer: float,
    *,
    cfg: Any | None = None,
    shadow_proxy: bool = True,
) -> None:
    """
    Feed the IG epic mid ring. Optionally shadow-copy into the mapped proxy
    when no external futures feed is connected (``shadow_proxy=True``).

    True high-velocity futures should call ``observe_leader_proxy_mid`` on the
    PROXY.* key separately — then set shadow_proxy=False via config to avoid
    overwriting real leader ticks.
    """
    b = float(bid or 0)
    o = float(offer or 0)
    if b <= 0 or o <= b:
        return
    mid = (b + o) / 2.0
    observe_leader_proxy_mid(str(epic), mid)
    block: dict[str, Any] = {}
    if cfg is not None and hasattr(cfg, "get"):
        try:
            raw = cfg.get("leader_follower") or {}
            if isinstance(raw, dict):
                block = raw
        except Exception:
            block = {}
    use_shadow = bool(block.get("shadow_proxy", shadow_proxy))
    leader = _leader_map(cfg).get(str(epic or "").strip())
    if leader and use_shadow:
        # Only shadow when proxy ring is empty / stale (>2s) so live futures win
        with _lock:
            pq = _mids.get(leader)
            stale = (not pq) or (time.time() - pq[-1][0] > 2.0)
        if stale:
            observe_leader_proxy_mid(leader, mid)


def micro_momentum(
    key: str,
    *,
    lookback: int = _DEFAULT_LOOKBACK,
) -> float | None:
    """
    Instantaneous micro-momentum ≈ (mid_now - mid_then) / mid_then over lookback ticks.

    Returns None when insufficient samples.
    """
    k = str(key or "").strip()
    n = max(2, int(lookback))
    with _lock:
        q = _mids.get(k)
        if not q or len(q) < n:
            return None
        sample = list(q)[-n:]
    m0 = sample[0][1]
    m1 = sample[-1][1]
    if m0 <= 0:
        return None
    return (m1 - m0) / m0


def _sign_delta(delta: float, eps: float) -> int:
    if delta > eps:
        return 1
    if delta < -eps:
        return -1
    return 0


def _update_divergence(
    epic: str,
    ig_mid: float,
    proxy_mid: float,
    *,
    eps: float,
    now: float,
    streak_limit: int = _DIVERGENCE_STREAK_LIMIT,
    veto_sec: float = _DIVERGENCE_VETO_SEC,
) -> tuple[bool, str]:
    """
    Track IG tick direction vs proxy delta. >streak_limit consecutive
    divergences → arm veto_sec localized entry block.
    """
    key = str(epic or "").strip()
    with _lock:
        prev_ig = _last_ig_mid.get(key)
        prev_px = _last_proxy_mid.get(key)
        _last_ig_mid[key] = ig_mid
        _last_proxy_mid[key] = proxy_mid
        until = float(_divergence_veto_until.get(key) or 0.0)
        if now < until:
            return False, f"leader_follower_divergence_veto remaining={until - now:.1f}s"

        if prev_ig is None or prev_px is None or prev_ig <= 0 or prev_px <= 0:
            _divergence_streak[key] = 0
            return True, "leader_follower_divergence_warm"

        ig_delta = ig_mid - prev_ig
        px_delta = proxy_mid - prev_px
        # Relative eps so high-velocity index ticks register
        ig_eps = max(eps, abs(prev_ig) * 1e-8)
        px_eps = max(eps, abs(prev_px) * 1e-8)
        sig_ig = _sign_delta(ig_delta, ig_eps)
        sig_px = _sign_delta(px_delta, px_eps)

        if sig_ig != 0 and sig_px != 0 and sig_ig != sig_px:
            streak = int(_divergence_streak.get(key) or 0) + 1
            _divergence_streak[key] = streak
            if streak > streak_limit:
                _divergence_veto_until[key] = now + float(veto_sec)
                _divergence_streak[key] = 0
                return (
                    False,
                    f"leader_follower_divergence_veto streak={streak} armed={veto_sec:.0f}s",
                )
        else:
            _divergence_streak[key] = 0

        return True, f"leader_follower_divergence_ok streak={_divergence_streak.get(key, 0)}"


def evaluate_leader_follower_gate(
    epic: str,
    direction: str,
    *,
    bid: float = 0.0,
    offer: float = 0.0,
    cfg: Any | None = None,
) -> tuple[bool, str]:
    """
    Return (allowed, reason).

    Veto BUY when leader/proxy micro-momentum is negative.
    SELL veto when momentum is strongly positive (symmetric drag-up risk).
    Also enforces short divergence veto when proxy vs IG tick disagree.
    """
    block: dict[str, Any] = {}
    if cfg is not None and hasattr(cfg, "get"):
        try:
            raw = cfg.get("leader_follower") or {}
            if isinstance(raw, dict):
                block = raw
        except Exception:
            block = {}
    if not bool(block.get("enabled", True)):
        return True, "leader_follower_off"

    b = float(bid or 0)
    o = float(offer or 0)
    ig_mid = (b + o) / 2.0 if b > 0 and o > b else 0.0

    # Refresh IG ring (no I/O). Proxy may already hold true futures ticks.
    observe_from_ig_quote(epic, bid, offer, cfg=cfg)

    leader = _leader_map(cfg).get(str(epic or "").strip()) or str(epic)
    lookback = int(block.get("lookback_ticks") or _DEFAULT_LOOKBACK)
    eps = float(block.get("momentum_eps") or _DEFAULT_EPS)
    streak_limit = int(block.get("divergence_streak_limit") or _DIVERGENCE_STREAK_LIMIT)
    veto_sec = float(block.get("divergence_veto_sec") or _DIVERGENCE_VETO_SEC)
    use_shadow = bool(block.get("shadow_proxy", True))
    # Divergence guard arms when a true high-velocity proxy feed is in use
    # (shadow_proxy=false) or when explicitly enabled in config.
    divergence_on = bool(block.get("divergence_guard", not use_shadow))

    # Proxy mid for divergence: last print on leader ring
    with _lock:
        pq = _mids.get(leader)
        proxy_mid = float(pq[-1][1]) if pq else ig_mid

    if divergence_on and ig_mid > 0 and proxy_mid > 0:
        ok_div, div_reason = _update_divergence(
            str(epic),
            ig_mid,
            proxy_mid,
            eps=eps,
            now=time.time(),
            streak_limit=streak_limit,
            veto_sec=veto_sec,
        )
        if not ok_div:
            return False, div_reason

    mom = micro_momentum(leader, lookback=lookback)
    if mom is None:
        return True, "leader_follower_warming"

    dir_u = str(direction or "BUY").upper()
    if dir_u == "BUY" and mom < -eps:
        return False, f"leader_follower_buy_veto mom={mom:.6f}"
    if dir_u == "SELL" and mom > eps:
        return False, f"leader_follower_sell_veto mom={mom:.6f}"
    return True, f"leader_follower_ok mom={mom:.6f}"
