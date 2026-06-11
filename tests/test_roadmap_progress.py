"""Tests for roadmap progress API payload."""

from __future__ import annotations


def test_build_roadmap_progress_shape():
    from api.roadmap_progress import build_roadmap_progress

    payload = build_roadmap_progress(history_days=3, write_snapshot=False)
    assert payload.get("ok") is True
    assert "overall_pct" in payload
    assert isinstance(payload.get("sections"), list)
    assert len(payload["sections"]) >= 4
    for sec in payload["sections"]:
        assert "id" in sec
        assert "pct" in sec
        assert isinstance(sec.get("items"), list)
