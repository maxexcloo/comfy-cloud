from __future__ import annotations

import shlex
from dataclasses import dataclass

import httpx

from control.config import ControlSettings, Provider
from control.preferences import ControlPreferences

WORKER_COMMAND = ("comfy-control", "serverless")
WORKER_HEALTH_PORT = 8000
WORKER_INITIALISING_HEALTH_PATH = "/ping"
WORKER_LIVE_HEALTH_PATH = "/health/live"
WORKER_POD_CONTAINER_STORAGE_GB = 30
WORKER_READY_HEALTH_PATH = "/health/ready"
WORKER_SERVERLESS_PORT = 9000
WORKER_STORAGE_GB = 100


@dataclass(frozen=True)
class DeploymentSelection:
    memory_gb: float | None = None
    option_id: str | None = None
    variant: str | None = None


def required_preference(name: str, preferences: ControlPreferences) -> str:
    value = preferences.environment().get(name)
    if not value:
        raise RuntimeError(f"{name} is required to manage this provider")
    return value


def worker_model_profiles(
    provider: Provider, preferences: ControlPreferences
) -> list[str]:
    selected = {
        choice.model
        for choices in preferences.routes.values()
        for choice in choices
        if choice.provider in {provider.id, *provider.aliases}
    }
    profiles = [
        profile
        for profile in preferences.model_profiles
        if not preferences.routes or profile in selected
    ]
    if any(profile in {"flux-2-klein-9b", "krea-2-turbo"} for profile in profiles):
        profiles.append("image-upscale")
    return list(dict.fromkeys(profiles))


def worker_environment(
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
) -> dict[str, str]:
    configured = preferences.environment()
    environment = {
        "API_KEY": provider.api_key,
        "CONTROL_UI_PASSWORD": settings.ui_password,
        "CONTROL_UI_USERNAME": settings.ui_username,
    }
    for name in (
        "CIVITAI_TOKEN",
        "HF_TOKEN",
        "COMFYUI_REQUEST_TIMEOUT",
        "GENERATION_TIMEOUT",
        "GENERATION_QUEUE_LIMIT",
        "MAXIMUM_REQUEST_MIB",
        "MODEL_PROFILES",
    ):
        if (value := configured.get(name)) or name == "MODEL_PROFILES":
            environment[name] = str(value or "")
    environment["MODEL_PROFILES"] = ",".join(
        worker_model_profiles(provider, preferences)
    )
    warmup_models = {
        "flux-2-klein-9b": "flux-2-klein-9b/text-to-image",
        "krea-2-turbo": "krea-2-turbo/text-to-image",
    }
    environment["WARMUP_MODEL"] = next(
        (
            warmup_models[profile]
            for profile in worker_model_profiles(provider, preferences)
            if profile in warmup_models
        ),
        "",
    )
    return environment


def configured_environment(
    configured: object,
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
) -> dict[str, str]:
    if isinstance(configured, list):
        environment = {
            str(item["key"]): str(item["value"])
            for item in configured
            if isinstance(item, dict) and "key" in item and "value" in item
        }
    elif isinstance(configured, dict):
        environment = {str(key): str(value) for key, value in configured.items()}
    else:
        environment = {}
    environment.update(worker_environment(provider, preferences, settings))
    return environment


def serverless_environment() -> dict[str, str]:
    return {
        "HEALTH_CHECK_PATH": WORKER_INITIALISING_HEALTH_PATH,
        "MODELS_DIR": "/models",
        "PORT": str(WORKER_HEALTH_PORT),
        "PORT_HEALTH": str(WORKER_HEALTH_PORT),
    }


def http_probe(
    path: str,
    *,
    failure_threshold: int,
    period_seconds: int,
    timeout_seconds: int,
) -> dict[str, object]:
    return {
        "failure_threshold": failure_threshold,
        "initial_delay_seconds": 0,
        "period_seconds": period_seconds,
        "success_threshold": 1,
        "timeout_seconds": timeout_seconds,
        "http": {
            "path": path,
            "port": WORKER_HEALTH_PORT,
            "scheme": "http",
        },
    }


def docker_flags(environment: dict[str, str], port: int) -> str:
    flags = [
        f"-e {shlex.quote(f'{name}={value}')}"
        for name, value in sorted(environment.items())
    ]
    flags.append(f"-p {port}:{port}")
    return " ".join(flags)


def response_json(status_code: int, value: object) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=value,
        request=httpx.Request("POST", "https://provider.invalid/internal"),
    )


async def checked_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    **kwargs: object,
) -> httpx.Response:
    response = await client.request(method, url, **kwargs)
    response.raise_for_status()
    return response
