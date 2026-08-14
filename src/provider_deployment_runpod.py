from __future__ import annotations

import shlex
from urllib.parse import quote

import httpx

from .control_config import ControlSettings, Provider
from .control_preferences import ControlPreferences
from .model_profiles import required_vram_gb
from .provider_deployment_common import (
    DeploymentSelection,
    checked_request,
    configured_environment,
    deployment_asset,
    required_preference,
)

ENDPOINT_GPUS = [
    "NVIDIA L40S",
    "NVIDIA RTX 6000 Ada Generation",
    "NVIDIA GeForce RTX 4090",
    "NVIDIA RTX A6000",
]


def headers(preferences: ControlPreferences) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {required_preference('RUNPOD_API_KEY', preferences)}"
    }


async def deployment_options(
    client: httpx.AsyncClient,
    preferences: ControlPreferences,
    *,
    cloud_variants: bool = False,
) -> list[dict[str, object]]:
    response = await checked_request(
        client,
        "POST",
        "https://api.runpod.io/graphql",
        headers=headers(preferences),
        json={
            "query": (
                "query { gpuTypes { id displayName memoryInGb secureCloud "
                "communityCloud securePrice communityPrice } }"
            )
        },
    )
    payload = response.json()
    if isinstance(payload, dict) and payload.get("errors"):
        raise RuntimeError("RunPod GPU catalogue returned GraphQL errors")
    data = payload.get("data") if isinstance(payload, dict) else None
    values = data.get("gpuTypes", []) if isinstance(data, dict) else []
    options = []
    for item in values:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        identifier = str(item["id"])
        common = {
            "label": str(item.get("displayName") or identifier),
            "memory_gb": item.get("memoryInGb"),
            "provider_option_id": identifier,
        }
        if cloud_variants:
            for cloud, available_key, price_key in (
                ("Community", "communityCloud", "communityPrice"),
                ("Secure", "secureCloud", "securePrice"),
            ):
                if not item.get(available_key):
                    continue
                options.append(
                    {
                        **common,
                        "available": True,
                        "cloud": cloud,
                        "cost_per_hour": item.get(price_key),
                        "id": f"{cloud.casefold()}:{identifier}",
                        "variant": cloud.casefold(),
                    }
                )
        else:
            options.append(
                {
                    **common,
                    "available": bool(
                        item.get("secureCloud") or item.get("communityCloud")
                    ),
                    "cost_per_hour": item.get("securePrice")
                    or item.get("communityPrice"),
                    "id": identifier,
                }
            )
    return options


async def deploy_pod(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
    *,
    selection: DeploymentSelection | None = None,
) -> httpx.Response:
    environment = preferences.environment()
    payload = deployment_asset("runpod", "pod.json")
    docker_args = str(payload.pop("dockerArgs", "")).strip()
    payload["dockerEntrypoint"] = []
    payload["dockerStartCmd"] = shlex.split(docker_args) if docker_args else []
    payload["env"] = configured_environment(
        payload.get("env"), provider, preferences, settings
    )
    payload["name"] = provider.management.name  # type: ignore[union-attr]
    payload["imageName"] = preferences.worker_image
    if isinstance(payload.get("ports"), str):
        payload["ports"] = [
            item.strip() for item in payload["ports"].split(",") if item.strip()
        ]
    if selection is not None and selection.option_id:
        payload["gpuTypeIds"] = [selection.option_id]
        if selection.variant:
            payload["cloudType"] = selection.variant.upper()
    elif gpu_types := environment.get("RUNPOD_GPU_TYPES"):
        payload["gpuTypeIds"] = [
            item.strip() for item in gpu_types.split(",") if item.strip()
        ]
    if data_centres := environment.get("RUNPOD_DATA_CENTRES"):
        payload["dataCenterIds"] = [
            item.strip() for item in data_centres.split(",") if item.strip()
        ]
    payload["allowedCudaVersions"] = ["13.0"]
    payload["computeType"] = "GPU"
    return await checked_request(
        client,
        "POST",
        "https://rest.runpod.io/v1/pods",
        headers=headers(preferences),
        json=payload,
    )


async def deploy_serverless(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
    *,
    selection: DeploymentSelection | None = None,
) -> httpx.Response:
    environment = preferences.environment()
    template = deployment_asset("runpod", "serverless.json")
    docker_args = str(template.pop("dockerArgs", "")).strip()
    template.pop("endpointType", None)
    template["dockerEntrypoint"] = []
    template["dockerStartCmd"] = shlex.split(docker_args) if docker_args else []
    template["env"] = configured_environment(
        template.get("env"), provider, preferences, settings
    )
    template["imageName"] = preferences.worker_image
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
        headers=headers(preferences),
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
            headers=headers(preferences),
            json=template,
        )
        template_value = template_response.json()
    else:
        template_id = existing.get("id")
        if not template_id:
            raise RuntimeError("RunPod template response has no id")
        update = dict(template)
        update.pop("isServerless", None)
        template_response = await checked_request(
            client,
            "POST",
            f"https://rest.runpod.io/v1/templates/{quote(str(template_id), safe='')}/update",
            headers=headers(preferences),
            json=update,
        )
        template_value = template_response.json()
    template_id = template_value.get("id") if isinstance(template_value, dict) else None
    if not template_id:
        raise RuntimeError("RunPod template response has no id")
    gpu_types = [
        item.strip()
        for item in environment.get("RUNPOD_GPU_TYPES", ",".join(ENDPOINT_GPUS)).split(
            ","
        )
        if item.strip()
    ]
    if selection is not None and selection.option_id:
        gpu_types = [selection.option_id]
    minimum_vram = required_vram_gb(preferences.model_profiles)
    options = await deployment_options(client, preferences)
    matching_options = {
        str(key): option
        for option in options
        for key in (option["id"], option["label"])
    }
    gpu_types = [
        str(matching_options[gpu]["id"])
        for gpu in gpu_types
        if gpu in matching_options
        and matching_options[gpu]["available"]
        and isinstance(matching_options[gpu]["memory_gb"], (int, float))
        and matching_options[gpu]["memory_gb"] >= minimum_vram
    ]
    if not gpu_types:
        raise RuntimeError(
            "RunPod has no available configured GPU with at least "
            f"{minimum_vram} GB VRAM"
        )
    endpoint: dict[str, object] = {
        "executionTimeoutMs": int(provider.request_timeout * 1000),
        "gpuCount": 1,
        "gpuTypeIds": gpu_types,
        "idleTimeout": min(max(provider.idle_seconds or 60, 1), 3600),
        "minCudaVersion": "13.0",
        "name": provider.management.name,  # type: ignore[union-attr]
        "scalerType": "QUEUE_DELAY",
        "scalerValue": 4,
        "templateId": str(template_id),
        "workersMax": preferences.runpod_maximum_workers,
        "workersMin": 0,
    }
    if data_centres := environment.get("RUNPOD_DATA_CENTRES"):
        endpoint["dataCenterIds"] = [
            item.strip() for item in data_centres.split(",") if item.strip()
        ]
    return await checked_request(
        client,
        "POST",
        "https://rest.runpod.io/v1/endpoints",
        headers=headers(preferences),
        json=endpoint,
    )


async def terminate(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    resource_id: str,
) -> httpx.Response:
    management = provider.management
    assert management is not None
    collection = "pods" if management.kind == "runpod-pod" else "endpoints"
    return await checked_request(
        client,
        "DELETE",
        f"https://rest.runpod.io/v1/{collection}/{quote(resource_id, safe='')}",
        headers=headers(preferences),
    )
