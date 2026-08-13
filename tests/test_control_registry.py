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

    assert [provider.id for provider in configured.providers] == [
        item["id"] for item in PROVIDER_CATALOGUE
    ]
    assert routes() == {
        "images": [target.provider for target in configured.models[0].targets],
        "videos": [target.provider for target in configured.models[2].targets],
    }


def test_registry_omits_unconfigured_providers_and_empty_routes():
    configured = control_file({"WORKER_API_KEY": "worker-key"})

    assert configured.providers == []
    assert configured.models == []
