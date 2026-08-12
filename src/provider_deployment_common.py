from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

import httpx

from .control_config import ControlSettings, Provider
from .control_preferences import ControlPreferences

DEPLOYMENT_ROOT = Path(
    os.getenv("CONTROL_DEPLOYMENT_ROOT", "/opt/comfy-control/deploy")
)


def required_preference(name: str, preferences: ControlPreferences) -> str:
    value = preferences.environment().get(name)
    if not value:
        raise RuntimeError(f"{name} is required to manage this provider")
    return value


def deployment_asset(*parts: str) -> dict[str, Any]:
    path = DEPLOYMENT_ROOT.joinpath(*parts)
    if not path.is_file():
        raise RuntimeError(f"provider deployment asset was not found: {path}")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError(f"provider deployment asset is invalid: {path}")
    return value


def worker_environment(
    provider: Provider,
    preferences: ControlPreferences,
    settings: ControlSettings,
) -> dict[str, str]:
    configured = preferences.environment()
    environment = {
        "API_KEY": provider.api_key,
        "COMFY_UI_PASSWORD": configured.get("COMFY_UI_PASSWORD", "").strip()
        or settings.ui_password,
        "COMFY_UI_USERNAME": configured.get("COMFY_UI_USERNAME", "").strip()
        or settings.ui_username,
    }
    for name in (
        "CIVITAI_TOKEN",
        "HF_TOKEN",
        "MAXIMUM_PENDING_GENERATIONS",
        "MAXIMUM_REQUEST_BYTES",
        "MODEL_PROFILES",
        "REQUEST_TIMEOUT",
        "WORKFLOW_TIMEOUT",
    ):
        if value := configured.get(name):
            environment[name] = value
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
