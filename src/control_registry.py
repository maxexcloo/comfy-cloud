from __future__ import annotations

from collections.abc import Mapping

from .control_config import (
    ControlFile,
    Provider,
    ProviderAction,
    ProviderLifecycle,
    ProviderManagement,
    RoutedModel,
    Target,
    UsageProbe,
)

PROVIDER_CATALOGUE = (
    {"id": "cliproxyapi", "platform": "CLI Proxy API", "type": "proxy"},
    {"id": "modal", "platform": "Modal", "type": "serverless"},
    {"id": "runpod", "platform": "RunPod", "type": "serverless"},
    {"id": "runpod-pod", "platform": "RunPod (Pod)", "type": "pod"},
    {"id": "salad", "platform": "SaladCloud", "type": "serverless"},
    {"id": "vast", "platform": "Vast", "type": "serverless"},
    {"id": "vast-pod", "platform": "Vast (Pod)", "type": "pod"},
)

MODEL_ROUTES = {
    "image-edit": (
        "image_edit",
        "flux-2-klein-9b/image-edit",
        (
            "modal",
            "runpod-pod",
            "runpod",
            "salad",
            "vast-pod",
            "vast",
        ),
        ("cliproxyapi", "grok-imagine-image-quality"),
    ),
    "image-generation": (
        "image_generation",
        "flux-2-klein-9b/text-to-image",
        (
            "modal",
            "runpod-pod",
            "runpod",
            "salad",
            "vast-pod",
            "vast",
        ),
        ("cliproxyapi", "grok-imagine-image-quality"),
    ),
    "image-to-video": (
        "video_generation",
        "minimax-h3/image-to-video",
        (
            "modal",
            "runpod-pod",
            "runpod",
            "salad",
            "vast-pod",
            "vast",
        ),
        ("cliproxyapi", "grok-imagine-video-1.5"),
    ),
    "text-to-video": (
        "video_generation",
        "minimax-h3/text-to-video",
        (
            "modal",
            "runpod-pod",
            "runpod",
            "salad",
            "vast-pod",
            "vast",
        ),
        ("cliproxyapi", "grok-imagine-video-1.5"),
    ),
}
ROUTE_FAMILIES = {
    "image-edit": "images",
    "image-generation": "images",
    "image-to-video": "videos",
    "text-to-video": "videos",
}


def bearer(name: str, environment: Mapping[str, str]) -> dict[str, str]:
    return {"authorization": f"Bearer {environment[name]}"}


def internal_action(
    name: str, confirmation: str | None = None, resource_id_path: str | None = None
) -> ProviderAction:
    return ProviderAction(
        confirmation=confirmation, internal=name, resource_id_path=resource_id_path
    )


def managed_provider(
    *,
    api_key: str,
    identifier: str,
    kind: str,
    name: str,
    platform: str,
    provider_type: str,
    idle_seconds: int,
    aliases: list[str] | None = None,
    function: str | None = None,
    usage: UsageProbe | None = None,
    actions: dict[str, ProviderAction] | None = None,
    lifecycle: ProviderLifecycle | None = None,
) -> Provider:
    return Provider.model_validate(
        {
            "actions": actions or {},
            "aliases": aliases or [],
            "api_key": api_key,
            "id": identifier,
            "idle_seconds": idle_seconds,
            "lifecycle": lifecycle or ProviderLifecycle(),
            "management": ProviderManagement(function=function, kind=kind, name=name),
            "platform": platform,
            "type": provider_type,
            "usage": usage,
        }
    )


def providers(environment: Mapping[str, str]) -> list[Provider]:
    configured: list[Provider] = []
    worker_key = environment.get("WORKER_API_KEY", "")
    if environment.get("CLIPROXY_API_KEY") and environment.get("CLIPROXY_URL"):
        headers = (
            bearer("CLIPROXY_MANAGEMENT_KEY", environment)
            if environment.get("CLIPROXY_MANAGEMENT_KEY")
            else {}
        )
        configured.append(
            Provider(
                aliases=["cliproxy"],
                api_key=environment["CLIPROXY_API_KEY"],
                base_url=environment["CLIPROXY_URL"],
                health_path="/v1/models",
                id="cliproxyapi",
                idle_seconds=0,
                platform="CLI Proxy API",
                type="proxy",
                usage=UsageProbe(
                    headers=headers,
                    kind="cliproxyapi",
                    url=f"{environment['CLIPROXY_URL']}/v0/management/usage-statistics-enabled",
                ),
            )
        )
    if environment.get("MODAL_TOKEN_ID") and worker_key:
        configured.append(
            managed_provider(
                actions={
                    "deploy": internal_action("modal-deploy"),
                    "terminate": internal_action(
                        "modal-terminate",
                        "Terminate the Modal deployment? Its volume will be preserved.",
                    ),
                },
                api_key=worker_key,
                function="serve",
                identifier="modal",
                idle_seconds=0,
                kind="modal",
                name="comfy-control",
                platform="Modal",
                provider_type="serverless",
                usage=UsageProbe(kind="modal"),
            )
        )
    if environment.get("RUNPOD_API_KEY") and worker_key:
        headers = bearer("RUNPOD_API_KEY", environment)
        configured.extend(
            [
                managed_provider(
                    actions={
                        "deploy": internal_action(
                            "provider-deploy", resource_id_path="id"
                        ),
                        "terminate": internal_action(
                            "provider-terminate",
                            "Terminate the RunPod Pod? Its Pod volume will be deleted.",
                        ),
                    },
                    api_key=worker_key,
                    identifier="runpod-pod",
                    idle_seconds=600,
                    kind="runpod-pod",
                    lifecycle=ProviderLifecycle(
                        start=ProviderAction(
                            headers=headers,
                            url="https://rest.runpod.io/v1/pods/{resource_id}/start",
                        ),
                        stop=ProviderAction(
                            confirmation="Stop the RunPod Pod?",
                            headers=headers,
                            url="https://rest.runpod.io/v1/pods/{resource_id}/stop",
                        ),
                    ),
                    name="comfy-control",
                    platform="RunPod",
                    provider_type="pod",
                    usage=UsageProbe(
                        headers=headers,
                        kind="runpod",
                        url="https://rest.runpod.io/v1/billing/pods",
                    ),
                ),
                managed_provider(
                    actions={
                        "deploy": internal_action(
                            "provider-deploy", resource_id_path="id"
                        ),
                        "scale-down": ProviderAction(
                            headers=headers,
                            json={"workersMin": 0},
                            method="PATCH",
                            url="https://rest.runpod.io/v1/endpoints/{resource_id}",
                        ),
                        "scale-up": ProviderAction(
                            headers=headers,
                            json={"workersMin": 1},
                            method="PATCH",
                            url="https://rest.runpod.io/v1/endpoints/{resource_id}",
                        ),
                        "terminate": internal_action(
                            "provider-terminate",
                            "Terminate the RunPod Serverless endpoint and its workers?",
                        ),
                    },
                    api_key=worker_key,
                    identifier="runpod",
                    idle_seconds=0,
                    kind="runpod",
                    name="comfy-control",
                    platform="RunPod",
                    provider_type="serverless",
                    usage=UsageProbe(
                        headers=headers,
                        kind="runpod",
                        url="https://rest.runpod.io/v1/billing/endpoints",
                    ),
                ),
            ]
        )
    if environment.get("SALAD_API_KEY") and worker_key:
        configured.append(
            managed_provider(
                actions={
                    "deploy": internal_action("provider-deploy", resource_id_path="id"),
                    "terminate": internal_action(
                        "provider-terminate",
                        "Terminate the SaladCloud container group?",
                    ),
                },
                api_key=worker_key,
                identifier="salad",
                idle_seconds=600,
                kind="salad",
                name="comfy-control",
                platform="SaladCloud",
                provider_type="serverless",
                usage=UsageProbe(kind="salad"),
            )
        )
    if environment.get("VAST_API_KEY") and worker_key:
        headers = bearer("VAST_API_KEY", environment)
        usage = UsageProbe(
            headers=headers,
            kind="vast",
            url="https://console.vast.ai/api/v0/users/current/",
        )
        configured.extend(
            [
                managed_provider(
                    actions={
                        "deploy": internal_action(
                            "provider-deploy", resource_id_path="new_contract"
                        ),
                        "terminate": internal_action(
                            "provider-terminate",
                            "Terminate the Vast.ai Pod? Its instance disk will be deleted.",
                        ),
                    },
                    api_key=worker_key,
                    identifier="vast-pod",
                    idle_seconds=600,
                    kind="vast-pod",
                    lifecycle=ProviderLifecycle(
                        start=ProviderAction(
                            headers=headers,
                            json={"state": "running"},
                            method="PUT",
                            url="https://console.vast.ai/api/v0/instances/{resource_id}/",
                        ),
                        stop=ProviderAction(
                            confirmation="Stop the Vast.ai Pod?",
                            headers=headers,
                            json={"state": "stopped"},
                            method="PUT",
                            url="https://console.vast.ai/api/v0/instances/{resource_id}/",
                        ),
                    ),
                    name="comfy-control-pod",
                    platform="Vast",
                    provider_type="pod",
                    usage=usage,
                ),
                managed_provider(
                    actions={
                        "deploy": internal_action(
                            "provider-deploy", resource_id_path="id"
                        ),
                        "terminate": internal_action(
                            "provider-terminate",
                            "Terminate the Vast.ai Serverless endpoint?",
                        ),
                    },
                    api_key=worker_key,
                    identifier="vast",
                    idle_seconds=0,
                    kind="vast",
                    name="comfy-control-serverless",
                    platform="Vast",
                    provider_type="serverless",
                    usage=usage,
                ),
            ]
        )
    return sorted(configured, key=lambda provider: provider.id)


def routes() -> dict[str, list[str]]:
    configured: dict[str, list[str]] = {}
    for identifier, (_, _, provider_ids, fallback) in MODEL_ROUTES.items():
        family = ROUTE_FAMILIES[identifier]
        configured.setdefault(family, [*provider_ids, fallback[0]])
    return configured


def control_file(environment: Mapping[str, str]) -> ControlFile:
    configured_providers = providers(environment)
    enabled = {provider.id for provider in configured_providers}
    models = []
    for identifier, (
        operation,
        worker_model,
        provider_ids,
        fallback,
    ) in MODEL_ROUTES.items():
        targets = [
            Target(model=worker_model, provider=provider)
            for provider in provider_ids
            if provider in enabled
        ]
        if fallback[0] in enabled:
            targets.append(Target(model=fallback[1], provider=fallback[0]))
        if targets:
            models.append(
                RoutedModel(id=identifier, operation=operation, targets=targets)
            )
    return ControlFile(models=models, providers=configured_providers)
