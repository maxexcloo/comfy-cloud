from __future__ import annotations

from urllib.parse import quote

import httpx

from control.config import Provider, ProviderAction
from control.preferences import ControlPreferences
from providers.base import (
    BaseAdapter,
    Discovery,
    ProviderNotDeployed,
    required_mapping_value,
)
from providers.deployment.common import required_preference
from providers.telemetry import normalise_usage, selected_fields


class SaladAdapter(BaseAdapter):
    def actions(
        self, provider: Provider, preferences: ControlPreferences
    ) -> dict[str, ProviderAction]:
        management = provider.management
        assert management is not None
        organisation = management.organisation or preferences.salad_organisation
        project = management.project or preferences.salad_project
        if not organisation or not project:
            return {}
        base_url = (
            "https://api.salad.com/api/public/organizations/"
            f"{quote(organisation, safe='')}/projects/{quote(project, safe='')}"
            f"/containers/{quote(management.name, safe='')}"
        )
        headers = {"Salad-Api-Key": required_preference("SALAD_API_KEY", preferences)}
        return {
            "start": ProviderAction(headers=headers, url=f"{base_url}/start"),
            "stop": ProviderAction(
                confirmation="Stop the SaladCloud container group?",
                headers=headers,
                url=f"{base_url}/stop",
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
        del resource_id, route
        management = provider.management
        assert management is not None
        organisation = management.organisation or required_preference(
            "SALAD_ORGANISATION", preferences
        )
        project = management.project or required_preference(
            "SALAD_PROJECT", preferences
        )
        response = await client.get(
            "https://api.salad.com/api/public/organizations/"
            f"{quote(organisation, safe='')}/projects/{quote(project, safe='')}"
            f"/containers/{quote(management.name, safe='')}",
            headers={
                "Salad-Api-Key": required_preference("SALAD_API_KEY", preferences)
            },
        )
        if response.status_code == 404:
            raise ProviderNotDeployed(f"provider resource not found: {management.name}")
        response.raise_for_status()
        resource = response.json()
        networking = required_mapping_value(resource, "networking")
        if not isinstance(networking, dict):
            raise TypeError("SaladCloud provider has invalid networking data")
        dns = str(required_mapping_value(networking, "dns"))
        return Discovery(
            dns if "://" in dns else f"https://{dns}",
            resource,
            str(required_mapping_value(resource, "id")),
        )

    def status(self, resource: dict[str, object]) -> tuple[str, dict[str, object]]:
        current = resource.get("current_state")
        state = str(
            (current.get("status") if isinstance(current, dict) else None) or "unknown"
        ).lower()
        return state, selected_fields(
            resource, "autostart_policy", "current_state", "display_name", "replicas"
        )

    async def usage(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        history_usage,
    ) -> list[dict[str, object]]:
        del history_usage
        probe = provider.usage
        assert probe is not None
        organisation = required_preference("SALAD_ORGANISATION", preferences)
        url = probe.url or (
            "https://api.salad.com/api/public/organizations/"
            f"{quote(organisation, safe='')}/quotas"
        )
        headers = probe.headers or {
            "Salad-Api-Key": required_preference("SALAD_API_KEY", preferences)
        }
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return normalise_usage(probe.kind, response.json())
