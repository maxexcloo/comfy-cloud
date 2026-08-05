from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    api_key: str
    catalog_dirs: tuple[Path, ...]
    comfy_url: str
    deployment_type: str
    models_dir: Path
    public_base_url: str
    request_timeout: float
    ui_password: str
    ui_username: str
    workflow_timeout: float
    jobs_dir: Path | None = None
    storage_env: dict[str, str] | None = None

    @property
    def ui_enabled(self) -> bool:
        return self.deployment_type == "pod"

    @classmethod
    def from_env(cls) -> Settings:
        deployment_type = os.getenv("MODE", os.getenv("DEPLOYMENT_TYPE", "pod")).lower()
        if deployment_type not in {"pod", "serverless"}:
            raise ValueError("MODE must be pod or serverless")
        roots = [Path(os.getenv("BUILTIN_CATALOG_DIR", "/opt/comfy-cloud/catalog"))]
        custom = os.getenv("CATALOG_DIR")
        if custom:
            roots.append(Path(custom))
        jobs_dir = os.getenv("JOBS_DIR")
        return cls(
            api_key=os.getenv("API_KEY", "change-me"),
            catalog_dirs=tuple(roots),
            comfy_url=os.getenv("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/"),
            deployment_type=deployment_type,
            models_dir=Path(os.getenv("MODELS_DIR", "/opt/ComfyUI/models")),
            public_base_url=os.getenv(
                "PUBLIC_BASE_URL", "http://localhost:8000"
            ).rstrip("/"),
            request_timeout=float(os.getenv("REQUEST_TIMEOUT", "60")),
            ui_password=os.getenv("COMFY_UI_PASSWORD", "change-me"),
            ui_username=os.getenv("COMFY_UI_USERNAME", "comfy"),
            workflow_timeout=float(os.getenv("WORKFLOW_TIMEOUT", "900")),
            jobs_dir=Path(jobs_dir) if jobs_dir else None,
        )
