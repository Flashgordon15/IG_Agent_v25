"""Provenance guards for ML score and hold stamps.

Regression cover for the 2026-07-24 autopsy corruption: a shared per-epic ML
snapshot masquerading as per-trade inference, unbounded probabilities, and
sync-collapsed holds being read as zero-second scalps.
"""

from __future__ import annotations

import unittest

from diagnostics.stamp_provenance import (
    EXIT_AUTHORITY_AGENT,
    EXIT_AUTHORITY_BROKER,
    EXIT_AUTHORITY_OPERATOR,
    EXIT_AUTHORITY_UNKNOWN,
    HOLD_SOURCE_SYNC_ARTIFACT,
    HOLD_SOURCE_TRACKED,
    ML_SOURCE_ABSENT,
    ML_SOURCE_EPIC_SNAPSHOT,
    ML_SOURCE_MODEL,
    ML_SOURCE_THRESHOLD_CONSTANT,
    clamp_probability,
    classify_exit_authority,
    classify_hold,
    classify_ml_score,
    hold_is_measurable,
    is_threshold_constant,
    ml_score_is_usable,
)


class ClampProbabilityTests(unittest.TestCase):
    def test_in_range_passes_through(self) -> None:
        self.assertEqual(clamp_probability(0.73), (0.73, False))

    def test_above_one_is_clamped_and_flagged(self) -> None:
        # Two Gold rows on 2026-07-24 were stamped 1.10074.
        self.assertEqual(clamp_probability(1.10074), (1.0, True))

    def test_below_zero_is_clamped_and_flagged(self) -> None:
        self.assertEqual(clamp_probability(-0.2), (0.0, True))

    def test_percent_style_stamp_is_clamped(self) -> None:
        self.assertEqual(clamp_probability(68.0), (1.0, True))

    def test_non_numeric_is_absent_not_repaired(self) -> None:
        self.assertEqual(clamp_probability(None), (None, False))
        self.assertEqual(clamp_probability("abc"), (None, False))

    def test_nan_is_absent(self) -> None:
        self.assertEqual(clamp_probability(float("nan")), (None, False))


class ThresholdConstantTests(unittest.TestCase):
    def test_sniper_threshold_detected(self) -> None:
        self.assertTrue(is_threshold_constant(0.68))

    def test_nearby_model_output_not_flagged(self) -> None:
        self.assertFalse(is_threshold_constant(0.6801))
        self.assertFalse(is_threshold_constant(0.7773))


class ClassifyMlScoreTests(unittest.TestCase):
    def test_model_score_is_trusted(self) -> None:
        out = classify_ml_score(0.7773, source=ML_SOURCE_MODEL)
        self.assertEqual(out["ml_score_at_entry"], 0.7773)
        self.assertEqual(out["ml_score_source"], ML_SOURCE_MODEL)
        self.assertTrue(out["ml_score_trusted"])

    def test_threshold_value_is_downgraded_even_if_caller_claims_model(self) -> None:
        out = classify_ml_score(0.68, source=ML_SOURCE_MODEL)
        self.assertEqual(out["ml_score_source"], ML_SOURCE_THRESHOLD_CONSTANT)
        self.assertFalse(out["ml_score_trusted"])
        # Must not stamp the gate default as if it were an inference.
        self.assertIsNone(out["ml_score_at_entry"])
        self.assertEqual(out.get("ml_score_threshold_rejected"), 0.68)

    def test_epic_snapshot_fallback_is_not_trusted(self) -> None:
        out = classify_ml_score(0.7123, source=ML_SOURCE_EPIC_SNAPSHOT)
        self.assertEqual(out["ml_score_source"], ML_SOURCE_EPIC_SNAPSHOT)
        self.assertFalse(out["ml_score_trusted"])

    def test_out_of_bounds_is_clamped_and_untrusted(self) -> None:
        out = classify_ml_score(1.10074, source=ML_SOURCE_MODEL)
        self.assertEqual(out["ml_score_at_entry"], 1.0)
        self.assertTrue(out["ml_score_out_of_bounds"])
        self.assertFalse(out["ml_score_trusted"])

    def test_absent_score(self) -> None:
        out = classify_ml_score(None)
        self.assertIsNone(out["ml_score_at_entry"])
        self.assertEqual(out["ml_score_source"], ML_SOURCE_ABSENT)
        self.assertFalse(out["ml_score_trusted"])


class ExitAuthorityTests(unittest.TestCase):
    def test_broker_attached_is_broker(self) -> None:
        self.assertEqual(
            classify_exit_authority(
                exit_reason="broker_attached", engine_origin="broker_attached"
            ),
            EXIT_AUTHORITY_BROKER,
        )

    def test_ig_transaction_sync_is_broker(self) -> None:
        self.assertEqual(
            classify_exit_authority(exit_reason="ig_transaction_sync"),
            EXIT_AUTHORITY_BROKER,
        )

    def test_soft_loss_is_agent(self) -> None:
        self.assertEqual(
            classify_exit_authority(
                exit_reason="micro_gbp_exit:soft_loss pnl=-3.20 soft=-2.95",
                engine_origin="MACRO_SENTINEL",
            ),
            EXIT_AUTHORITY_AGENT,
        )

    def test_virtual_stop_is_agent(self) -> None:
        self.assertEqual(
            classify_exit_authority(exit_reason="virtual_stop:virtual_stop_ceiling"),
            EXIT_AUTHORITY_AGENT,
        )

    def test_agent_token_wins_over_generic_broker_substring(self) -> None:
        self.assertEqual(
            classify_exit_authority(
                exit_reason="soft_loss", engine_origin="broker_attached"
            ),
            EXIT_AUTHORITY_AGENT,
        )

    def test_operator_flatten_is_operator(self) -> None:
        self.assertEqual(
            classify_exit_authority(exit_reason="operator_flatten_all"),
            EXIT_AUTHORITY_OPERATOR,
        )

    def test_blank_is_unknown(self) -> None:
        self.assertEqual(classify_exit_authority(), EXIT_AUTHORITY_UNKNOWN)


class ClassifyHoldTests(unittest.TestCase):
    def test_zero_hold_on_broker_exit_is_sync_artifact(self) -> None:
        out = classify_hold(
            0.0, exit_reason="broker_attached", engine_origin="broker_attached"
        )
        self.assertEqual(out["hold_sec_source"], HOLD_SOURCE_SYNC_ARTIFACT)
        self.assertFalse(out["hold_sec_trusted"])

    def test_zero_hold_on_agent_exit_is_kept_as_measured(self) -> None:
        out = classify_hold(
            0.0,
            exit_reason="soft_loss",
            engine_origin="QUANT_SNIPER",
            source=HOLD_SOURCE_TRACKED,
        )
        self.assertTrue(out["hold_sec_trusted"])

    def test_tracked_hold_is_trusted(self) -> None:
        out = classify_hold(
            180.0,
            exit_reason="trail",
            engine_origin="MACRO_SENTINEL",
            source=HOLD_SOURCE_TRACKED,
        )
        self.assertEqual(out["hold_sec"], 180.0)
        self.assertTrue(out["hold_sec_trusted"])

    def test_absent_hold(self) -> None:
        out = classify_hold(None)
        self.assertIsNone(out["hold_sec"])
        self.assertFalse(out["hold_sec_trusted"])


class LegacyRowUsabilityTests(unittest.TestCase):
    """Rows written before provenance existed must still be judged correctly."""

    def test_legacy_zero_hold_broker_row_is_not_measurable(self) -> None:
        row = {
            "hold_sec": 0.0,
            "exit_reason": "broker_attached",
            "engine_origin": "broker_attached",
        }
        self.assertFalse(hold_is_measurable(row))

    def test_legacy_real_hold_is_measurable(self) -> None:
        row = {"hold_sec": 240.0, "exit_reason": "trail", "engine_origin": "MACRO_SENTINEL"}
        self.assertTrue(hold_is_measurable(row))

    def test_legacy_threshold_ml_stamp_is_unusable(self) -> None:
        self.assertFalse(ml_score_is_usable({"ml_score_at_entry": 0.68}))

    def test_legacy_out_of_bounds_ml_stamp_is_unusable(self) -> None:
        self.assertFalse(ml_score_is_usable({"ml_score_at_entry": 1.10074}))

    def test_legacy_ordinary_ml_stamp_is_usable(self) -> None:
        self.assertTrue(ml_score_is_usable({"ml_score_at_entry": 0.7773}))

    def test_explicit_source_overrides_legacy_heuristic(self) -> None:
        row = {"ml_score_at_entry": 0.7773, "ml_score_source": ML_SOURCE_EPIC_SNAPSHOT}
        self.assertFalse(ml_score_is_usable(row))


if __name__ == "__main__":
    unittest.main()
