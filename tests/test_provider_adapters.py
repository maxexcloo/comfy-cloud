import pytest

from comfy_control.control_config import Provider
from comfy_control.provider_adapters import provider_adapter, provider_panel_url


@pytest.mark.parametrize(
    ("kind", "resource", "state"),
    [
        ("runpod-pod", {"desiredStatus": "RUNNING"}, "running"),
        ("runpod-serverless", {"workers": {"ready": 0}}, "scaled-down"),
        ("runpod-serverless", {"workers": {"running": 1}}, "ready"),
        ("salad", {"current_state": {"status": "RUNNING"}}, "running"),
        ("vast-pod", {"actual_status": "running"}, "running"),
        ("vast-serverless", {"endpoint_state": "ready"}, "ready"),
    ],
)
def test_provider_adapters_normalise_state(kind, resource, state):
    provider = Provider.model_validate(
        {
            "api_key": "key",
            "id": "provider",
            "management": {
                "function": "serve" if kind == "modal" else None,
                "kind": kind,
                "name": "comfy-control",
            },
        }
    )

    adapter = provider_adapter(provider)

    assert adapter is not None
    assert adapter.status(resource)[0] == state


def test_proxy_panel_url_is_derived_without_an_adapter():
    provider = Provider(
        api_key="key",
        base_url="https://proxy.example",
        id="proxy",
        type="proxy",
    )

    assert provider_panel_url(provider, {}, provider.base_url) == (
        "https://proxy.example/management.html"
    )
