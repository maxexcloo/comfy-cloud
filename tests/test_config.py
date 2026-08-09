import pytest

from comfy_control.config import Settings


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


@pytest.mark.parametrize(
    "name", ["MAXIMUM_PENDING_GENERATIONS", "MAXIMUM_REQUEST_BYTES"]
)
def test_settings_reject_non_positive_limits(monkeypatch, name):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("MODE", "serverless")
    monkeypatch.setenv(name, "0")

    with pytest.raises(ValueError, match=name):
        Settings.from_env()
