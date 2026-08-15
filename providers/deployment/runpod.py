from __future__ import annotations

from urllib.parse import quote

import httpx

from catalogue.profiles import profile_policy, required_vram_gb
from control.config import ControlSettings, Provider
from control.preferences import ControlPreferences
from providers.deployment.common import (
    WORKER_COMMAND,
    WORKER_HEALTH_PORT,
    WORKER_POD_CONTAINER_STORAGE_GB,
    WORKER_STORAGE_GB,
    DeploymentSelection,
    checked_request,
    configured_environment,
    required_preference,
    serverless_environment,
    worker_model_profiles,
)


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
                "query { gpuTypes { id displayName memoryInGb "
                "community: lowestPrice(input: { gpuCount: 1, secureCloud: false }) "
                "{ stockStatus uninterruptablePrice } "
                "secure: lowestPrice(input: { gpuCount: 1, secureCloud: true }) "
                "{ stockStatus uninterruptablePrice } } }"
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
        if cloud_variants and " MIG " in f" {identifier.upper()} ":
            continue
        common = {
            "label": str(item.get("displayName") or identifier),
            "memory_gb": item.get("memoryInGb"),
            "provider_option_id": identifier,
        }
        variants = []
        for cloud in ("Community", "Secure"):
            price = item.get(cloud.casefold())
            price = price if isinstance(price, dict) else {}
            cost = price.get("uninterruptablePrice")
            stock = price.get("stockStatus")
            available = (
                isinstance(cost, (float, int))
                and isinstance(stock, str)
                and stock.casefold() != "none"
            )
            variants.append(
                {
                    **common,
                    "availability": stock,
                    "available": available,
                    "cloud": cloud,
                    "cost_per_hour": cost,
                    "id": f"{cloud.casefold()}:{identifier}",
                    "variant": cloud.casefold(),
                }
            )
        if cloud_variants:
            options.extend(variants)
        else:
            available_variants = [
                variant for variant in variants if variant["available"]
            ]
            selected = min(
                available_variants or variants,
                key=lambda variant: (
                    not bool(variant["available"]),
                    variant["cost_per_hour"]
                    if isinstance(variant["cost_per_hour"], (float, int))
                    else float("inf"),
                ),
            )
            options.append(
                {
                    **common,
                    "availability": selected["availability"],
                    "available": selected["available"],
                    "cost_per_hour": selected["cost_per_hour"],
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
    payload: dict[str, object] = {
        "containerDiskInGb": WORKER_POD_CONTAINER_STORAGE_GB,
        "dockerEntrypoint": [],
        "dockerStartCmd": [],
        "env": configured_environment({}, provider, preferences, settings),
        "imageName": preferences.worker_image,
        "name": provider.management.name,  # type: ignore[union-attr]
        "ports": [f"{WORKER_HEALTH_PORT}/http"],
        "volumeInGb": WORKER_STORAGE_GB,
        "volumeMountPath": "/opt/ComfyUI/models",
    }
    if preferences.runpod_network_volume_id:
        payload["networkVolumeId"] = preferences.runpod_network_volume_id
        payload.pop("volumeInGb", None)
    if selection is not None and selection.option_id:
        gpu_type = selection.option_id
        if selection.variant:
            payload["cloudType"] = selection.variant.upper()
            prefix = f"{selection.variant.casefold()}:"
            if gpu_type.casefold().startswith(prefix):
                gpu_type = gpu_type[len(prefix) :]
        payload["gpuTypeIds"] = [gpu_type]
    elif gpu_types := environment.get("RUNPOD_GPU_TYPES"):
        payload["gpuTypeIds"] = [
            item.strip() for item in gpu_types.split(",") if item.strip()
        ]
    else:
        profiles = worker_model_profiles(provider, preferences)
        primary = next(
            (profile for profile in profiles if profile != "image-upscale"),
            "flux-2-klein-9b",
        )
        policy = profile_policy(primary)
        minimum_vram = required_vram_gb(profiles)
        compatible = [
            option
            for option in await deployment_options(
                client, preferences, cloud_variants=True
            )
            if option["available"]
            and isinstance(option["memory_gb"], (float, int))
            and option["memory_gb"] >= minimum_vram
            and (
                not isinstance(option["cost_per_hour"], (float, int))
                or option["cost_per_hour"] <= policy.hourly_cost_limit
            )
        ]
        if not compatible:
            raise RuntimeError(
                "RunPod has no available automatic GPU within the workload cost "
                f"limit of ${policy.hourly_cost_limit:.2f}/hour"
            )
        automatic = min(
            compatible,
            key=lambda option: (
                option["cost_per_hour"]
                if isinstance(option["cost_per_hour"], (float, int))
                else float("inf"),
                str(option["label"]).casefold(),
            ),
        )
        payload["cloudType"] = str(automatic["variant"]).upper()
        payload["gpuTypeIds"] = [str(automatic["provider_option_id"])]
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


async def replace_unavailable_pod(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
) -> httpx.Response:
    minimum_vram = required_vram_gb(worker_model_profiles(provider, preferences))
    configured = set(preferences.runpod_gpu_types)
    options = await deployment_options(client, preferences, cloud_variants=True)
    compatible = [
        option
        for option in options
        if option["available"]
        and isinstance(option["memory_gb"], (float, int))
        and option["memory_gb"] >= minimum_vram
        and (
            not configured
            or configured
            & {
                str(option["id"]),
                str(option["label"]),
                str(option["provider_option_id"]),
            }
        )
    ]
    if not compatible:
        raise RuntimeError(
            "RunPod has no available configured replacement GPU with at least "
            f"{minimum_vram} GB VRAM"
        )
    selected = min(
        compatible,
        key=lambda option: (
            option["cost_per_hour"]
            if isinstance(option["cost_per_hour"], (float, int))
            else float("inf"),
            str(option["label"]).casefold(),
            str(option["variant"]),
        ),
    )
    return await deploy_pod(
        client,
        provider,
        preferences,
        settings,
        selection=DeploymentSelection(
            memory_gb=float(selected["memory_gb"]),
            option_id=str(selected["id"]),
            variant=str(selected["variant"]),
        ),
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
    template: dict[str, object] = {
        "containerDiskInGb": WORKER_STORAGE_GB,
        "dockerEntrypoint": [],
        "dockerStartCmd": list(WORKER_COMMAND),
        "env": configured_environment(
            serverless_environment(), provider, preferences, settings
        ),
        "imageName": preferences.worker_image,
        "isPublic": False,
        "isServerless": True,
        "name": f"{provider.management.name}-template",  # type: ignore[union-attr]
        "ports": [f"{WORKER_HEALTH_PORT}/http"],
        "volumeInGb": 0,
    }
    if preferences.runpod_network_volume_id:
        environment_value = template["env"]
        assert isinstance(environment_value, dict)
        environment_value["MODELS_DIR"] = "/runpod-volume"
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
        for item in environment.get("RUNPOD_GPU_TYPES", "").split(",")
        if item.strip()
    ]
    if selection is not None and selection.option_id:
        gpu_types = [selection.option_id]
    minimum_vram = required_vram_gb(worker_model_profiles(provider, preferences))
    options = await deployment_options(client, preferences)
    matching_options = {
        str(key): option
        for option in options
        for key in (option["id"], option["label"])
    }
    if not gpu_types:
        profiles = worker_model_profiles(provider, preferences)
        primary = next(
            (profile for profile in profiles if profile != "image-upscale"),
            "flux-2-klein-9b",
        )
        policy = profile_policy(primary)
        compatible = [
            option
            for option in options
            if option["available"]
            and isinstance(option["memory_gb"], (int, float))
            and option["memory_gb"] >= minimum_vram
            and (
                not isinstance(option["cost_per_hour"], (int, float))
                or option["cost_per_hour"] <= policy.hourly_cost_limit
            )
        ]
        if compatible:
            selected = min(
                compatible,
                key=lambda option: (
                    option["cost_per_hour"]
                    if isinstance(option["cost_per_hour"], (int, float))
                    else float("inf"),
                    str(option["label"]).casefold(),
                ),
            )
            gpu_types = [str(selected["id"])]
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
    if preferences.runpod_network_volume_id:
        endpoint["networkVolumeId"] = preferences.runpod_network_volume_id
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
