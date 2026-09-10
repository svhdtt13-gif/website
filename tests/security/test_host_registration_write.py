#!/usr/bin/env python3
"""Static safety checks for configuration-only Host Registration writes."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_host_registration_has_guarded_configuration_only_boundary():
    app = (ROOT / "webapp" / "backend" / "app.py").read_text(encoding="utf-8")
    service = (ROOT / "webapp" / "backend" / "services" / "host_registration.py").read_text(encoding="utf-8")
    repository = (ROOT / "webapp" / "backend" / "repositories" / "portable_store.py").read_text(encoding="utf-8")
    index = (ROOT / "webapp" / "frontend" / "index.html").read_text(encoding="utf-8")
    assert '"/up/api/host_registration"' in app
    assert '"/up/api/host_binding"' in app
    assert "_portable_write_gate" in app
    assert 'request.headers.get("Authorization", "")' in app
    assert "ai_tool" not in service
    assert "subprocess" not in service
    assert ".post(" not in service
    assert "BEGIN IMMEDIATE" in repository
    assert "ensure_offline_binding" in repository
    assert '"state": "OFFLINE"' not in service
    assert "host_registration" not in index
    assert "host_binding" not in index
