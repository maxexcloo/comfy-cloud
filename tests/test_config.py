import pytest

from comfy_cloud.config import Settings


def test_settings_require_api_key(monkeypatch):
    monkeypatch.setenv("MODE", "serverless")
    monkeypatch.delenv("API_KEY", raising=False)

    with pytest.raises(ValueError, match="API_KEY"):
        Settings.from_env()


def test_pod_settings_require_ui_password(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("MODE", "pod")
    monkeypatch.setenv("COMFY_UI_PASSWORD", "REPLACE_ME")

    with pytest.raises(ValueError, match="COMFY_UI_PASSWORD"):
        Settings.from_env()
