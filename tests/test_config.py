import pytest

from comfy_control.worker.config import Settings


def test_settings_require_api_key(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)

    with pytest.raises(ValueError, match="API_KEY"):
        Settings.from_env("serverless")


def test_pod_settings_require_ui_password(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("CONTROL_UI_PASSWORD", "REPLACE_ME")

    with pytest.raises(ValueError, match="CONTROL_UI_PASSWORD"):
        Settings.from_env("pod")


def test_worker_uses_control_ui_credentials(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("CONTROL_UI_PASSWORD", "control-password")
    monkeypatch.setenv("CONTROL_UI_USERNAME", "control-user")

    configured = Settings.from_env("pod")

    assert configured.ui_password == "control-password"
    assert configured.ui_username == "control-user"


@pytest.mark.parametrize("name", ["GENERATION_QUEUE_LIMIT", "MAXIMUM_REQUEST_MIB"])
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
