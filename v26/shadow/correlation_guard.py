"""Shadow-only: block correlated same-direction intents across FX epics."""

from __future__ import annotations

from strategies.base import ShadowIntent

FX_EPICS = frozenset(
    {
        "CS.D.EURUSD.CFD.IP",
        "CS.D.GBPUSD.CFD.IP",
    }
)

_recent: list[tuple[str, str, str, str]] = []  # epic, direction, strategy_id, session
_MAX_SAME_DIR_FX = 1


def reset_shadow_correlation_for_tests() -> None:
    _recent.clear()


def apply_shadow_correlation_guard(intent: ShadowIntent) -> ShadowIntent:
    if not intent.would_trade or intent.direction not in ("BUY", "SELL"):
        return intent
    if intent.epic not in FX_EPICS:
        return intent

    same = [
        r
        for r in _recent
        if r[1] == intent.direction
        and r[2] != intent.strategy_id
        and r[0] in FX_EPICS
        and r[3] == intent.session
    ]
    if len(same) >= _MAX_SAME_DIR_FX:
        intent.would_trade = False
        intent.reason = (
            f"{intent.reason} | shadow corr: {intent.direction} already on "
            f"{same[0][0]} via {same[0][2]}"
        ).strip(" |")
        intent.payload = {**intent.payload, "correlation_blocked": True}
        return intent

    _recent.append((intent.epic, intent.direction, intent.strategy_id, intent.session))
    if len(_recent) > 200:
        del _recent[:-100]
    return intent
