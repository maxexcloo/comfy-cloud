from __future__ import annotations

import httpx

from .control_config import Provider, ProviderAction
from .control_preferences import ControlPreferences
from .provider_adapter import (
    BaseAdapter,
    Discovery,
    named_resource,
    required_mapping_value,
)
from .provider_deployment import required_preference
from .provider_telemetry import selected_fields


def vast_headers(preferences: ControlPreferences) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {required_preference('VAST_API_KEY', preferences)}"
    }


class VastPodAdapter(BaseAdapter):
    def actions(
        self, provider: Provider, preferences: ControlPreferences
    ) -> dict[str, ProviderAction]:
        headers = {
            "authorization": f"Bearer {required_preference('VAST_API_KEY', preferences)}"
        }
        url = "https://console.vast.ai/api/v0/instances/{resource_id}/"
        return {
            "start": ProviderAction(
                headers=headers,
                json={"state": "running"},
                method="PUT",
                url=url,
            ),
            "stop": ProviderAction(
                confirmation="Stop the Vast.ai Pod?",
                headers=headers,
                json={"state": "stopped"},
                method="PUT",
                url=url,
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
        response = await client.get(
            "https://console.vast.ai/api/v1/instances/",
            headers=vast_headers(preferences),
        )
        response.raise_for_status()
        resource = named_resource(
            required_mapping_value(response.json(), "instances"),
            management.name,
            "label",
            resource_id,
        )
        identifier = str(required_mapping_value(resource, "id"))
        address = required_mapping_value(resource, "public_ipaddr")
        ports = required_mapping_value(resource, "ports")
        mapping = (
            ports.get(f"{management.port}/tcp") if isinstance(ports, dict) else None
        )
        if (
            not isinstance(mapping, list)
            or not mapping
            or not isinstance(mapping[0], dict)
        ):
            raise RuntimeError(
                f"Vast.ai provider has no public mapping for port {management.port}"
            )
        port = required_mapping_value(mapping[0], "HostPort")
        return Discovery(f"http://{address}:{port}", resource, identifier)

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
    async def discover(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        resource_id: str | None,
        *,
        route: bool,
    ) -> Discovery:
        management = provider.management
        assert management is not None
        headers = vast_headers(preferences)
        response = await client.get(
            "https://console.vast.ai/api/v0/endptjobs/", headers=headers
        )
        response.raise_for_status()
        resource = named_resource(
            required_mapping_value(response.json(), "results"),
            management.name,
            "endpoint_name",
            resource_id,
        )
        identifier = str(required_mapping_value(resource, "id"))
        base_url = None
        if route:
            route_response = await client.post(
                "https://run.vast.ai/route/",
                headers=headers,
                json={"cost": 100, "endpoint": management.name},
            )
            route_response.raise_for_status()
            base_url = str(required_mapping_value(route_response.json(), "url")).rstrip(
                "/"
            )
        return Discovery(base_url, resource, identifier)

    def status(self, resource: dict[str, object]) -> tuple[str, dict[str, object]]:
        return str(resource.get("endpoint_state", "unknown")).lower(), selected_fields(
            resource,
            "endpoint_state",
            "max_workers",
            "min_load",
            "num_workers",
            "target_util",
        )
