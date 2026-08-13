from __future__ import annotations

from collections.abc import Callable

import httpx

from .control_config import Provider, ProviderAction
from .control_preferences import ControlPreferences
from .provider_adapter import (
    BaseAdapter,
    Discovery,
    ProviderNotDeployed,
    named_resource,
    required_mapping_value,
)
from .provider_deployment_common import required_preference
from .provider_telemetry import first_number, selected_fields


class RunPodAdapter(BaseAdapter):
    serverless: bool = False

    def actions(
        self, provider: Provider, preferences: ControlPreferences
    ) -> dict[str, ProviderAction]:
        if self.serverless:
            return {}
        headers = {
            "authorization": (
                f"Bearer {required_preference('RUNPOD_API_KEY', preferences)}"
            )
        }
        return {
            "start": ProviderAction(
                headers=headers,
                url="https://rest.runpod.io/v1/pods/{resource_id}/start",
            ),
            "stop": ProviderAction(
                confirmation="Stop the RunPod Pod?",
                headers=headers,
                url="https://rest.runpod.io/v1/pods/{resource_id}/stop",
            ),
        }

    async def discover(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        resource_id: str | None,
        *,
        route: bool,
    ) -> Discovery:
        del route
        management = provider.management
        assert management is not None
        collection = "endpoints" if self.serverless else "pods"
        response = await client.get(
            f"https://rest.runpod.io/v1/{collection}",
            headers={
                "Authorization": (
                    f"Bearer {required_preference('RUNPOD_API_KEY', preferences)}"
                )
            },
        )
        if response.status_code == 404:
            raise ProviderNotDeployed(f"provider resource not found: {management.name}")
        response.raise_for_status()
        resource = named_resource(response.json(), management.name, "name", resource_id)
        identifier = str(required_mapping_value(resource, "id"))
        base_url = (
            f"https://{identifier}.api.runpod.ai"
            if self.serverless
            else f"https://{identifier}-{management.port}.proxy.runpod.net"
        )
        return Discovery(base_url, resource, identifier)

    async def usage(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        history_usage: Callable[[str], dict[str, int]],
    ) -> list[dict[str, object]]:
        del provider, history_usage
        response = await client.post(
            "https://api.runpod.io/graphql",
            headers={
                "Authorization": (
                    f"Bearer {required_preference('RUNPOD_API_KEY', preferences)}"
                )
            },
            json={"query": "query { myself { currentSpendPerHr spendLimit } }"},
        )
        response.raise_for_status()
        payload = response.json()
        metrics = []
        for label, path, unit in (
            ("Current Spend", "data.myself.currentSpendPerHr", "USD/hour"),
            ("Spend Limit", "data.myself.spendLimit", "USD"),
        ):
            if (value := first_number(payload, path)) is not None:
                metrics.append({"label": label, "unit": unit, "value": value})
        if not metrics:
            raise RuntimeError("RunPod usage response did not include spend data")
        return metrics


class RunPodPodAdapter(RunPodAdapter):
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


class RunPodServerlessAdapter(RunPodAdapter):
    serverless = True

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
