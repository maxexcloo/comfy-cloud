from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .control_config import ControlSettings, Provider

DEPLOYMENT_ROOT = Path(
    os.getenv("CONTROL_DEPLOYMENT_ROOT", "/opt/comfy-control/deploy")
)
RUNPOD_ENDPOINT_GPUS = [
    "NVIDIA L40S",
    "NVIDIA RTX 6000 Ada Generation",
    "NVIDIA GeForce RTX 4090",
    "NVIDIA RTX A6000",
]


def required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required to manage this provider")
    return value


def deployment_asset(*parts: str) -> dict[str, Any]:
    path = DEPLOYMENT_ROOT.joinpath(*parts)
    if not path.is_file():
        raise RuntimeError(f"provider deployment asset was not found: {path}")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError(f"provider deployment asset is invalid: {path}")
    return value


def worker_environment(provider: Provider, settings: ControlSettings) -> dict[str, str]:
    environment = {
        "API_KEY": provider.api_key,
        "COMFY_UI_PASSWORD": os.getenv("COMFY_UI_PASSWORD", settings.ui_password),
        "COMFY_UI_USERNAME": os.getenv("COMFY_UI_USERNAME", settings.ui_username),
    }
    for name in (
        "CIVITAI_TOKEN",
        "HF_TOKEN",
        "MAXIMUM_PENDING_GENERATIONS",
        "MAXIMUM_REQUEST_BYTES",
        "MODEL_PROFILES",
        "PUBLIC_BASE_URL",
    ):
        if value := os.getenv(name):
            environment[name] = value
    return environment


def configured_environment(
    configured: object, provider: Provider, settings: ControlSettings
) -> dict[str, str]:
    if isinstance(configured, list):
        environment = {
            str(item["key"]): str(item["value"])
            for item in configured
            if isinstance(item, dict) and "key" in item and "value" in item
        }
    elif isinstance(configured, dict):
        environment = {str(key): str(value) for key, value in configured.items()}
    else:
        environment = {}
    environment.update(worker_environment(provider, settings))
    return environment


def docker_flags(environment: dict[str, str], port: int) -> str:
    flags = [
        f"-e {shlex.quote(f'{name}={value}')}"
        for name, value in sorted(environment.items())
    ]
    flags.append(f"-p {port}:{port}")
    return " ".join(flags)


def response_json(status_code: int, value: object) -> httpx.Response:
    return httpx.Response(status_code, json=value)


async def deploy_provider(
    client: httpx.AsyncClient,
    provider: Provider,
    settings: ControlSettings,
) -> httpx.Response:
    management = provider.management
    if management is None:
        raise RuntimeError("provider has no deployment management")
    if management.kind == "runpod-pod":
        return await deploy_runpod_pod(client, provider, settings)
    if management.kind == "runpod-serverless":
        return await deploy_runpod_serverless(client, provider, settings)
    if management.kind == "salad":
        return await deploy_salad(client, provider, settings)
    if management.kind == "vast-pod":
        return await deploy_vast_pod(client, provider, settings)
    if management.kind == "vast-serverless":
        return await deploy_vast_serverless(client, provider, settings)
    raise RuntimeError(f"unsupported standalone provider: {management.kind}")


async def terminate_provider(
    client: httpx.AsyncClient, provider: Provider, resource_id: str
) -> httpx.Response:
    management = provider.management
    if management is None:
        raise RuntimeError("provider has no deployment management")
    if management.kind.startswith("runpod-"):
        collection = "pods" if management.kind == "runpod-pod" else "endpoints"
        return await checked_request(
            client,
            "DELETE",
            f"https://rest.runpod.io/v1/{collection}/{quote(resource_id, safe='')}",
            headers=runpod_headers(),
        )
    if management.kind == "salad":
        return await checked_request(
            client,
            "DELETE",
            f"{salad_container_url(provider)}",
            headers=salad_headers(),
        )
    if management.kind == "vast-pod":
        return await checked_request(
            client,
            "DELETE",
            f"https://console.vast.ai/api/v0/instances/{quote(resource_id, safe='')}/",
            headers=vast_headers(),
        )
    if management.kind == "vast-serverless":
        groups = await checked_request(
            client,
            "GET",
            "https://console.vast.ai/api/v0/workergroups/",
            headers=vast_headers(),
        )
        payload = groups.json()
        for group in payload.get("results", []) if isinstance(payload, dict) else []:
            if not isinstance(group, dict):
                continue
            if (
                str(group.get("endpoint_id")) != resource_id
                and group.get("endpoint_name") != management.name
            ):
                continue
            await checked_request(
                client,
                "DELETE",
                "https://console.vast.ai/api/v0/workergroups/"
                f"{quote(str(group['id']), safe='')}/",
                headers=vast_headers(),
            )
        return await checked_request(
            client,
            "DELETE",
            f"https://console.vast.ai/api/v0/endptjobs/{quote(resource_id, safe='')}/",
            headers=vast_headers(),
        )
    raise RuntimeError(f"unsupported standalone provider: {management.kind}")


async def checked_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: object,
) -> httpx.Response:
    response = await client.request(method, url, **kwargs)
    response.raise_for_status()
    return response


def runpod_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {required_environment('RUNPOD_API_KEY')}"}


async def deploy_runpod_pod(
    client: httpx.AsyncClient, provider: Provider, settings: ControlSettings
) -> httpx.Response:
    payload = deployment_asset("runpod", "pod.json")
    docker_args = str(payload.pop("dockerArgs", "")).strip()
    payload["dockerEntrypoint"] = []
    payload["dockerStartCmd"] = shlex.split(docker_args) if docker_args else []
    payload["env"] = configured_environment(payload.get("env"), provider, settings)
    payload["name"] = provider.management.name  # type: ignore[union-attr]
    payload["imageName"] = os.getenv("WORKER_IMAGE", str(payload["imageName"]))
    if isinstance(payload.get("ports"), str):
        payload["ports"] = [
            item.strip() for item in payload["ports"].split(",") if item.strip()
        ]
    if gpu_types := os.getenv("RUNPOD_GPU_TYPES"):
        payload["gpuTypeIds"] = [
            item.strip() for item in gpu_types.split(",") if item.strip()
        ]
    if data_centres := os.getenv("RUNPOD_DATA_CENTRES"):
        payload["dataCenterIds"] = [
            item.strip() for item in data_centres.split(",") if item.strip()
        ]
    return await checked_request(
        client,
        "POST",
        "https://rest.runpod.io/v1/pods",
        headers=runpod_headers(),
        json=payload,
    )


async def deploy_runpod_serverless(
    client: httpx.AsyncClient, provider: Provider, settings: ControlSettings
) -> httpx.Response:
    template = deployment_asset("runpod", "serverless.json")
    docker_args = str(template.pop("dockerArgs", "")).strip()
    template.pop("endpointType", None)
    template["dockerEntrypoint"] = []
    template["dockerStartCmd"] = shlex.split(docker_args) if docker_args else []
    template["env"] = configured_environment(template.get("env"), provider, settings)
    template["imageName"] = os.getenv("WORKER_IMAGE", str(template["imageName"]))
    template["isPublic"] = False
    template["isServerless"] = True
    template["name"] = f"{provider.management.name}-template"  # type: ignore[union-attr]
    template["volumeInGb"] = 0
    if isinstance(template.get("ports"), str):
        template["ports"] = [
            item.strip() for item in template["ports"].split(",") if item.strip()
        ]
    templates_response = await checked_request(
        client,
        "GET",
        "https://rest.runpod.io/v1/templates",
        headers=runpod_headers(),
    )
    templates = templates_response.json()
    existing = (
        next(
            (
                item
                for item in templates
                if isinstance(item, dict) and item.get("name") == template["name"]
            ),
            None,
        )
        if isinstance(templates, list)
        else None
    )
    if existing is None:
        template_response = await checked_request(
            client,
            "POST",
            "https://rest.runpod.io/v1/templates",
            headers=runpod_headers(),
            json=template,
        )
        template_value = template_response.json()
    else:
        template_value = existing
    template_id = template_value.get("id") if isinstance(template_value, dict) else None
    if not template_id:
        raise RuntimeError("RunPod template response has no id")
    gpu_types = [
        item.strip()
        for item in os.getenv("RUNPOD_GPU_TYPES", ",".join(RUNPOD_ENDPOINT_GPUS)).split(
            ","
        )
        if item.strip()
    ]
    endpoint: dict[str, object] = {
        "endpointType": "load-balancer",
        "executionTimeoutMs": int(provider.request_timeout * 1000),
        "gpuCount": 1,
        "gpuTypeIds": gpu_types,
        "idleTimeout": min(max(provider.idle_seconds or 60, 1), 3600),
        "name": provider.management.name,  # type: ignore[union-attr]
        "scalerType": "QUEUE_DELAY",
        "scalerValue": 4,
        "templateId": str(template_id),
        "workersMax": int(os.getenv("RUNPOD_MAXIMUM_WORKERS", "1")),
        "workersMin": 0,
    }
    if data_centres := os.getenv("RUNPOD_DATA_CENTRES"):
        endpoint["dataCenterIds"] = [
            item.strip() for item in data_centres.split(",") if item.strip()
        ]
    return await checked_request(
        client,
        "POST",
        "https://rest.runpod.io/v1/endpoints",
        headers=runpod_headers(),
        json=endpoint,
    )


def salad_headers() -> dict[str, str]:
    return {"Salad-Api-Key": required_environment("SALAD_API_KEY")}


def salad_scope(provider: Provider) -> tuple[str, str]:
    management = provider.management
    if management is None:
        raise RuntimeError("provider has no SaladCloud management")
    return (
        management.organisation or required_environment("SALAD_ORGANISATION"),
        management.project or required_environment("SALAD_PROJECT"),
    )


def salad_container_url(provider: Provider) -> str:
    organisation, project = salad_scope(provider)
    management = provider.management
    assert management is not None
    return (
        "https://api.salad.com/api/public/organizations/"
        f"{quote(organisation, safe='')}/projects/{quote(project, safe='')}"
        f"/containers/{quote(management.name, safe='')}"
    )


async def salad_gpu_classes(client: httpx.AsyncClient, provider: Provider) -> list[str]:
    if configured := os.getenv("SALAD_GPU_CLASSES"):
        return [item.strip() for item in configured.split(",") if item.strip()]
    organisation, _ = salad_scope(provider)
    response = await checked_request(
        client,
        "GET",
        "https://api.salad.com/api/public/organizations/"
        f"{quote(organisation, safe='')}/gpu-classes",
        headers=salad_headers(),
    )
    payload = response.json()
    values = payload.get("items", []) if isinstance(payload, dict) else payload
    classes = (
        [item for item in values if isinstance(item, dict)]
        if isinstance(values, list)
        else []
    )
    preferred = ("l40s", "4090", "5090", "a6000", "a40")
    matches = [
        str(item.get("id"))
        for item in classes
        if item.get("id")
        and any(name in str(item.get("name", "")).lower() for name in preferred)
    ]
    if not matches:
        raise RuntimeError(
            "SaladCloud has no suitable GPU class; set SALAD_GPU_CLASSES explicitly"
        )
    return matches


async def deploy_salad(
    client: httpx.AsyncClient, provider: Provider, settings: ControlSettings
) -> httpx.Response:
    payload = deployment_asset("salad", "container-group.json")
    container = payload.get("container")
    if not isinstance(container, dict):
        raise TypeError("SaladCloud deployment asset has no container")
    container["image"] = os.getenv("WORKER_IMAGE", str(container["image"]))
    container["environment_variables"] = configured_environment(
        container.get("environment_variables"), provider, settings
    )
    resources = container.get("resources")
    if not isinstance(resources, dict):
        raise TypeError("SaladCloud deployment asset has no resources")
    resources["gpu_classes"] = await salad_gpu_classes(client, provider)
    management = provider.management
    assert management is not None
    payload["name"] = management.name
    organisation, project = salad_scope(provider)
    return await checked_request(
        client,
        "POST",
        "https://api.salad.com/api/public/organizations/"
        f"{quote(organisation, safe='')}/projects/{quote(project, safe='')}/containers",
        headers=salad_headers(),
        json=payload,
    )


def vast_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {required_environment('VAST_API_KEY')}"}


async def deploy_vast_pod(
    client: httpx.AsyncClient, provider: Provider, settings: ControlSettings
) -> httpx.Response:
    specification = deployment_asset("vast", "pod.json")
    environment = configured_environment(specification.get("env"), provider, settings)
    search = await checked_request(
        client,
        "POST",
        "https://console.vast.ai/api/v0/bundles/",
        headers=vast_headers(),
        json={
            "allocated_storage": specification.get("disk_space", 100),
            "direct_port_count": {"gte": 1},
            "gpu_ram": {"gte": int(os.getenv("VAST_MINIMUM_GPU_RAM_MB", "24000"))},
            "limit": 20,
            "num_gpus": {"eq": 1},
            "order": [["dph_total", "asc"]],
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "type": "ondemand",
            "verified": {"eq": True},
        },
    )
    payload = search.json()
    offers = payload.get("offers", []) if isinstance(payload, dict) else []
    if isinstance(offers, dict):
        offers = [offers]
    offer = next((item for item in offers if isinstance(item, dict)), None)
    if offer is None or not (
        offer_id := offer.get("id") or offer.get("ask_contract_id")
    ):
        raise RuntimeError("Vast.ai has no suitable rentable GPU offer")
    management = provider.management
    assert management is not None
    return await checked_request(
        client,
        "PUT",
        f"https://console.vast.ai/api/v0/asks/{quote(str(offer_id), safe='')}/",
        headers=vast_headers(),
        json={
            "cancel_unavail": True,
            "disk": specification.get("disk_space", 100),
            "env": docker_flags(environment, management.port),
            "image": os.getenv("WORKER_IMAGE", str(specification["image"])),
            "label": management.name,
            "target_state": "running",
        },
    )


async def deploy_vast_serverless(
    client: httpx.AsyncClient, provider: Provider, settings: ControlSettings
) -> httpx.Response:
    specification = deployment_asset("vast", "serverless.json")
    environment = configured_environment(specification.get("env"), provider, settings)
    management = provider.management
    assert management is not None
    image = os.getenv("WORKER_IMAGE", str(specification["image"]))
    ports = specification.get("ports")
    worker_port = (
        int(ports[0]) if isinstance(ports, list) and ports else management.port
    )
    template_response = await checked_request(
        client,
        "POST",
        "https://console.vast.ai/api/v0/template/",
        headers=vast_headers(),
        json={
            "args_str": "comfy-control vast-serverless",
            "docker_login_pass": "",
            "docker_login_repo": "",
            "docker_login_user": "",
            "env": docker_flags(environment, worker_port),
            "image": image,
            "name": f"{management.name}-template",
            "private": True,
            "recommended_disk_space": specification.get("disk_space", 100),
            "runtype": "args",
        },
    )
    template_payload = template_response.json()
    template = (
        template_payload.get("template") if isinstance(template_payload, dict) else None
    )
    if not isinstance(template, dict) or not template.get("hash_id"):
        raise RuntimeError("Vast.ai template response has no hash_id")
    endpoint = await checked_request(
        client,
        "POST",
        "https://console.vast.ai/api/v0/endptjobs/",
        headers=vast_headers(),
        json={
            "cold_workers": 0,
            "endpoint_name": management.name,
            "inactivity_timeout": max(provider.idle_seconds or 60, 60),
            "max_workers": int(os.getenv("VAST_MAXIMUM_WORKERS", "1")),
            "min_load": 0,
            "target_util": 0.9,
        },
    )
    endpoint_payload = endpoint.json()
    endpoint_id = (
        endpoint_payload.get("result") if isinstance(endpoint_payload, dict) else None
    )
    if endpoint_id is None:
        raise RuntimeError("Vast.ai endpoint response has no result")
    workergroup = await checked_request(
        client,
        "POST",
        "https://console.vast.ai/api/v0/workergroups/",
        headers=vast_headers(),
        json={
            "cold_workers": 0,
            "endpoint_id": endpoint_id,
            "endpoint_name": management.name,
            "gpu_ram": int(os.getenv("VAST_MINIMUM_GPU_RAM_GB", "24")),
            "max_workers": int(os.getenv("VAST_MAXIMUM_WORKERS", "1")),
            "search_params": (
                "verified=true rentable=true rented=false num_gpus=1 "
                f"gpu_ram>={int(os.getenv('VAST_MINIMUM_GPU_RAM_MB', '24000'))}"
            ),
            "template_hash": str(template["hash_id"]),
            "test_workers": 1,
        },
    )
    return response_json(
        201,
        {
            "endpoint": endpoint_payload,
            "id": endpoint_id,
            "template": {"hash_id": template["hash_id"], "id": template.get("id")},
            "workergroup": workergroup.json(),
        },
    )
