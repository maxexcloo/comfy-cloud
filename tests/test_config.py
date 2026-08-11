import pytest

from comfy_control.config import Settings


def test_settings_require_api_key(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)

    with pytest.raises(ValueError, match="API_KEY"):
        Settings.from_env("serverless")


def test_pod_settings_require_ui_password(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("COMFY_UI_PASSWORD", "REPLACE_ME")

    with pytest.raises(ValueError, match="COMFY_UI_PASSWORD"):
        Settings.from_env("pod")


@pytest.mark.parametrize(
    "name", ["MAXIMUM_PENDING_GENERATIONS", "MAXIMUM_REQUEST_BYTES"]
)
def test_settings_reject_non_positive_limits(monkeypatch, name):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv(name, "0")

    with pytest.raises(ValueError, match=name):
        Settings.from_env("serverless")


def test_mode_environment_does_not_select_runtime(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("MODE", "pod")

    settings = Settings.from_env("serverless")

    assert settings.deployment_type == "serverless"


def test_cliproxy_fallback_requires_url_and_key_together(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("CLIPROXY_API_KEY", "proxy-key")
    monkeypatch.delenv("CLIPROXY_URL", raising=False)

    with pytest.raises(ValueError, match="must either both be set"):
        Settings.from_env("serverless")
