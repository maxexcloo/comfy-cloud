from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from .control_config import Provider, ProviderAction
from .control_preferences import ControlPreferences
from .provider_adapter import (
    BaseAdapter,
    Discovery,
    named_resource,
    required_mapping_value,
)
from .provider_deployment_common import required_preference
from .provider_telemetry import first_number, selected_fields


def vast_headers(preferences: ControlPreferences) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {required_preference('VAST_API_KEY', preferences)}"
    }


async def vast_usage(
    client: httpx.AsyncClient,
    preferences: ControlPreferences,
) -> list[dict[str, object]]:
    headers = vast_headers(preferences)
    now = datetime.now(UTC)
    account, charges = await asyncio.gather(
        client.get("https://console.vast.ai/api/v0/users/current/", headers=headers),
        client.get(
            "https://console.vast.ai/api/v0/charges/",
            headers=headers,
            params={
                "limit": 500,
                "select_filters": json.dumps(
                    {
                        "day": {
                            "gte": int(
                                now.replace(
                                    day=1, hour=0, minute=0, second=0
                                ).timestamp()
                            ),
                            "lte": int(now.timestamp()),
                        }
                    },
                    separators=(",", ":"),
                ),
            },
        ),
    )
    account.raise_for_status()
    charges.raise_for_status()
    charge_payload = charges.json()
    records = (
        charge_payload.get("results", []) if isinstance(charge_payload, dict) else []
    )
    spend = sum(
        float(record.get("amount", 0)) for record in records if isinstance(record, dict)
    )
    balance = first_number(account.json(), "balance", "credit")
    metrics: list[dict[str, object]] = []
    if balance is not None:
        metric: dict[str, object] = {
            "label": "Credit",
            "unit": "USD",
            "value": balance,
        }
        if balance >= 0 and spend > 0:
            metric["maximum"] = round(balance + spend, 4)
        metrics.append(metric)
    metrics.append({"label": "Month Spend", "unit": "USD", "value": round(spend, 4)})
    return metrics


class VastAdapter(BaseAdapter):
    async def usage(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        history_usage: Callable[[str], dict[str, int]],
    ) -> list[dict[str, object]]:
        del provider, history_usage
        return await vast_usage(client, preferences)


class VastPodAdapter(VastAdapter):
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
        base_url = None
        if isinstance(mapping, list) and mapping and isinstance(mapping[0], dict):
            port = required_mapping_value(mapping[0], "HostPort")
            base_url = f"http://{address}:{port}"
        return Discovery(base_url, resource, identifier)

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


class VastServerlessAdapter(VastAdapter):
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
