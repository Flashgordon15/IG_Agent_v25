"""Tests for daily digest API."""

from __future__ import annotations

from pathlib import Path


def test_load_daily_digest_returns_markdown(tmp_path, monkeypatch):
    from api import daily_digest as mod

    digest_dir = tmp_path / "docs" / "morning"
    digest_dir.mkdir(parents=True)
    day = "2099-01-15"
    body = f"# Daily Operator Digest — {day}\n\n## At a glance\n\n- **Agent running** | Yes |\n"
    (digest_dir / "DAILY_DIGEST_LATEST.md").write_text(body, encoding="utf-8")

    monkeypatch.setattr(mod, "_digest_dir", lambda: digest_dir)
    monkeypatch.setattr(mod, "_today_london", lambda: day)

    payload = mod.load_daily_digest(regenerate_if_stale=False)
    assert payload["day"] == day
    assert "Daily Operator Digest" in payload["markdown"]
    assert payload["source"] == "file"


def test_api_daily_digest_route(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from api import daily_digest as mod
    from api.server import create_app

    digest_dir = tmp_path / "docs" / "morning"
    digest_dir.mkdir(parents=True)
    day = "2099-06-01"
    (digest_dir / "DAILY_DIGEST_LATEST.md").write_text(
        f"# Daily Operator Digest — {day}\n\n*Generated*\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "_digest_dir", lambda: digest_dir)
    monkeypatch.setattr(mod, "_today_london", lambda: day)

    client = TestClient(create_app(watch_snapshot=False))
    resp = client.get("/api/daily-digest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["day"] == day
    assert "markdown" in data
