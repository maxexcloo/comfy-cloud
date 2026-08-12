from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

from .control_config import ControlSettings, Provider
from .control_preferences import ControlPreferences
from .provider_deployment import deploy_provider, terminate_provider
from .provider_telemetry import first_number, selected_fields


class ProviderAdapter(Protocol):
    kind: str

    def status(self, resource: dict[str, object]) -> tuple[str, dict[str, object]]: ...

    def panel_url(
        self, provider: Provider, details: dict[str, object], base_url: str | None
    ) -> str | None: ...

    async def deploy(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        settings: ControlSettings,
    ) -> httpx.Response: ...

    async def terminate(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        resource_id: str,
    ) -> httpx.Response: ...


@dataclass(frozen=True)
class BaseAdapter:
    kind: str
    panel: str | None = None

    def status(self, resource: dict[str, object]) -> tuple[str, dict[str, object]]:
        return "unknown", resource

    def panel_url(
        self, provider: Provider, details: dict[str, object], base_url: str | None
    ) -> str | None:
        return self.panel or base_url

    async def deploy(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        settings: ControlSettings,
    ) -> httpx.Response:
        return await deploy_provider(client, provider, preferences, settings)

    async def terminate(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        resource_id: str,
    ) -> httpx.Response:
        return await terminate_provider(client, provider, preferences, resource_id)


class ModalAdapter(BaseAdapter):
    def panel_url(
        self, provider: Provider, details: dict[str, object], base_url: str | None
    ) -> str | None:
        return str(details.get("panel_url") or self.panel)


class RunPodPodAdapter(BaseAdapter):
    def status(self, resource: dict[str, object]) -> tuple[str, dict[str, object]]:
        return str(resource.get("desiredStatus", "unknown")).lower(), selected_fields(
            resource,
            "costPerHr",
            "desiredStatus",
            "gpuCount",
            "gpuTypeId",
            "lastStartedAt",
            "machineId",
            "vcpuCount",
        )


class RunPodServerlessAdapter(BaseAdapter):
    def status(self, resource: dict[str, object]) -> tuple[str, dict[str, object]]:
        workers = resource.get("workers")
        active = first_number(workers, "running", "ready")
        return "ready" if active else "scaled-down", selected_fields(
            resource,
            "executionTimeoutMs",
            "gpuIds",
            "idleTimeout",
            "maxWorkers",
            "minWorkers",
            "workers",
        )


class SaladAdapter(BaseAdapter):
    def status(self, resource: dict[str, object]) -> tuple[str, dict[str, object]]:
        current = resource.get("current_state")
        state = str(
            (current.get("status") if isinstance(current, dict) else None) or "unknown"
        ).lower()
        return state, selected_fields(
            resource, "autostart_policy", "current_state", "display_name", "replicas"
        )


class VastPodAdapter(BaseAdapter):
    def status(self, resource: dict[str, object]) -> tuple[str, dict[str, object]]:
        return str(resource.get("actual_status", "unknown")).lower(), selected_fields(
            resource,
            "actual_status",
            "cur_state",
            "dph_total",
            "gpu_name",
            "gpu_util",
            "num_gpus",
        )


class VastServerlessAdapter(BaseAdapter):
    def status(self, resource: dict[str, object]) -> tuple[str, dict[str, object]]:
        return str(resource.get("endpoint_state", "unknown")).lower(), selected_fields(
            resource,
            "endpoint_state",
            "max_workers",
            "min_load",
            "num_workers",
            "target_util",
        )


ADAPTERS: dict[str, ProviderAdapter] = {
    "modal": ModalAdapter("modal", "https://modal.com/apps"),
    "runpod-pod": RunPodPodAdapter("runpod-pod", "https://console.runpod.io/pods"),
    "runpod-serverless": RunPodServerlessAdapter(
        "runpod-serverless", "https://console.runpod.io/serverless"
    ),
    "salad": SaladAdapter("salad", "https://portal.salad.com/"),
    "vast-pod": VastPodAdapter("vast-pod", "https://cloud.vast.ai/instances/"),
    "vast-serverless": VastServerlessAdapter(
        "vast-serverless", "https://cloud.vast.ai/serverless/"
    ),
}


def provider_adapter(provider: Provider) -> ProviderAdapter | None:
    management = provider.management
    return ADAPTERS.get(management.kind) if management is not None else None


def provider_panel_url(
    provider: Provider, details: dict[str, object], base_url: str | None
) -> str | None:
    adapter = provider_adapter(provider)
    if adapter is not None:
        return adapter.panel_url(provider, details, base_url)
    if provider.type == "proxy" and base_url:
        return f"{base_url}/management.html"
    return base_url
