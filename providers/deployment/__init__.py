from __future__ import annotations

import httpx

from catalogue.profiles import profile_policy, required_vram_gb
from control.config import ControlSettings, Provider
from control.preferences import ControlPreferences
from providers.capacity import (
    CapacityOffer,
    baseline_p95_seconds,
    rank_capacity,
)
from providers.deployment.common import (
    DeploymentSelection,
    worker_model_profiles,
)
from providers.deployment.runpod import (
    deploy_pod as deploy_runpod_pod,
)
from providers.deployment.runpod import (
    deploy_serverless as deploy_runpod_serverless,
)
from providers.deployment.runpod import (
    deployment_options as runpod_deployment_options,
)
from providers.deployment.runpod import (
    terminate as terminate_runpod,
)
from providers.deployment.salad import (
    deploy as deploy_salad,
)
from providers.deployment.salad import (
    deployment_options as salad_deployment_options,
)
from providers.deployment.salad import (
    terminate as terminate_salad,
)
from providers.deployment.vast import (
    deploy_pod as deploy_vast_pod,
)
from providers.deployment.vast import (
    deploy_serverless as deploy_vast_serverless,
)
from providers.deployment.vast import (
    deployment_options as vast_deployment_options,
)
from providers.deployment.vast import (
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
    profiles = worker_model_profiles(provider, preferences)
    primary_profile = next(
        (profile for profile in profiles if profile != "image-upscale"),
        "flux-2-klein-9b",
    )
    selected_workload = profile_policy(primary_profile)
    minimum_memory = required_vram_gb(profiles)
    prepared = []
    for option in options:
        memory = option.get("memory_gb")
        compatible = not isinstance(memory, (float, int)) or memory >= minimum_memory
        label = str(option.get("label", option.get("id", "Capacity")))
        cost = option.get("cost_per_hour")
        reliability = option.get("reliability")
        offer = CapacityOffer(
            available=bool(option.get("available", True)),
            benchmarked_p95_seconds=baseline_p95_seconds(
                selected_workload, provider.id, label
            ),
            cost_per_hour=(float(cost) if isinstance(cost, (float, int)) else None),
            id=str(option.get("id", "automatic")),
            label=label,
            location=(str(option["location"]) if option.get("location") else None),
            memory_gb=(float(memory) if isinstance(memory, (float, int)) else None),
            mode=provider.type,
            provider=provider.id,
            reliability=(
                float(reliability) if isinstance(reliability, (float, int)) else None
            ),
        )
        prepared.append(
            {
                **option,
                "benchmarked_p95_seconds": offer.benchmarked_p95_seconds,
                "compatible": compatible,
                "experimental": not offer.proven(),
                "minimum_memory_gb": minimum_memory,
                "proven": offer.proven(),
                "recommended_memory_gb": selected_workload.recommended_vram_gb,
                "type": provider.type,
                "within_cost_limit": offer.within_cost_limit(
                    selected_workload.hourly_cost_limit
                ),
                "workload": primary_profile,
            }
        )
    ranked_ids = {
        offer.id: index
        for index, offer in enumerate(
            rank_capacity(
                [
                    CapacityOffer(
                        available=bool(item.get("available", True)),
                        benchmarked_p95_seconds=(
                            float(item["benchmarked_p95_seconds"])
                            if isinstance(
                                item.get("benchmarked_p95_seconds"), (float, int)
                            )
                            else None
                        ),
                        cost_per_hour=(
                            float(item["cost_per_hour"])
                            if isinstance(item.get("cost_per_hour"), (float, int))
                            else None
                        ),
                        id=str(item.get("id", "automatic")),
                        label=str(item.get("label", item.get("id", "Capacity"))),
                        memory_gb=(
                            float(item["memory_gb"])
                            if isinstance(item.get("memory_gb"), (float, int))
                            else None
                        ),
                        mode=provider.type,
                        provider=provider.id,
                        reliability=(
                            float(item["reliability"])
                            if isinstance(item.get("reliability"), (float, int))
                            else None
                        ),
                    )
                    for item in prepared
                ],
                hourly_cost_limit=selected_workload.hourly_cost_limit,
                minimum_vram_gb=minimum_memory,
            )
        )
    }
    return sorted(
        prepared,
        key=lambda option: (
            str(option.get("id", "")) not in ranked_ids,
            ranked_ids.get(str(option.get("id", "")), len(prepared)),
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
