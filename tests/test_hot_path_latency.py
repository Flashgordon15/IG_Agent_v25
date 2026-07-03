"""Hot-path latency and packet validator tests."""

from __future__ import annotations

from system.latency_trace import (
    get_latency_trace_snapshot,
    record_pipeline_complete,
    record_stage,
    reset_latency_trace_for_tests,
)
from system.packet_validator import (
    REASON_OK,
    reset_packet_validator_for_tests,
    validate_quote_packet_fast,
)


def test_latency_trace_zero_alloc_ring():
    reset_latency_trace_for_tests()
    record_stage(epic="IX.D.DOW.IFM.IP", stage="feed_hub", mono_ts=1.0)
    record_stage(epic="IX.D.DOW.IFM.IP", stage="decision", mono_ts=1.005)
    record_stage(epic="IX.D.DOW.IFM.IP", stage="ig_rest", mono_ts=1.010)
    record_pipeline_complete(epic="IX.D.DOW.IFM.IP")
    snap = get_latency_trace_snapshot()
    assert snap["samples"] >= 1
    assert snap["p50_total_ms"] is not None


def test_validate_quote_packet_fast_codes():
    reset_packet_validator_for_tests()
    assert validate_quote_packet_fast(epic="E", bid=100.0, offer=100.5) == REASON_OK
    assert validate_quote_packet_fast(epic="", bid=1.0, offer=2.0) != REASON_OK
    assert validate_quote_packet_fast(epic="E", bid=100.0, offer=99.0) != REASON_OK
