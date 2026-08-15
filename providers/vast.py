from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from control.config import Provider, ProviderAction
from control.preferences import ControlPreferences
from providers.base import (
    BaseAdapter,
    Discovery,
    named_resource,
    required_mapping_value,
)
from providers.deployment.common import required_preference
from providers.telemetry import first_number, selected_fields


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
    account_result, charges_result = await asyncio.gather(
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
        return_exceptions=True,
    )
    if isinstance(account_result, BaseException):
        raise account_result
    account = account_result
    account.raise_for_status()
    charges = charges_result if isinstance(charges_result, httpx.Response) else None
    charge_payload = (
        charges.json() if charges is not None and charges.is_success else {}
    )
    records = (
        charge_payload.get("results", []) if isinstance(charge_payload, dict) else []
    )
    spend = sum(
        float(record.get("amount", 0)) for record in records if isinstance(record, dict)
    )
    account_payload = account.json()
    balance = first_number(account_payload, "credit", "balance")
    total_spend = first_number(account_payload, "total_spend")
    if total_spend is not None:
        total_spend = abs(total_spend)
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
    if charges is not None and charges.is_success:
        metrics.append(
            {"label": "Month Spend", "unit": "USD", "value": round(spend, 4)}
        )
    if total_spend is not None:
        metrics.append({"label": "Total Spend", "unit": "USD", "value": total_spend})
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
            "gpu_ram",
            "gpu_util",
            "num_gpus",
        )


class VastServerlessAdapter(VastAdapter):
    def worker_log_error(self, error: str) -> str:
        if "no ready worker" in error.casefold():
            return "Waiting for Vast serverless worker assignment"
        return error

    async def execute_serverless(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        spec: dict[str, object],
        files: list[tuple[str, tuple[str, bytes, str]]],
        routed_request: dict[str, object] | None,
    ) -> list[tuple[bytes, str, str]] | None:
        management = provider.management
        assert management is not None
        request_index = 0
        deadline = asyncio.get_running_loop().time() + provider.request_timeout
        route = routed_request or {}
        while not route.get("url") and asyncio.get_running_loop().time() < deadline:
            route_response = await client.post(
                "https://run.vast.ai/route/",
                headers=vast_headers(preferences),
                json={
                    "cost": 100,
                    "endpoint": management.name,
                    "request_idx": request_index,
                },
            )
            route_response.raise_for_status()
            value = route_response.json()
            if not isinstance(value, dict):
                raise TypeError("Vast.ai route response is invalid")
            route = value
            if route.get("url"):
                break
            request_index = int(route.get("request_idx") or request_index)
            await asyncio.sleep(2)
        worker_url = route.get("url")
        if not isinstance(worker_url, str) or not worker_url:
            raise TimeoutError("Vast.ai Serverless did not provide a ready worker")
        token = required_preference("VAST_API_KEY", preferences)
        response = await client.post(
            f"{worker_url.rstrip('/')}/internal/executions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "auth_data": route,
                "payload": {
                    "files": [
                        {
                            "content": base64.b64encode(content).decode(),
                            "content_type": content_type,
                            "field": field,
                            "filename": filename,
                        }
                        for field, (filename, content, content_type) in files
                    ],
                    "spec": spec,
                },
            },
            params={"api_key": token},
            timeout=provider.request_timeout,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result") if isinstance(payload, dict) else None
        outputs = result.get("outputs") if isinstance(result, dict) else None
        if not isinstance(outputs, list):
            raise TypeError("Vast.ai worker returned an invalid execution result")
        decoded = []
        for output in outputs:
            if not isinstance(output, dict):
                raise TypeError("Vast.ai worker returned an invalid output")
            try:
                decoded.append(
                    (
                        base64.b64decode(str(output["content"]), validate=True),
                        str(output["content_type"]),
                        str(output["filename"]),
                    )
                )
            except (KeyError, ValueError) as exc:
                raise RuntimeError("Vast.ai worker returned an invalid output") from exc
        return decoded

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
        routed_request = None
        if route:
            route_response = await client.post(
                "https://run.vast.ai/route/",
                headers=headers,
                json={"cost": 100, "endpoint": management.name},
            )
            route_response.raise_for_status()
            value = route_response.json()
            if not isinstance(value, dict):
                raise TypeError("Vast.ai route response is invalid")
            routed_request = value
            base_url = str(required_mapping_value(value, "url")).rstrip("/")
        return Discovery(base_url, resource, identifier, routed_request)

    def route_confirms_ready(self) -> bool:
        return True

    def status(self, resource: dict[str, object]) -> tuple[str, dict[str, object]]:
        return str(resource.get("endpoint_state", "unknown")).lower(), selected_fields(
            resource,
            "endpoint_state",
            "max_workers",
            "min_load",
            "num_workers",
            "target_util",
        )
