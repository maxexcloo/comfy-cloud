from __future__ import annotations

from urllib.parse import quote

import httpx

from .control_config import ControlSettings, Provider
from .control_preferences import ControlPreferences
from .provider_deployment_common import (
    checked_request,
    configured_environment,
    deployment_asset,
    docker_flags,
    required_preference,
    response_json,
)
from .provider_deployment_runpod import (
    deploy_pod as deploy_runpod_pod,
)
from .provider_deployment_runpod import (
    deploy_serverless as deploy_runpod_serverless,
)
from .provider_deployment_runpod import (
    terminate as terminate_runpod,
)


async def deploy_provider(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
) -> httpx.Response:
    management = provider.management
    if management is None:
        raise RuntimeError("provider has no deployment management")
    if management.kind == "runpod-pod":
        return await deploy_runpod_pod(client, provider, preferences, settings)
    if management.kind == "runpod-serverless":
        return await deploy_runpod_serverless(client, provider, preferences, settings)
    if management.kind == "salad":
        return await deploy_salad(client, provider, preferences, settings)
    if management.kind == "vast-pod":
        return await deploy_vast_pod(client, provider, preferences, settings)
    if management.kind == "vast-serverless":
        return await deploy_vast_serverless(client, provider, preferences, settings)
    raise RuntimeError(f"unsupported standalone provider: {management.kind}")


async def terminate_provider(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    resource_id: str,
) -> httpx.Response:
    management = provider.management
    if management is None:
        raise RuntimeError("provider has no deployment management")
    if management.kind.startswith("runpod-"):
        return await terminate_runpod(client, provider, preferences, resource_id)
    if management.kind == "salad":
        return await checked_request(
            client,
            "DELETE",
            f"{salad_container_url(provider, preferences)}",
            headers=salad_headers(preferences),
        )
    if management.kind == "vast-pod":
        return await checked_request(
            client,
            "DELETE",
            f"https://console.vast.ai/api/v0/instances/{quote(resource_id, safe='')}/",
            headers=vast_headers(preferences),
        )
    if management.kind == "vast-serverless":
        groups = await checked_request(
            client,
            "GET",
            "https://console.vast.ai/api/v0/workergroups/",
            headers=vast_headers(preferences),
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
                headers=vast_headers(preferences),
            )
        return await checked_request(
            client,
            "DELETE",
            f"https://console.vast.ai/api/v0/endptjobs/{quote(resource_id, safe='')}/",
            headers=vast_headers(preferences),
        )
    raise RuntimeError(f"unsupported standalone provider: {management.kind}")


def salad_headers(preferences: ControlPreferences) -> dict[str, str]:
    return {"Salad-Api-Key": required_preference("SALAD_API_KEY", preferences)}


def salad_scope(provider: Provider, preferences: ControlPreferences) -> tuple[str, str]:
    management = provider.management
    if management is None:
        raise RuntimeError("provider has no SaladCloud management")
    return (
        management.organisation
        or required_preference("SALAD_ORGANISATION", preferences),
        management.project or required_preference("SALAD_PROJECT", preferences),
    )


def salad_container_url(provider: Provider, preferences: ControlPreferences) -> str:
    organisation, project = salad_scope(provider, preferences)
    management = provider.management
    assert management is not None
    return (
        "https://api.salad.com/api/public/organizations/"
        f"{quote(organisation, safe='')}/projects/{quote(project, safe='')}"
        f"/containers/{quote(management.name, safe='')}"
    )


async def salad_gpu_classes(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
) -> list[str]:
    if preferences.salad_gpu_classes:
        return preferences.salad_gpu_classes
    organisation, _ = salad_scope(provider, preferences)
    response = await checked_request(
        client,
        "GET",
        "https://api.salad.com/api/public/organizations/"
        f"{quote(organisation, safe='')}/gpu-classes",
        headers=salad_headers(preferences),
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
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
) -> httpx.Response:
    payload = deployment_asset("salad", "container-group.json")
    container = payload.get("container")
    if not isinstance(container, dict):
        raise TypeError("SaladCloud deployment asset has no container")
    container["image"] = preferences.worker_image
    container["environment_variables"] = configured_environment(
        container.get("environment_variables"), provider, preferences, settings
    )
    resources = container.get("resources")
    if not isinstance(resources, dict):
        raise TypeError("SaladCloud deployment asset has no resources")
    resources["gpu_classes"] = await salad_gpu_classes(client, provider, preferences)
    management = provider.management
    assert management is not None
    payload["name"] = management.name
    organisation, project = salad_scope(provider, preferences)
    return await checked_request(
        client,
        "POST",
        "https://api.salad.com/api/public/organizations/"
        f"{quote(organisation, safe='')}/projects/{quote(project, safe='')}/containers",
        headers=salad_headers(preferences),
        json=payload,
    )


def vast_headers(preferences: ControlPreferences) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {required_preference('VAST_API_KEY', preferences)}"
    }


async def deploy_vast_pod(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
) -> httpx.Response:
    specification = deployment_asset("vast", "pod.json")
    environment = configured_environment(
        specification.get("env"), provider, preferences, settings
    )
    search = await checked_request(
        client,
        "POST",
        "https://console.vast.ai/api/v0/bundles/",
        headers=vast_headers(preferences),
        json={
            "allocated_storage": specification.get("disk_space", 100),
            "direct_port_count": {"gte": 1},
            "gpu_ram": {"gte": preferences.vast_minimum_gpu_memory_gb * 1000},
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
        headers=vast_headers(preferences),
        json={
            "cancel_unavail": True,
            "disk": specification.get("disk_space", 100),
            "env": docker_flags(environment, management.port),
            "image": preferences.worker_image,
            "label": management.name,
            "target_state": "running",
        },
    )


async def deploy_vast_serverless(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
) -> httpx.Response:
    specification = deployment_asset("vast", "serverless.json")
    environment = configured_environment(
        specification.get("env"), provider, preferences, settings
    )
    management = provider.management
    assert management is not None
    image = preferences.worker_image
    ports = specification.get("ports")
    worker_port = (
        int(ports[0]) if isinstance(ports, list) and ports else management.port
    )
    template_response = await checked_request(
        client,
        "POST",
        "https://console.vast.ai/api/v0/template/",
        headers=vast_headers(preferences),
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
        headers=vast_headers(preferences),
        json={
            "cold_workers": 0,
            "endpoint_name": management.name,
            "inactivity_timeout": max(provider.idle_seconds or 60, 60),
            "max_workers": preferences.vast_maximum_workers,
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
        headers=vast_headers(preferences),
        json={
            "cold_workers": 0,
            "endpoint_id": endpoint_id,
            "endpoint_name": management.name,
            "gpu_ram": preferences.vast_minimum_gpu_memory_gb,
            "max_workers": preferences.vast_maximum_workers,
            "search_params": (
                "verified=true rentable=true rented=false num_gpus=1 "
                f"gpu_ram>={preferences.vast_minimum_gpu_memory_gb * 1000}"
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
