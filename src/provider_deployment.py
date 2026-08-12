from __future__ import annotations

import httpx

from .control_config import ControlSettings, Provider
from .control_preferences import ControlPreferences
from .provider_deployment_runpod import (
    deploy_pod as deploy_runpod_pod,
)
from .provider_deployment_runpod import (
    deploy_serverless as deploy_runpod_serverless,
)
from .provider_deployment_runpod import (
    terminate as terminate_runpod,
)
from .provider_deployment_salad import (
    deploy as deploy_salad,
)
from .provider_deployment_salad import (
    terminate as terminate_salad,
)
from .provider_deployment_vast import (
    deploy_pod as deploy_vast_pod,
)
from .provider_deployment_vast import (
    deploy_serverless as deploy_vast_serverless,
)
from .provider_deployment_vast import (
    terminate as terminate_vast,
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
    if management.kind == "runpod":
        return await deploy_runpod_serverless(client, provider, preferences, settings)
    if management.kind == "salad":
        return await deploy_salad(client, provider, preferences, settings)
    if management.kind == "vast-pod":
        return await deploy_vast_pod(client, provider, preferences, settings)
    if management.kind == "vast":
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
    if management.kind in {"runpod", "runpod-pod"}:
        return await terminate_runpod(client, provider, preferences, resource_id)
    if management.kind == "salad":
        return await terminate_salad(client, provider, preferences, resource_id)
    if management.kind in {"vast", "vast-pod"}:
        return await terminate_vast(client, provider, preferences, resource_id)
    raise RuntimeError(f"unsupported standalone provider: {management.kind}")
