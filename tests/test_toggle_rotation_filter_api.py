from __future__ import annotations

from unittest.mock import MagicMock, patch

from api.routes import api_admin_toggle_rotation_filter


@patch("runtime.market_orchestrator.MarketOrchestrator.hot_reload_config")
@patch("system.config_loader.update_config_values")
@patch("system.config_loader.get_config")
def test_toggle_rotation_filter_flips_and_returns_state(
    mock_get_config: MagicMock,
    mock_update: MagicMock,
    _mock_reload: MagicMock,
) -> None:
    cfg = MagicMock()
    cfg.get.return_value = True
    mock_get_config.return_value = cfg

    updated = {"enforce_top3_rotation_filter": False}
    mock_update.return_value = updated

    out = api_admin_toggle_rotation_filter()

    mock_update.assert_called_once_with(enforce_top3_rotation_filter=False)
    assert out["success"] is True
    assert out["enforce_top3_rotation_filter"] is False
