from __future__ import annotations

import httpx

from comfy_control.catalogue.profiles import required_vram_gb
from comfy_control.control.config import ControlSettings, Provider
from comfy_control.control.preferences import ControlPreferences
from comfy_control.providers.deployment.common import (
    DeploymentSelection,
    worker_model_profiles,
)
from comfy_control.providers.deployment.runpod import (
    deploy_pod as deploy_runpod_pod,
)
from comfy_control.providers.deployment.runpod import (
    deploy_serverless as deploy_runpod_serverless,
)
from comfy_control.providers.deployment.runpod import (
    deployment_options as runpod_deployment_options,
)
from comfy_control.providers.deployment.runpod import (
    terminate as terminate_runpod,
)
from comfy_control.providers.deployment.salad import (
    deploy as deploy_salad,
)
from comfy_control.providers.deployment.salad import (
    deployment_options as salad_deployment_options,
)
from comfy_control.providers.deployment.salad import (
    terminate as terminate_salad,
)
from comfy_control.providers.deployment.vast import (
    deploy_pod as deploy_vast_pod,
)
from comfy_control.providers.deployment.vast import (
    deploy_serverless as deploy_vast_serverless,
)
from comfy_control.providers.deployment.vast import (
    deployment_options as vast_deployment_options,
)
from comfy_control.providers.deployment.vast import (
    terminate as terminate_vast,
)


async def deployment_options(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
) -> list[dict[str, object]]:
    management = provider.management
    if management is None:
        return []
    if management.kind == "modal":
        options = [
            {"available": True, "cost_per_hour": None, "id": gpu, "label": gpu}
            for gpu in ("A100", "H100", "L40S")
        ]
    elif management.kind in {"runpod", "runpod-pod"}:
        options = await runpod_deployment_options(
            client,
            preferences,
            cloud_variants=management.kind == "runpod-pod",
        )
    elif management.kind == "salad":
        options = await salad_deployment_options(client, provider, preferences)
    elif management.kind in {"vast", "vast-pod"}:
        options = await vast_deployment_options(client, provider, preferences)
    else:
        return []
    minimum_memory = required_vram_gb(worker_model_profiles(provider, preferences))
    prepared = []
    for option in options:
        memory = option.get("memory_gb")
        compatible = not isinstance(memory, (float, int)) or memory >= minimum_memory
        prepared.append(
            {
                **option,
                "compatible": compatible,
                "minimum_memory_gb": minimum_memory,
                "type": provider.type,
            }
        )
    return sorted(
        prepared,
        key=lambda option: (
            not bool(option.get("available")),
            not bool(option.get("compatible")),
            option.get("cost_per_hour")
            if isinstance(option.get("cost_per_hour"), (float, int))
            else float("inf"),
            str(option.get("label", "")).casefold(),
        ),
    )


async def deploy_provider(
    client: httpx.AsyncClient,
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
    *,
    selection: DeploymentSelection | None = None,
) -> httpx.Response:
    management = provider.management
    if management is None:
        raise RuntimeError("provider has no deployment management")
    if management.kind == "runpod-pod":
        return await deploy_runpod_pod(
            client, provider, preferences, settings, selection=selection
        )
    if management.kind == "runpod":
        return await deploy_runpod_serverless(
            client, provider, preferences, settings, selection=selection
        )
    if management.kind == "salad":
        return await deploy_salad(
            client, provider, preferences, settings, selection=selection
        )
    if management.kind == "vast-pod":
        return await deploy_vast_pod(
            client, provider, preferences, settings, selection=selection
        )
    if management.kind == "vast":
        return await deploy_vast_serverless(
            client, provider, preferences, settings, selection=selection
        )
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
    if management.kind in {"runpod", "runpod-pod"}:
        return await terminate_runpod(client, provider, preferences, resource_id)
    if management.kind == "salad":
        return await terminate_salad(client, provider, preferences, resource_id)
    if management.kind in {"vast", "vast-pod"}:
        return await terminate_vast(client, provider, preferences, resource_id)
    raise RuntimeError(f"unsupported standalone provider: {management.kind}")
