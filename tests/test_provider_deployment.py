import json
from pathlib import Path

import httpx
import pytest

from comfy_control import provider_deployment
from comfy_control.control_config import (
    ControlSettings,
    Provider,
    ProviderManagement,
)
from comfy_control.provider_deployment import (
    configured_environment,
    deploy_provider,
    terminate_provider,
)

ROOT = Path(__file__).parents[1]


def settings(tmp_path: Path) -> ControlSettings:
    return ControlSettings(
        api_key="control-key",
        config_file=tmp_path / "control.yaml",
        database_path=tmp_path / "control.db",
        maximum_request_bytes=1024,
        ui_password="ui-password",
        ui_username="comfy",
    )


def provider(kind: str, name: str = "comfy-control") -> Provider:
    return Provider(
        id=kind,
        api_key="worker-key",
        management=ProviderManagement(kind=kind, name=name, function="serve"),
    )


@pytest.fixture(autouse=True)
def deployment_root(monkeypatch):
    monkeypatch.setattr(provider_deployment, "DEPLOYMENT_ROOT", ROOT / "deploy")


@pytest.mark.asyncio
async def test_deploys_runpod_pod_from_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "runpod-key")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/pods"
        assert request.headers["authorization"] == "Bearer runpod-key"
        payload = json.loads(request.content)
        assert payload["dockerStartCmd"] == []
        assert payload["env"]["API_KEY"] == "worker-key"
        assert payload["ports"] == ["8000/http"]
        return httpx.Response(201, json={"id": "pod-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await deploy_provider(
            client, provider("runpod-pod"), settings(tmp_path)
        )

    assert response.json()["id"] == "pod-1"


def test_worker_ui_credentials_fall_back_when_compose_values_are_empty(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("COMFY_UI_PASSWORD", "")
    monkeypatch.setenv("COMFY_UI_USERNAME", "")
    environment = configured_environment({}, provider("runpod-pod"), settings(tmp_path))

    assert environment["COMFY_UI_PASSWORD"] == "ui-password"
    assert environment["COMFY_UI_USERNAME"] == "comfy"


@pytest.mark.asyncio
async def test_deploys_runpod_serverless_template_and_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "runpod-key")
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.method == "GET":
            return httpx.Response(200, json=[])
        payload = json.loads(request.content)
        if request.url.path == "/v1/templates":
            assert payload["dockerStartCmd"] == ["comfy-control", "serverless"]
            assert payload["isServerless"] is True
            return httpx.Response(200, json={"id": "template-1"})
        assert payload["templateId"] == "template-1"
        assert payload["workersMin"] == 0
        return httpx.Response(201, json={"id": "endpoint-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await deploy_provider(
            client, provider("runpod-serverless"), settings(tmp_path)
        )

    assert requests == ["/v1/templates", "/v1/templates", "/v1/endpoints"]
    assert response.json()["id"] == "endpoint-1"


@pytest.mark.asyncio
async def test_deploys_salad_group_with_discovered_gpu(tmp_path, monkeypatch):
    monkeypatch.setenv("SALAD_API_KEY", "salad-key")
    monkeypatch.setenv("SALAD_ORGANISATION", "acme")
    monkeypatch.setenv("SALAD_PROJECT", "media")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/gpu-classes"):
            return httpx.Response(
                200, json={"items": [{"id": "gpu-1", "name": "NVIDIA L40S"}]}
            )
        payload = json.loads(request.content)
        assert payload["container"]["resources"]["gpu_classes"] == ["gpu-1"]
        assert payload["container"]["environment_variables"]["API_KEY"] == "worker-key"
        return httpx.Response(201, json={"id": "group-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await deploy_provider(client, provider("salad"), settings(tmp_path))

    assert response.json()["id"] == "group-1"


@pytest.mark.asyncio
async def test_deploys_and_terminates_vast_serverless(tmp_path, monkeypatch):
    monkeypatch.setenv("VAST_API_KEY", "vast-key")
    methods: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/api/v0/template/":
            payload = json.loads(request.content)
            assert "-p 9000:9000" in payload["env"]
            return httpx.Response(
                200, json={"template": {"hash_id": "hash-1", "id": 5}}
            )
        if request.method == "POST" and request.url.path == "/api/v0/endptjobs/":
            return httpx.Response(200, json={"result": 7})
        if request.method == "POST" and request.url.path == "/api/v0/workergroups/":
            return httpx.Response(200, json={"id": 9})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "results": [{"endpoint_id": 7, "endpoint_name": "comfy", "id": 9}]
                },
            )
        return httpx.Response(200, json={"success": True})

    managed = provider("vast-serverless", "comfy")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        deployed = await deploy_provider(client, managed, settings(tmp_path))
        terminated = await terminate_provider(client, managed, "7")

    assert deployed.json()["id"] == 7
    assert terminated.is_success
    assert ("DELETE", "/api/v0/workergroups/9/") in methods
    assert ("DELETE", "/api/v0/endptjobs/7/") in methods
