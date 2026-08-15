from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PLACEHOLDER_SECRETS = {"change-me", "change_me", "replace-me", "replace_me"}
DeploymentType = Literal["pod", "serverless"]


def required_secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    normalised = value.lower()
    if (
        not value
        or normalised in PLACEHOLDER_SECRETS
        or normalised.startswith("replace-with-")
    ):
        raise ValueError(f"{name} must be set to a non-placeholder value")
    return value


@dataclass(frozen=True)
class Settings:
    api_key: str
    catalogue_dirs: tuple[Path, ...]
    comfy_url: str
    deployment_type: DeploymentType
    models_dir: Path
    comfyui_request_timeout: float
    generation_timeout: float
    ui_password: str
    ui_username: str
    generation_queue_limit: int = 2
    maximum_request_bytes: int = 100 * 1024 * 1024
    warmup_model: str | None = None

    @property
    def ui_enabled(self) -> bool:
        return self.deployment_type == "pod"

    @classmethod
    def from_env(cls, deployment_type: DeploymentType) -> Settings:
        roots = [
            Path(os.getenv("BUILTIN_CATALOGUE_DIR", "/opt/comfy-control/catalogue"))
        ]
        custom = os.getenv("CATALOGUE_DIR")
        if custom:
            roots.append(Path(custom))
        api_key = required_secret("API_KEY")
        ui_password = (
            required_secret("CONTROL_UI_PASSWORD")
            if deployment_type == "pod"
            else os.getenv("CONTROL_UI_PASSWORD", "")
        )
        generation_queue_limit = int(os.getenv("GENERATION_QUEUE_LIMIT", "2"))
        maximum_request_bytes = (
            int(os.getenv("MAXIMUM_REQUEST_MIB", "100")) * 1024 * 1024
        )
        if generation_queue_limit < 1:
            raise ValueError("GENERATION_QUEUE_LIMIT must be at least 1")
        if maximum_request_bytes < 1:
            raise ValueError("MAXIMUM_REQUEST_MIB must be at least 1")
        return cls(
            api_key=api_key,
            catalogue_dirs=tuple(roots),
            comfy_url=os.getenv("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/"),
            deployment_type=deployment_type,
            models_dir=Path(os.getenv("MODELS_DIR", "/opt/ComfyUI/models")),
            comfyui_request_timeout=float(os.getenv("COMFYUI_REQUEST_TIMEOUT", "60")),
            ui_password=ui_password,
            ui_username=os.getenv("CONTROL_UI_USERNAME", "comfy"),
            generation_timeout=float(os.getenv("GENERATION_TIMEOUT", "900")),
            generation_queue_limit=generation_queue_limit,
            maximum_request_bytes=maximum_request_bytes,
            warmup_model=os.getenv("WARMUP_MODEL") or None,
        )
