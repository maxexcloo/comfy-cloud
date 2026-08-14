from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx

from comfy_control.control.config import ControlSettings, Provider, ProviderAction
from comfy_control.control.preferences import ControlPreferences
from comfy_control.providers.deployment import deploy_provider, terminate_provider
from comfy_control.providers.deployment.common import DeploymentSelection
from comfy_control.providers.telemetry import normalise_usage


class ProviderNotDeployed(RuntimeError):
    pass


@dataclass(frozen=True)
class Discovery:
    base_url: str | None
    resource: dict[str, object]
    resource_id: str
    routed_request: dict[str, object] | None = None


@dataclass(frozen=True)
class StartRecovery:
    new_resource_id: str
    old_resource_id: str


def required_mapping_value(value: object, key: str) -> object:
    if not isinstance(value, dict) or key not in value or value[key] in (None, ""):
        raise RuntimeError(f"provider response has no {key}")
    return value[key]


def named_resource(
    value: object, name: str, name_key: str, resource_id: str | None
) -> dict[str, object]:
    if not isinstance(value, list):
        raise TypeError("provider returned an invalid resource list")
    resources = [item for item in value if isinstance(item, dict)]
    if resource_id is not None:
        matches = [
            item
            for item in resources
            if str(item.get("id")) == resource_id and item.get(name_key) == name
        ]
        if len(matches) == 1:
            return matches[0]
    matches = [item for item in resources if item.get(name_key) == name]
    if not matches:
        raise ProviderNotDeployed(f"provider resource not found: {name}")
    if len(matches) > 1:
        raise RuntimeError(f"provider resource name is ambiguous: {name}")
    return matches[0]


class ProviderAdapter(Protocol):
    kind: str

    def actions(
        self, provider: Provider, preferences: ControlPreferences
    ) -> dict[str, ProviderAction]: ...

    async def deploy(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        settings: ControlSettings,
        selection: DeploymentSelection | None = None,
    ) -> httpx.Response: ...

    async def discover(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        resource_id: str | None,
        *,
        route: bool,
    ) -> Discovery: ...

    async def execute_serverless(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        spec: dict[str, object],
        files: list[tuple[str, tuple[str, bytes, str]]],
        routed_request: dict[str, object] | None,
    ) -> list[tuple[bytes, str, str]] | None: ...

    async def live_status(
        self, provider: Provider
    ) -> tuple[str, dict[str, object]]: ...

    async def recover_start(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        settings: ControlSettings,
        resource_id: str,
        response: httpx.Response,
    ) -> StartRecovery | None: ...

    def route_confirms_ready(self) -> bool: ...

    def panel_url(
        self, provider: Provider, details: dict[str, object], base_url: str | None
    ) -> str | None: ...

    def status(self, resource: dict[str, object]) -> tuple[str, dict[str, object]]: ...

    async def terminate(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        resource_id: str,
    ) -> httpx.Response: ...

    async def usage(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        history_usage: Callable[[str], dict[str, int]],
    ) -> list[dict[str, object]]: ...

    def worker_headers(
        self, provider: Provider, preferences: ControlPreferences
    ) -> dict[str, str]: ...

    def worker_log_error(self, error: str) -> str: ...


@dataclass(frozen=True)
class BaseAdapter:
    kind: str
    panel: str | None = None

    def actions(
        self, provider: Provider, preferences: ControlPreferences
    ) -> dict[str, ProviderAction]:
        del provider, preferences
        return {}

    async def deploy(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        settings: ControlSettings,
        selection: DeploymentSelection | None = None,
    ) -> httpx.Response:
        return await deploy_provider(
            client, provider, preferences, settings, selection=selection
        )

    async def discover(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        resource_id: str | None,
        *,
        route: bool,
    ) -> Discovery:
        raise RuntimeError(f"provider discovery is not implemented: {self.kind}")

    async def execute_serverless(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        spec: dict[str, object],
        files: list[tuple[str, tuple[str, bytes, str]]],
        routed_request: dict[str, object] | None,
    ) -> list[tuple[bytes, str, str]] | None:
        del client, provider, preferences, spec, files, routed_request
        return None

    async def live_status(self, provider: Provider) -> tuple[str, dict[str, object]]:
        raise RuntimeError(f"live provider status is not implemented: {self.kind}")

    async def recover_start(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        settings: ControlSettings,
        resource_id: str,
        response: httpx.Response,
    ) -> StartRecovery | None:
        del client, provider, preferences, settings, resource_id, response
        return None

    def route_confirms_ready(self) -> bool:
        return False

    def panel_url(
        self, provider: Provider, details: dict[str, object], base_url: str | None
    ) -> str | None:
        return self.panel or base_url

    def status(self, resource: dict[str, object]) -> tuple[str, dict[str, object]]:
        return "unknown", resource

    def worker_log_error(self, error: str) -> str:
        return error

    async def terminate(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        resource_id: str,
    ) -> httpx.Response:
        return await terminate_provider(client, provider, preferences, resource_id)

    async def usage(
        self,
        client: httpx.AsyncClient,
        provider: Provider,
        preferences: ControlPreferences,
        history_usage: Callable[[str], dict[str, int]],
    ) -> list[dict[str, object]]:
        del preferences, history_usage
        probe = provider.usage
        if probe is None:
            return []
        if probe.url is None:
            raise RuntimeError(f"provider usage URL is missing: {provider.id}")
        response = await client.get(probe.url, headers=probe.headers)
        response.raise_for_status()
        return normalise_usage(probe.kind, response.json())

    def worker_headers(
        self, provider: Provider, preferences: ControlPreferences
    ) -> dict[str, str]:
        del preferences
        return {"Authorization": f"Bearer {provider.api_key}"}
