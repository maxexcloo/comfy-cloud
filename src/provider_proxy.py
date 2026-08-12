from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx

from .control_config import Provider, UsageProbe
from .control_preferences import ControlPreferences
from .provider_adapter import BaseAdapter
from .provider_telemetry import (
    deduplicate_metrics,
    normalise_usage,
    normalise_xai_quota,
    xai_user_id,
)


class ProxyAdapter(BaseAdapter):
    def panel_url(
        self, provider: Provider, details: dict[str, object], base_url: str | None
    ) -> str | None:
        del provider, details
        return f"{base_url}/management.html" if base_url else None

    async def usage(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        history_usage: Callable[[str], dict[str, int]],
    ) -> list[dict[str, object]]:
        del preferences
        probe = provider.usage
        assert probe is not None and probe.url is not None
        response = await client.get(probe.url, headers=probe.headers)
        response.raise_for_status()
        metrics = normalise_usage(probe.kind, history_usage(provider.id))
        try:
            metrics.extend(await xai_quotas(client, probe))
        except Exception:  # noqa: BLE001 - optional quota telemetry
            metrics.append({"label": "Grok allowances", "value": "unavailable"})
        return metrics


async def xai_quotas(
    client: httpx.AsyncClient, probe: UsageProbe
) -> list[dict[str, object]]:
    management_url = str(probe.url).rsplit("/", 1)[0]
    response = await client.get(f"{management_url}/auth-files", headers=probe.headers)
    response.raise_for_status()
    payload = response.json()
    files = payload.get("files", []) if isinstance(payload, dict) else []
    accounts = [
        item
        for item in files
        if isinstance(item, dict)
        and str(item.get("type", "")).casefold() == "xai"
        and item.get("disabled") is not True
        and (item.get("auth_index") or item.get("authIndex"))
    ]
    results = await asyncio.gather(
        *(
            xai_quota_account(client, management_url, probe, account, index)
            for index, account in enumerate(accounts, 1)
        ),
        return_exceptions=True,
    )
    return [
        metric for result in results if isinstance(result, list) for metric in result
    ]


async def xai_quota_account(
    client: httpx.AsyncClient,
    management_url: str,
    probe: UsageProbe,
    account: dict[str, object],
    index: int,
) -> list[dict[str, object]]:
    request_headers = {
        "Authorization": "Bearer $TOKEN$",
        "accept": "*/*",
        "user-agent": "grok-pager/0.2.91 grok-shell/0.2.91 (macos; aarch64)",
        "x-grok-client-version": "0.2.91",
        "x-xai-token-auth": "xai-grok-cli",
    }
    if user_id := xai_user_id(account):
        request_headers["x-userid"] = user_id
    calls = await asyncio.gather(
        *(
            client.post(
                f"{management_url}/api-call",
                headers=probe.headers,
                json={
                    "authIndex": str(
                        account.get("auth_index") or account.get("authIndex")
                    ),
                    "header": request_headers,
                    "method": "GET",
                    "url": url,
                },
            )
            for url in (
                "https://cli-chat-proxy.grok.com/v1/billing?format=credits",
                "https://cli-chat-proxy.grok.com/v1/billing",
            )
        ),
        return_exceptions=True,
    )
    metrics: list[dict[str, object]] = []
    for response in calls:
        if not isinstance(response, httpx.Response) or not response.is_success:
            continue
        result = response.json()
        if (
            not isinstance(result, dict)
            or not 200 <= int(result.get("status_code", 0)) < 300
        ):
            continue
        body = result.get("body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                continue
        metrics.extend(normalise_xai_quota(body, index))
    return deduplicate_metrics(metrics)
