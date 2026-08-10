from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from .catalogue import Operation
from .config import required_secret


class LifecycleAction(BaseModel):
    method: Literal["DELETE", "GET", "PATCH", "POST", "PUT"] = "POST"
    url: str
    confirmation: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    json_body: dict[str, Any] | None = Field(default=None, alias="json")

    @model_validator(mode="after")
    def validate_action(self) -> LifecycleAction:
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("action url must use HTTP or HTTPS")
        return self


class ProviderLifecycle(BaseModel):
    start: LifecycleAction | None = None
    stop: LifecycleAction | None = None


class Provider(BaseModel):
    id: str
    base_url: str
    api_key: str
    health_path: str = "/health/ready"
    idle_seconds: int = 600
    request_timeout: float = 1200
    startup_timeout: float = 900
    actions: dict[str, LifecycleAction] = Field(default_factory=dict)
    lifecycle: ProviderLifecycle = Field(default_factory=ProviderLifecycle)

    @model_validator(mode="after")
    def validate_provider(self) -> Provider:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", self.id):
            raise ValueError("provider id must be a lowercase slug")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("provider base_url must use HTTP or HTTPS")
        if not self.api_key:
            raise ValueError("provider api_key must not be empty")
        if self.idle_seconds < 0:
            raise ValueError("idle_seconds must not be negative")
        if self.request_timeout <= 0 or self.startup_timeout < 0:
            raise ValueError("provider timeouts must not be negative")
        invalid_actions = sorted(
            name
            for name in self.actions
            if not re.fullmatch(r"[a-z][a-z0-9-]*", name) or name in {"start", "stop"}
        )
        if invalid_actions:
            raise ValueError(
                "provider action names must be lowercase slugs other than start/stop: "
                + ", ".join(invalid_actions)
            )
        self.base_url = self.base_url.rstrip("/")
        return self


class Target(BaseModel):
    model: str
    provider: str


class RoutedModel(BaseModel):
    id: str
    operation: Operation
    targets: list[Target]

    @model_validator(mode="after")
    def validate_model(self) -> RoutedModel:
        if not self.id or self.id.startswith("/") or ".." in self.id:
            raise ValueError("model id must be a stable relative identifier")
        if not self.targets:
            raise ValueError("model must have at least one target")
        return self


class ControlFile(BaseModel):
    models: list[RoutedModel]
    providers: list[Provider]

    @model_validator(mode="after")
    def validate_relationships(self) -> ControlFile:
        provider_ids = [provider.id for provider in self.providers]
        model_ids = [model.id for model in self.models]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("provider ids must be unique")
        if len(model_ids) != len(set(model_ids)):
            raise ValueError("model ids must be unique")
        unknown = sorted(
            {
                target.provider
                for model in self.models
                for target in model.targets
                if target.provider not in provider_ids
            }
        )
        if unknown:
            raise ValueError(
                f"models reference unknown providers: {', '.join(unknown)}"
            )
        return self

    @classmethod
    def load(cls, path: Path) -> ControlFile:
        raw = yaml.safe_load(path.read_text())
        return cls.model_validate(expand_environment(raw))


def expand_environment(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_environment(item) for item in value]
    if isinstance(value, str) and value.startswith("env."):
        name = value[4:]
        resolved = os.getenv(name)
        if resolved is None:
            raise ValueError(f"environment variable is not set: {name}")
        return resolved
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = os.getenv(name)
            if resolved is None:
                raise ValueError(f"environment variable is not set: {name}")
            return resolved

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
    return value


@dataclass(frozen=True)
class ControlSettings:
    api_key: str
    config_file: Path
    database_path: Path
    maximum_request_bytes: int
    ui_password: str
    ui_username: str

    @classmethod
    def from_env(cls) -> ControlSettings:
        maximum_request_bytes = int(
            os.getenv("CONTROL_MAXIMUM_REQUEST_BYTES", str(100 * 1024 * 1024))
        )
        if maximum_request_bytes < 1:
            raise ValueError("CONTROL_MAXIMUM_REQUEST_BYTES must be at least 1")
        return cls(
            api_key=required_secret("CONTROL_API_KEY"),
            config_file=Path(os.getenv("CONTROL_CONFIG", "/config/control.yaml")),
            database_path=Path(os.getenv("CONTROL_DATABASE", "/data/comfy-control.db")),
            maximum_request_bytes=maximum_request_bytes,
            ui_password=required_secret("CONTROL_UI_PASSWORD"),
            ui_username=os.getenv("CONTROL_UI_USERNAME", "comfy"),
        )
