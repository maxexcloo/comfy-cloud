import httpx
import pytest

from comfy_control.control_config import Provider
from comfy_control.control_preferences import ControlPreferences
from comfy_control.provider_adapters import provider_adapter, provider_panel_url
from comfy_control.provider_runpod import RunPodServerlessAdapter


@pytest.mark.parametrize(
    ("kind", "resource", "state"),
    [
        ("runpod-pod", {"desiredStatus": "RUNNING"}, "running"),
        ("runpod", {"workers": [], "workersMin": 0}, "scaled-down"),
        ("runpod", {"workers": [], "workersMin": 1}, "starting"),
        (
            "runpod",
            {"workers": [{"desiredStatus": "EXITED"}], "workersMin": 1},
            "error",
        ),
        (
            "runpod",
            {"workers": [{"desiredStatus": "RUNNING"}], "workersMin": 1},
            "ready",
        ),
        ("salad", {"current_state": {"status": "RUNNING"}}, "running"),
        ("vast-pod", {"actual_status": "running"}, "running"),
        ("vast", {"endpoint_state": "ready"}, "ready"),
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


def test_runpod_status_exposes_counts_without_worker_secrets():
    resource = {
        "workers": [
            {
                "desiredStatus": "RUNNING",
                "env": {"WORKER_API_KEY": "must-not-leak"},
            }
        ],
        "workersMax": 2,
        "workersMin": 1,
    }

    state, details = RunPodServerlessAdapter("runpod").status(resource)

    assert state == "ready"
    assert details == {
        "workerStates": {"running": 1},
        "workersMax": 2,
        "workersMin": 1,
    }
    assert "must-not-leak" not in str(details)


@pytest.mark.asyncio
async def test_runpod_usage_reports_account_spend():
    request = None

    def respond(received: httpx.Request) -> httpx.Response:
        nonlocal request
        request = received
        return httpx.Response(
            200,
            json={"data": {"myself": {"currentSpendPerHr": 1.25, "spendLimit": 80}}},
        )

    provider = Provider.model_validate(
        {
            "api_key": "worker-key",
            "id": "runpod",
            "management": {
                "kind": "runpod",
                "name": "comfy-control",
            },
        }
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        metrics = await RunPodServerlessAdapter("runpod").usage(
            client,
            provider,
            ControlPreferences(runpod_api_key="runpod-key"),
            lambda _: {},
        )

    assert metrics == [
        {"label": "Current Spend", "unit": "USD/hour", "value": 1.25},
        {"label": "Spend Limit", "unit": "USD", "value": 80},
    ]
    assert request is not None
    assert request.headers["authorization"] == "Bearer runpod-key"


@pytest.mark.parametrize(
    ("kind", "module"),
    [
        ("modal", "comfy_control.provider_modal"),
        ("runpod-pod", "comfy_control.provider_runpod"),
        ("salad", "comfy_control.provider_salad"),
        ("vast-pod", "comfy_control.provider_vast"),
    ],
)
def test_provider_implementations_are_isolated_by_provider(kind, module):
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
    assert adapter.__class__.__module__ == module
