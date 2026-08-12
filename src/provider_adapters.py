from __future__ import annotations

from .control_config import Provider, ProviderAction
from .control_preferences import ControlPreferences
from .provider_adapter import ProviderAdapter, ProviderNotDeployed
from .provider_modal import ModalAdapter
from .provider_proxy import ProxyAdapter
from .provider_runpod import RunPodPodAdapter, RunPodServerlessAdapter
from .provider_salad import SaladAdapter
from .provider_vast import VastPodAdapter, VastServerlessAdapter

ADAPTERS: dict[str, ProviderAdapter] = {
    "modal": ModalAdapter("modal", "https://modal.com/apps"),
    "proxy": ProxyAdapter("proxy"),
    "runpod-pod": RunPodPodAdapter("runpod-pod", "https://console.runpod.io/pods"),
    "runpod": RunPodServerlessAdapter("runpod", "https://console.runpod.io/serverless"),
    "salad": SaladAdapter("salad", "https://portal.salad.com/"),
    "vast-pod": VastPodAdapter("vast-pod", "https://cloud.vast.ai/instances/"),
    "vast": VastServerlessAdapter("vast", "https://cloud.vast.ai/serverless/"),
}


def provider_adapter(provider: Provider) -> ProviderAdapter | None:
    management = provider.management
    if management is not None:
        return ADAPTERS.get(management.kind)
    return ADAPTERS.get("proxy") if provider.type == "proxy" else None


def provider_panel_url(
    provider: Provider, details: dict[str, object], base_url: str | None
) -> str | None:
    adapter = provider_adapter(provider)
    if adapter is not None:
        return adapter.panel_url(provider, details, base_url)
    return base_url


def available_provider_actions(
    provider: Provider,
    preferences: ControlPreferences,
    resource_id: str | None,
) -> dict[str, ProviderAction]:
    actions = dict(provider.actions)
    if provider.lifecycle.start is not None:
        actions["start"] = provider.lifecycle.start
    if provider.lifecycle.stop is not None:
        actions["stop"] = provider.lifecycle.stop
    adapter = provider_adapter(provider)
    if adapter is not None:
        for name, action in adapter.actions(provider, preferences).items():
            actions.setdefault(name, action)
    return dict(
        sorted(
            (name, action)
            for name, action in actions.items()
            if not (name == "deploy" and resource_id)
            and not (
                resource_id is None
                and name in {"delete", "destroy", "start", "stop", "terminate"}
            )
            and not (
                resource_id is None and action.url and "{resource_id}" in action.url
            )
        )
    )


__all__ = [
    "ProviderNotDeployed",
    "available_provider_actions",
    "provider_adapter",
    "provider_panel_url",
]
