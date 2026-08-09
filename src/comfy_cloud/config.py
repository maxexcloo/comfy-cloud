from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER_SECRETS = {"change-me", "change_me", "replace-me", "replace_me"}


def _required_secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value.lower() in PLACEHOLDER_SECRETS:
        raise ValueError(f"{name} must be set to a non-placeholder value")
    return value


@dataclass(frozen=True)
class Settings:
    api_key: str
    catalogue_dirs: tuple[Path, ...]
    comfy_url: str
    deployment_type: str
    models_dir: Path
    public_base_url: str | None
    request_timeout: float
    ui_password: str
    ui_username: str
    workflow_timeout: float
    jobs_dir: Path | None = None
    maximum_pending_generations: int = 8
    maximum_request_bytes: int = 100 * 1024 * 1024
    storage_env: dict[str, str] | None = None

    @property
    def ui_enabled(self) -> bool:
        return self.deployment_type == "pod"

    @classmethod
    def from_env(cls) -> Settings:
        deployment_type = os.getenv("MODE", os.getenv("DEPLOYMENT_TYPE", "pod")).lower()
        if deployment_type not in {"pod", "serverless"}:
            raise ValueError("MODE must be pod or serverless")
        roots = [Path(os.getenv("BUILTIN_CATALOGUE_DIR", "/opt/comfy-cloud/catalogue"))]
        custom = os.getenv("CATALOGUE_DIR")
        if custom:
            roots.append(Path(custom))
        jobs_dir = os.getenv("JOBS_DIR")
        api_key = _required_secret("API_KEY")
        ui_password = (
            _required_secret("COMFY_UI_PASSWORD")
            if deployment_type == "pod"
            else os.getenv("COMFY_UI_PASSWORD", "")
        )
        public_base_url = os.getenv("PUBLIC_BASE_URL")
        maximum_pending_generations = int(os.getenv("MAXIMUM_PENDING_GENERATIONS", "8"))
        maximum_request_bytes = int(
            os.getenv("MAXIMUM_REQUEST_BYTES", str(100 * 1024 * 1024))
        )
        if maximum_pending_generations < 1:
            raise ValueError("MAXIMUM_PENDING_GENERATIONS must be at least 1")
        if maximum_request_bytes < 1:
            raise ValueError("MAXIMUM_REQUEST_BYTES must be at least 1")
        return cls(
            api_key=api_key,
            catalogue_dirs=tuple(roots),
            comfy_url=os.getenv("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/"),
            deployment_type=deployment_type,
            models_dir=Path(os.getenv("MODELS_DIR", "/opt/ComfyUI/models")),
            public_base_url=public_base_url.rstrip("/") if public_base_url else None,
            request_timeout=float(os.getenv("REQUEST_TIMEOUT", "60")),
            ui_password=ui_password,
            ui_username=os.getenv("COMFY_UI_USERNAME", "comfy"),
            workflow_timeout=float(os.getenv("WORKFLOW_TIMEOUT", "900")),
            jobs_dir=Path(jobs_dir) if jobs_dir else None,
            maximum_pending_generations=maximum_pending_generations,
            maximum_request_bytes=maximum_request_bytes,
        )
