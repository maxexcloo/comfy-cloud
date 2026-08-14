from __future__ import annotations

from urllib.parse import quote

import httpx

from .control_config import ControlSettings, Provider
from .control_preferences import ControlPreferences
from .provider_deployment_common import (
    DeploymentSelection,
    checked_request,
    configured_environment,
    deployment_asset,
    required_preference,
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
    resources["gpu_classes"] = await gpu_classes(
        client, provider, preferences, selection
    )
    management = provider.management
    assert management is not None
    payload["name"] = management.name
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
