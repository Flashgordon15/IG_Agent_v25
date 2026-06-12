"""Snapshot store slow-enrichment cache — protects hot quote/WS path."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api import snapshot_store as store


class SnapshotSlowEnrichmentTests(unittest.TestCase):
    def setUp(self) -> None:
        store.reset_snapshot_store_for_tests()

    def tearDown(self) -> None:
        store.reset_snapshot_store_for_tests()

    def test_slow_enrichment_cached_across_reader_calls(self) -> None:
        calls = {"n": 0}

        def fake_shadow_metrics() -> dict:
            calls["n"] += 1
            return {"ok": True, "shadow": {}, "live": {}}

        tick = {"type": "tick", "bid": 1.0, "offer": 1.1}
        with patch(
            "system.shadow_analytics.shadow_vs_live_metrics",
            side_effect=fake_shadow_metrics,
        ):
            store._build_slow_enrichment_blob(force=True)
            out1 = store._tick_for_readers(dict(tick))
            out2 = store._tick_for_readers({**tick, "bid": 1.01})

        self.assertEqual(calls["n"], 1)
        self.assertIn("shadow_vs_live", out1.get("metrics") or {})
        self.assertIn("shadow_vs_live", out2.get("metrics") or {})

    def test_hub_quote_path_avoids_get_tick(self) -> None:
        with patch.object(store, "get_tick") as mock_get, patch.object(
            store, "publish_tick", return_value={}
        ) as mock_publish, patch.object(
            store, "_raw_tick_copy", return_value={"markets": {}}
        ):
            store.push_hub_quote_to_dashboard("EPIC.A", 100.0, 100.5, tick_age_s=0.0)
        mock_get.assert_not_called()
        mock_publish.assert_called_once()


if __name__ == "__main__":
    unittest.main()
