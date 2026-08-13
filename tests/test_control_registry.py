from comfy_control.control_registry import PROVIDER_CATALOGUE, control_file, routes


def environment() -> dict[str, str]:
    return {
        "CLIPROXY_API_KEY": "cliproxy-key",
        "CLIPROXY_MANAGEMENT_KEY": "management-key",
        "CLIPROXY_URL": "http://cliproxy",
        "MODAL_TOKEN_ID": "modal-id",
        "RUNPOD_API_KEY": "runpod-key",
        "SALAD_API_KEY": "salad-key",
        "VAST_API_KEY": "vast-key",
        "WORKER_API_KEY": "worker-key",
    }


def test_registry_contains_all_supported_providers_and_routes():
    configured = control_file(environment())
    provider_order = [
        "modal",
        "runpod-pod",
        "runpod",
        "salad",
        "vast-pod",
        "vast",
    ]

    assert [provider.id for provider in configured.providers] == [
        item["id"] for item in PROVIDER_CATALOGUE
    ]
    assert routes() == {
        "images": [
            {"model": "flux-2-klein-9b", "provider": provider}
            for provider in provider_order
        ]
        + [{"model": "grok-imagine", "provider": "cliproxyapi"}],
        "videos": [
            {"model": "minimax-h3", "provider": provider} for provider in provider_order
        ]
        + [{"model": "grok-imagine", "provider": "cliproxyapi"}],
    }


def test_registry_omits_unconfigured_providers_and_empty_routes():
    configured = control_file({"WORKER_API_KEY": "worker-key"})

    assert configured.providers == []
    assert configured.models == []
