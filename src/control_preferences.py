from __future__ import annotations

import base64
import hashlib
import os
from collections.abc import Mapping
from typing import Any, ClassVar

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .control_store import ControlStore

DEFAULT_WORKER_IMAGE = "ghcr.io/maxexcloo/comfy-control:worker"
WORKER_IMAGE_REPOSITORY = "ghcr.io/maxexcloo/comfy-control"


def revision_worker_image() -> str | None:
    revision = os.getenv("COMFY_CONTROL_REVISION", "").strip().lower()
    if len(revision) < 7 or any(
        character not in "0123456789abcdef" for character in revision
    ):
        return None
    return f"{WORKER_IMAGE_REPOSITORY}:sha-{revision[:7]}-worker"


class ConfigurationConflict(RuntimeError):
    pass


class RoutePreference(BaseModel):
    provider: str
    model: str


class ControlPreferences(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    ENVIRONMENT_FIELDS: ClassVar[dict[str, tuple[str, ...]]] = {
        "civitai_token": ("CIVITAI_TOKEN",),
        "cliproxy_api_key": ("CLIPROXY_API_KEY",),
        "cliproxy_management_key": ("CLIPROXY_MANAGEMENT_KEY",),
        "cliproxy_url": ("CLIPROXY_URL",),
        "comfyui_request_timeout": ("COMFYUI_REQUEST_TIMEOUT",),
        "generation_timeout": ("GENERATION_TIMEOUT",),
        "hf_token": ("HF_TOKEN",),
        "generation_queue_limit": ("GENERATION_QUEUE_LIMIT",),
        "maximum_request_mib": ("MAXIMUM_REQUEST_MIB",),
        "modal_gpu": ("MODAL_GPU",),
        "modal_minimum_containers": ("MODAL_MIN_CONTAINERS",),
        "modal_model_volume": ("MODAL_MODEL_VOLUME",),
        "modal_scaledown_window": ("MODAL_SCALEDOWN_WINDOW",),
        "modal_token_id": ("MODAL_TOKEN_ID",),
        "modal_token_secret": ("MODAL_TOKEN_SECRET",),
        "model_profiles": ("MODEL_PROFILES",),
        "runpod_api_key": ("RUNPOD_API_KEY",),
        "runpod_data_centres": ("RUNPOD_DATA_CENTRES",),
        "runpod_gpu_types": ("RUNPOD_GPU_TYPES",),
        "runpod_maximum_workers": ("RUNPOD_MAXIMUM_WORKERS",),
        "salad_api_key": ("SALAD_API_KEY",),
        "salad_gpu_classes": ("SALAD_GPU_CLASSES",),
        "salad_organisation": ("SALAD_ORGANISATION",),
        "salad_project": ("SALAD_PROJECT",),
        "vast_api_key": ("VAST_API_KEY",),
        "vast_maximum_workers": ("VAST_MAXIMUM_WORKERS",),
        "vast_minimum_gpu_memory_gb": (
            "VAST_MINIMUM_GPU_RAM_GB",
            "VAST_MINIMUM_GPU_RAM_MB",
        ),
        "worker_api_key": ("WORKER_API_KEY",),
        "worker_image": ("WORKER_IMAGE",),
    }
    FIELD_METADATA: ClassVar[dict[str, dict[str, object]]] = {
        "civitai_token": {
            "label": "Civitai token",
            "section": "Credentials",
            "secret": True,
        },
        "cliproxy_api_key": {
            "label": "CLI Proxy API key",
            "section": "Credentials",
            "secret": True,
        },
        "cliproxy_management_key": {
            "label": "CLI Proxy API management key",
            "section": "Credentials",
            "secret": True,
        },
        "hf_token": {
            "label": "Hugging Face token",
            "section": "Credentials",
            "secret": True,
        },
        "modal_token_id": {
            "label": "Modal token ID",
            "section": "Credentials",
            "secret": True,
        },
        "modal_token_secret": {
            "label": "Modal token secret",
            "section": "Credentials",
            "secret": True,
        },
        "runpod_api_key": {
            "label": "RunPod API key",
            "section": "Credentials",
            "secret": True,
        },
        "salad_api_key": {
            "label": "SaladCloud API key",
            "section": "Credentials",
            "secret": True,
        },
        "vast_api_key": {
            "label": "Vast.ai API key",
            "section": "Credentials",
            "secret": True,
        },
        "worker_api_key": {
            "label": "Worker API key",
            "section": "Credentials",
            "secret": True,
        },
        "cliproxy_url": {
            "label": "CLI Proxy API URL",
            "section": "Providers",
            "type": "url",
        },
        "salad_organisation": {
            "label": "SaladCloud organisation",
            "section": "Providers",
        },
        "salad_project": {"label": "SaladCloud project", "section": "Providers"},
        "modal_gpu": {"label": "Modal GPU", "section": "Deployment"},
        "modal_minimum_containers": {
            "label": "Modal minimum containers",
            "minimum": 0,
            "section": "Deployment",
            "type": "number",
        },
        "modal_model_volume": {"label": "Modal model volume", "section": "Deployment"},
        "modal_scaledown_window": {
            "label": "Modal scaledown window (seconds)",
            "minimum": 1,
            "section": "Deployment",
            "type": "number",
        },
        "runpod_data_centres": {
            "label": "RunPod data centres",
            "section": "Deployment",
            "type": "list",
        },
        "runpod_gpu_types": {
            "label": "RunPod GPU types",
            "section": "Deployment",
            "type": "list",
        },
        "runpod_maximum_workers": {
            "label": "RunPod maximum workers",
            "minimum": 1,
            "section": "Deployment",
            "type": "number",
        },
        "salad_gpu_classes": {
            "label": "SaladCloud GPU classes",
            "section": "Deployment",
            "type": "list",
        },
        "vast_maximum_workers": {
            "label": "Vast.ai maximum workers",
            "minimum": 1,
            "section": "Deployment",
            "type": "number",
        },
        "vast_minimum_gpu_memory_gb": {
            "label": "Vast.ai minimum GPU memory (GB)",
            "minimum": 1,
            "section": "Deployment",
            "type": "number",
        },
        "worker_image": {"label": "Worker image", "section": "Deployment"},
        "comfyui_request_timeout": {
            "label": "ComfyUI request timeout (seconds)",
            "minimum": 0.1,
            "section": "Worker",
            "type": "number",
        },
        "generation_timeout": {
            "label": "Generation timeout (seconds)",
            "minimum": 0.1,
            "section": "Worker",
            "type": "number",
        },
        "generation_queue_limit": {
            "label": "Generation queue limit",
            "minimum": 1,
            "section": "Worker",
            "type": "number",
        },
        "maximum_request_mib": {
            "label": "Maximum request size (MiB)",
            "minimum": 1,
            "section": "Worker",
            "type": "number",
        },
        "model_profiles": {
            "label": "Worker model packages",
            "section": "Worker",
            "type": "models",
        },
        "routes": {"label": "Provider routes", "section": "Routing", "type": "routes"},
    }
    SECRET_FIELDS: ClassVar[frozenset[str]] = frozenset(
        name for name, metadata in FIELD_METADATA.items() if metadata.get("secret")
    )

    civitai_token: str = ""
    cliproxy_api_key: str = ""
    cliproxy_management_key: str = ""
    hf_token: str = ""
    modal_token_id: str = ""
    modal_token_secret: str = ""
    runpod_api_key: str = ""
    salad_api_key: str = ""
    vast_api_key: str = ""
    worker_api_key: str = ""
    cliproxy_url: str = ""
    salad_organisation: str = ""
    salad_project: str = ""
    modal_gpu: str = "L40S"
    modal_minimum_containers: int = 0
    modal_model_volume: str = "comfy-control-models"
    modal_scaledown_window: int = 60
    runpod_data_centres: list[str] = Field(default_factory=list)
    runpod_gpu_types: list[str] = Field(default_factory=list)
    runpod_maximum_workers: int = 1
    salad_gpu_classes: list[str] = Field(default_factory=list)
    vast_maximum_workers: int = 1
    vast_minimum_gpu_memory_gb: int = 24
    worker_image: str = DEFAULT_WORKER_IMAGE
    comfyui_request_timeout: float = 60
    generation_timeout: float = 900
    generation_queue_limit: int = 8
    maximum_request_mib: int = 100
    model_profiles: list[str] = Field(default_factory=lambda: ["flux-2-klein-9b"])
    routes: dict[str, list[RoutePreference]] = Field(default_factory=dict)

    @property
    def maximum_request_bytes(self) -> int:
        return self.maximum_request_mib * 1024 * 1024

    @field_validator(
        "runpod_data_centres",
        "runpod_gpu_types",
        "salad_gpu_classes",
        "model_profiles",
    )
    @classmethod
    def clean_list(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @field_validator("cliproxy_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("URL must use HTTP or HTTPS")
        return value

    @field_validator("routes")
    @classmethod
    def validate_routes(
        cls, value: dict[str, list[RoutePreference]]
    ) -> dict[str, list[RoutePreference]]:
        if set(value) - {"images", "videos"}:
            raise ValueError("provider routes only support images and videos")
        for family, targets in value.items():
            routes = [(target.provider, target.model) for target in targets]
            if len(routes) != len(set(routes)):
                raise ValueError(f"{family} provider routes must be unique")
            if any(not target.provider or not target.model for target in targets):
                raise ValueError(f"{family} provider routes require provider and model")
        return value

    @model_validator(mode="after")
    def validate_preferences(self) -> ControlPreferences:
        positive = (
            self.comfyui_request_timeout,
            self.generation_timeout,
            self.generation_queue_limit,
            self.maximum_request_mib,
            self.modal_scaledown_window,
            self.runpod_maximum_workers,
            self.vast_maximum_workers,
            self.vast_minimum_gpu_memory_gb,
        )
        if any(value <= 0 for value in positive) or self.modal_minimum_containers < 0:
            raise ValueError("numeric configuration values must be positive")
        if bool(self.cliproxy_api_key) != bool(self.cliproxy_url):
            raise ValueError("CLI Proxy API URL and API key must both be configured")
        if bool(self.modal_token_id) != bool(self.modal_token_secret):
            raise ValueError("Modal token ID and secret must both be configured")
        if self.salad_api_key and not (self.salad_organisation and self.salad_project):
            raise ValueError(
                "SaladCloud organisation and project are required with its API key"
            )
        if not self.worker_image.strip():
            raise ValueError("worker image must not be empty")
        return self

    @classmethod
    def from_environment(cls) -> ControlPreferences:
        def values(name: str) -> list[str]:
            return [
                item.strip() for item in os.getenv(name, "").split(",") if item.strip()
            ]

        minimum_gpu_memory = os.getenv("VAST_MINIMUM_GPU_RAM_GB")
        if minimum_gpu_memory is None and os.getenv("VAST_MINIMUM_GPU_RAM_MB"):
            minimum_gpu_memory = str(
                max(1, int(os.environ["VAST_MINIMUM_GPU_RAM_MB"]) // 1000)
            )
        return cls(
            civitai_token=os.getenv("CIVITAI_TOKEN", ""),
            cliproxy_api_key=os.getenv("CLIPROXY_API_KEY", ""),
            cliproxy_management_key=os.getenv("CLIPROXY_MANAGEMENT_KEY", ""),
            cliproxy_url=os.getenv("CLIPROXY_URL", ""),
            comfyui_request_timeout=float(os.getenv("COMFYUI_REQUEST_TIMEOUT", "60")),
            generation_timeout=float(os.getenv("GENERATION_TIMEOUT", "900")),
            hf_token=os.getenv("HF_TOKEN", ""),
            generation_queue_limit=int(os.getenv("GENERATION_QUEUE_LIMIT", "8")),
            maximum_request_mib=int(os.getenv("MAXIMUM_REQUEST_MIB", "100")),
            modal_gpu=os.getenv("MODAL_GPU", "L40S"),
            modal_minimum_containers=int(os.getenv("MODAL_MIN_CONTAINERS", "0")),
            modal_model_volume=os.getenv("MODAL_MODEL_VOLUME", "comfy-control-models"),
            modal_scaledown_window=int(os.getenv("MODAL_SCALEDOWN_WINDOW", "60")),
            modal_token_id=os.getenv("MODAL_TOKEN_ID", ""),
            modal_token_secret=os.getenv("MODAL_TOKEN_SECRET", ""),
            model_profiles=(
                values("MODEL_PROFILES")
                if "MODEL_PROFILES" in os.environ
                else ["flux-2-klein-9b"]
            ),
            runpod_api_key=os.getenv("RUNPOD_API_KEY", ""),
            runpod_data_centres=values("RUNPOD_DATA_CENTRES"),
            runpod_gpu_types=values("RUNPOD_GPU_TYPES"),
            runpod_maximum_workers=int(os.getenv("RUNPOD_MAXIMUM_WORKERS", "1")),
            salad_api_key=os.getenv("SALAD_API_KEY", ""),
            salad_gpu_classes=values("SALAD_GPU_CLASSES"),
            salad_organisation=os.getenv("SALAD_ORGANISATION", ""),
            salad_project=os.getenv("SALAD_PROJECT", ""),
            vast_api_key=os.getenv("VAST_API_KEY", ""),
            vast_maximum_workers=int(os.getenv("VAST_MAXIMUM_WORKERS", "1")),
            vast_minimum_gpu_memory_gb=int(minimum_gpu_memory or "24"),
            worker_api_key=os.getenv("WORKER_API_KEY", ""),
            worker_image=os.getenv("WORKER_IMAGE")
            or revision_worker_image()
            or DEFAULT_WORKER_IMAGE,
        )

    @classmethod
    def environment_overrides(cls) -> dict[str, object]:
        configured = cls.from_environment().model_dump()
        overrides = {
            field: configured[field]
            for field, names in cls.ENVIRONMENT_FIELDS.items()
            if any(name in os.environ for name in names)
        }
        if "WORKER_IMAGE" not in os.environ and revision_worker_image() is not None:
            overrides["worker_image"] = configured["worker_image"]
        return overrides

    def environment(self) -> dict[str, str]:
        values = {
            "CIVITAI_TOKEN": self.civitai_token,
            "CLIPROXY_API_KEY": self.cliproxy_api_key,
            "CLIPROXY_MANAGEMENT_KEY": self.cliproxy_management_key,
            "CLIPROXY_URL": self.cliproxy_url,
            "HF_TOKEN": self.hf_token,
            "COMFYUI_REQUEST_TIMEOUT": str(self.comfyui_request_timeout),
            "GENERATION_TIMEOUT": str(self.generation_timeout),
            "GENERATION_QUEUE_LIMIT": str(self.generation_queue_limit),
            "MAXIMUM_REQUEST_MIB": str(self.maximum_request_mib),
            "MODAL_GPU": self.modal_gpu,
            "MODAL_MIN_CONTAINERS": str(self.modal_minimum_containers),
            "MODAL_MODEL_VOLUME": self.modal_model_volume,
            "MODAL_SCALEDOWN_WINDOW": str(self.modal_scaledown_window),
            "MODAL_TOKEN_ID": self.modal_token_id,
            "MODAL_TOKEN_SECRET": self.modal_token_secret,
            "MODEL_PROFILES": ",".join(self.model_profiles),
            "RUNPOD_API_KEY": self.runpod_api_key,
            "RUNPOD_DATA_CENTRES": ",".join(self.runpod_data_centres),
            "RUNPOD_GPU_TYPES": ",".join(self.runpod_gpu_types),
            "RUNPOD_MAXIMUM_WORKERS": str(self.runpod_maximum_workers),
            "SALAD_API_KEY": self.salad_api_key,
            "SALAD_GPU_CLASSES": ",".join(self.salad_gpu_classes),
            "SALAD_ORGANISATION": self.salad_organisation,
            "SALAD_PROJECT": self.salad_project,
            "VAST_API_KEY": self.vast_api_key,
            "VAST_MAXIMUM_WORKERS": str(self.vast_maximum_workers),
            "VAST_MINIMUM_GPU_MEMORY_GB": str(self.vast_minimum_gpu_memory_gb),
            "WORKER_API_KEY": self.worker_api_key,
            "WORKER_IMAGE": self.worker_image,
        }
        return {
            name: value
            for name, value in values.items()
            if value != "" or name == "MODEL_PROFILES"
        }


class ConfigurationManager:
    def __init__(
        self,
        store: ControlStore,
        secret_key: str,
        initial: ControlPreferences | None = None,
        environment_overrides: Mapping[str, Any] | None = None,
    ):
        key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
        self.cipher = Fernet(key)
        self.environment_overrides = dict(environment_overrides or {})
        self.locked_fields = frozenset(self.environment_overrides)
        self.store = store
        loaded = store.configuration()
        if loaded is None:
            self.revision = 1
            stored_preferences = initial or ControlPreferences()
            self._save(stored_preferences, expected_revision=0)
        else:
            revision, document, secrets = loaded
            for name, encrypted in secrets.items():
                try:
                    document[name] = self.cipher.decrypt(encrypted.encode()).decode()
                except InvalidToken as exc:
                    raise ValueError(
                        f"stored configuration secret cannot be decrypted: {name}"
                    ) from exc
            stored_preferences = ControlPreferences.model_validate(document)
            self.revision = revision
        self.stored_preferences = stored_preferences
        self.preferences = self._effective(stored_preferences)

    def _effective(self, stored: ControlPreferences) -> ControlPreferences:
        return ControlPreferences.model_validate(
            stored.model_dump() | self.environment_overrides
        )

    def _save(self, preferences: ControlPreferences, expected_revision: int) -> None:
        document = preferences.model_dump()
        secrets = {
            name: self.cipher.encrypt(str(document.pop(name)).encode()).decode()
            for name in sorted(ControlPreferences.SECRET_FIELDS)
            if document.get(name)
        }
        try:
            self.revision = self.store.save_configuration(
                document, secrets, expected_revision=expected_revision
            )
        except RuntimeError as exc:
            raise ConfigurationConflict(
                "configuration changed; reload before saving"
            ) from exc

    def describe(self) -> dict[str, object]:
        values = self.preferences.model_dump()
        fields = []
        for name, metadata in ControlPreferences.FIELD_METADATA.items():
            value: object = values[name]
            if name in ControlPreferences.SECRET_FIELDS:
                value = None
            fields.append(
                {
                    "configured": bool(values[name]),
                    "locked": name in self.locked_fields,
                    "name": name,
                    "value": value,
                    **metadata,
                }
            )
        return {"fields": fields, "revision": self.revision}

    def prepare_update(
        self, values: Mapping[str, Any], revision: int
    ) -> ControlPreferences:
        if revision != self.revision:
            raise ConfigurationConflict("configuration changed; reload before saving")
        unknown = sorted(set(values) - set(ControlPreferences.model_fields))
        if unknown:
            raise ValueError(f"unknown configuration fields: {', '.join(unknown)}")
        locked = sorted(set(values) & self.locked_fields)
        if locked:
            raise ValueError(
                "environment-controlled configuration fields cannot be changed: "
                + ", ".join(locked)
            )
        document = self.preferences.model_dump()
        for name, value in values.items():
            if name in ControlPreferences.SECRET_FIELDS and value == "":
                continue
            document[name] = "" if value is None else value
        return ControlPreferences.model_validate(document)

    def save(self, preferences: ControlPreferences, revision: int) -> None:
        document = preferences.model_dump()
        stored = self.stored_preferences.model_dump()
        for name in self.locked_fields:
            document[name] = stored[name]
        stored_preferences = ControlPreferences.model_validate(document)
        self._save(stored_preferences, expected_revision=revision)
        self.stored_preferences = stored_preferences
        self.preferences = self._effective(stored_preferences)
