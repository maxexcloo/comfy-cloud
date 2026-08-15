from __future__ import annotations

from urllib.parse import quote

import httpx

from catalogue.profiles import required_vram_gb
from control.config import ControlSettings, Provider
from control.preferences import ControlPreferences
from providers.deployment.common import (
    WORKER_COMMAND,
    WORKER_HEALTH_PORT,
    WORKER_INITIALISING_HEALTH_PATH,
    WORKER_LIVE_HEALTH_PATH,
    WORKER_READY_HEALTH_PATH,
    WORKER_STORAGE_GB,
    DeploymentSelection,
    checked_request,
    configured_environment,
    http_probe,
    required_preference,
    worker_model_profiles,
)


def headers(preferences: ControlPreferences) -> dict[str, str]:
    return {"Salad-Api-Key": required_preference("SALAD_API_KEY", preferences)}


def scope(provider: Provider, preferences: ControlPreferences) -> tuple[str, str]:
    management = provider.management
    if management is None:
        raise RuntimeError("provider has no SaladCloud management")
    return (
        management.organisation
        or required_preference("SALAD_ORGANISATION", preferences),
        management.project or required_preference("SALAD_PROJECT", preferences),
    )


def container_url(provider: Provider, preferences: ControlPreferences) -> str:
    organisation, project = scope(provider, preferences)
    management = provider.management
    assert management is not None
    return (
        "https://api.salad.com/api/public/organizations/"
        f"{quote(organisation, safe='')}/projects/{quote(project, safe='')}"
        f"/containers/{quote(management.name, safe='')}"
    )


async def gpu_classes(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    selection: DeploymentSelection | None = None,
) -> list[str]:
    if selection is not None and selection.option_id:
        return [selection.option_id]
    if preferences.salad_gpu_classes:
        return preferences.salad_gpu_classes
    organisation, _ = scope(provider, preferences)
    response = await checked_request(
        client,
        "GET",
        "https://api.salad.com/api/public/organizations/"
        f"{quote(organisation, safe='')}/gpu-classes",
        headers=headers(preferences),
    )
    payload = response.json()
    values = payload.get("items", []) if isinstance(payload, dict) else payload
    classes = (
        [item for item in values if isinstance(item, dict)]
        if isinstance(values, list)
        else []
    )
    preferred = ("l40s", "4090", "5090", "a6000", "a40")
    minimum_vram = required_vram_gb(worker_model_profiles(provider, preferences))
    matches = [
        str(item.get("id"))
        for item in classes
        if item.get("id")
        and any(name in str(item.get("name", "")).lower() for name in preferred)
        and (
            not isinstance(
                item.get("memory_gb") or item.get("gpu_memory_gb"), (float, int)
            )
            or float(item.get("memory_gb") or item.get("gpu_memory_gb")) >= minimum_vram
        )
    ]
    if not matches:
        raise RuntimeError(
            "SaladCloud has no suitable GPU class with at least "
            f"{minimum_vram} GB VRAM; set SALAD_GPU_CLASSES explicitly"
        )
    return matches


async def deployment_options(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
) -> list[dict[str, object]]:
    organisation, _ = scope(provider, preferences)
    response = await checked_request(
        client,
        "GET",
        "https://api.salad.com/api/public/organizations/"
        f"{quote(organisation, safe='')}/gpu-classes",
        headers=headers(preferences),
    )
    payload = response.json()
    values = payload.get("items", []) if isinstance(payload, dict) else payload
    return (
        [
            {
                "available": item.get("is_available", item.get("available", True)),
                "cost_per_hour": item.get("price") or item.get("price_per_hour"),
                "id": str(item.get("id")),
                "label": str(item.get("name") or item.get("id")),
                "memory_gb": item.get("memory_gb") or item.get("gpu_memory_gb"),
            }
            for item in values
            if isinstance(item, dict) and item.get("id")
        ]
        if isinstance(values, list)
        else []
    )


async def deploy(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
    *,
    selection: DeploymentSelection | None = None,
) -> httpx.Response:
    management = provider.management
    assert management is not None
    payload = {
        "autostart_policy": True,
        "display_name": management.name,
        "name": management.name,
        "replicas": 1,
        "restart_policy": "always",
        "container": {
            "command": list(WORKER_COMMAND),
            "environment_variables": configured_environment(
                {}, provider, preferences, settings
            ),
            "image": preferences.worker_image,
            "image_caching": True,
            "priority": "low",
            "resources": {
                "cpu": 4,
                "gpu_classes": await gpu_classes(
                    client, provider, preferences, selection
                ),
                "memory": 16_384,
                "storage_amount": WORKER_STORAGE_GB * 1024**3,
            },
        },
        "liveness_probe": http_probe(
            WORKER_LIVE_HEALTH_PATH,
            failure_threshold=3,
            period_seconds=30,
            timeout_seconds=5,
        ),
        "networking": {
            "auth": False,
            "port": WORKER_HEALTH_PORT,
            "protocol": "http",
        },
        "readiness_probe": http_probe(
            WORKER_READY_HEALTH_PATH,
            failure_threshold=3,
            period_seconds=5,
            timeout_seconds=3,
        ),
        "startup_probe": http_probe(
            WORKER_INITIALISING_HEALTH_PATH,
            failure_threshold=180,
            period_seconds=5,
            timeout_seconds=3,
        ),
    }
    organisation, project = scope(provider, preferences)
    return await checked_request(
        client,
        "POST",
        "https://api.salad.com/api/public/organizations/"
        f"{quote(organisation, safe='')}/projects/{quote(project, safe='')}/containers",
        headers=headers(preferences),
        json=payload,
    )


async def terminate(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    resource_id: str,
) -> httpx.Response:
    del resource_id
    return await checked_request(
        client,
        "DELETE",
        container_url(provider, preferences),
        headers=headers(preferences),
    )
