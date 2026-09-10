#!/usr/bin/env python3
"""Static safety boundary checks for Host Agent Discovery."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_discovery_has_no_runtime_control_or_generic_write_path():
    app = (ROOT / "webapp" / "backend" / "app.py").read_text(encoding="utf-8")
    service = (ROOT / "webapp" / "backend" / "services" / "host_discovery.py").read_text(encoding="utf-8")
    config = (ROOT / "webapp" / "backend" / "config.py").read_text(encoding="utf-8")
    assert '"/up/api/host_discovery"' in app
    assert 'if request.method != "GET":' in app
    assert '"api/host_discovery"' not in config
    assert "PREREQUISITE_ALLOWLIST" in service
    assert "process_observation" in service
    assert '"active_runtime_owner": None' in service
    assert "subprocess" not in service
    assert "task scheduler" not in service.lower()
