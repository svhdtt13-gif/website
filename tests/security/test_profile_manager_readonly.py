#!/usr/bin/env python3
"""Static boundary checks for the Remote Profile Manager foundation."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_endpoint_is_read_only_and_not_in_generic_write_allowlist():
    app = (ROOT / "webapp" / "backend" / "app.py").read_text(encoding="utf-8")
    config = (ROOT / "webapp" / "backend" / "config.py").read_text(encoding="utf-8")
    service = (ROOT / "webapp" / "backend" / "services" / "profile_manager.py").read_text(encoding="utf-8")
    assert '"/up/api/profile_manager"' in app
    assert 'methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]' in app
    assert 'if request.method != "GET":' in app
    assert '"api/profile_manager"' not in config
    assert "sqlite3.connect(path.resolve().as_uri() + \"?mode=ro\", uri=True)" in service
    assert '"active_runtime_owner": None' in service
