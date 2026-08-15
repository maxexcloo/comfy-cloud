from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from catalogue.workflows import Operation
from worker.config import required_secret


class ProviderAction(BaseModel):
    method: Literal["DELETE", "GET", "PATCH", "POST", "PUT"] = "POST"
    url: str | None = None
    confirmation: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    internal: (
        Literal[
            "modal-deploy",
            "modal-terminate",
            "provider-deploy",
            "provider-terminate",
        ]
        | None
    ) = None
    json_body: dict[str, Any] | None = Field(default=None, alias="json")
    resource_id_path: str | None = None

    @model_validator(mode="after")
    def validate_action(self) -> ProviderAction:
        if bool(self.url) == bool(self.internal):
            raise ValueError("action requires exactly one of url or internal")
        if self.url and not self.url.startswith(("http://", "https://")):
            raise ValueError("action url must use HTTP or HTTPS")
        return self


class ProviderLifecycle(BaseModel):
    start: ProviderAction | None = None
    stop: ProviderAction | None = None


class UsageProbe(BaseModel):
    kind: Literal["cliproxyapi", "modal", "runpod", "salad", "vast"]
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_probe(self) -> UsageProbe:
        if self.kind not in {"modal", "salad"} and not self.url:
            raise ValueError(f"{self.kind} usage requires a URL")
        if self.url and not self.url.startswith(("http://", "https://")):
            raise ValueError("usage URL must use HTTP or HTTPS")
        return self


class ProviderManagement(BaseModel):
    kind: Literal[
        "modal",
        "runpod",
        "runpod-pod",
        "salad",
        "vast",
        "vast-pod",
    ]
    name: str
    function: str | None = None
    organisation: str | None = None
    port: int = 8000
    project: str | None = None

    @model_validator(mode="after")
    def validate_management(self) -> ProviderManagement:
        if not self.name:
            raise ValueError("managed provider name must not be empty")
        if self.port < 1 or self.port > 65535:
            raise ValueError("managed provider port must be between 1 and 65535")
        if self.kind == "modal" and not self.function:
            raise ValueError("modal management requires a function")
        return self


class Provider(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    id: str
    api_key: str
    aliases: list[str] = Field(default_factory=list)
    base_url: str | None = None
    health_path: str = "/health/ready"
    idle_seconds: int = 600
    request_timeout: float = 1200
    resource_id: str | None = None
    startup_timeout: float = 900
    actions: dict[str, ProviderAction] = Field(default_factory=dict)
    lifecycle: ProviderLifecycle = Field(default_factory=ProviderLifecycle)
    management: ProviderManagement | None = None
    platform: str | None = None
    type: Literal["pod", "proxy", "serverless"] = "pod"
    usage: UsageProbe | None = None

    @model_validator(mode="after")
    def validate_provider(self) -> Provider:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", self.id):
            raise ValueError("provider id must be a lowercase slug")
        if any(not re.fullmatch(r"[a-z][a-z0-9-]*", alias) for alias in self.aliases):
            raise ValueError("provider aliases must be lowercase slugs")
        if len(self.aliases) != len(set(self.aliases)) or self.id in self.aliases:
            raise ValueError("provider aliases must be unique and differ from the id")
        if self.base_url is None and self.management is None:
            raise ValueError("provider requires base_url or management")
        if self.base_url is not None and not self.base_url.startswith(
            ("http://", "https://")
        ):
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
        if self.base_url is not None:
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
    model_config = ConfigDict(hide_input_in_errors=True)

    models: list[RoutedModel]
    providers: list[Provider]

    @model_validator(mode="after")
    def validate_relationships(self) -> ControlFile:
        provider_ids = [provider.id for provider in self.providers]
        provider_names = [
            name
            for provider in self.providers
            for name in (provider.id, *provider.aliases)
        ]
        model_ids = [model.id for model in self.models]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("provider ids must be unique")
        if len(provider_names) != len(set(provider_names)):
            raise ValueError("provider ids and aliases must be unique")
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
    def load(
        cls, path: Path, environment: Mapping[str, str] | None = None
    ) -> ControlFile:
        environment = os.environ if environment is None else environment
        raw = yaml.safe_load(path.read_text())
        providers = raw.get("providers", [])
        raw["providers"] = [
            {key: value for key, value in provider.items() if key != "enabled_if"}
            for provider in providers
            if not provider.get("enabled_if") or environment.get(provider["enabled_if"])
        ]
        enabled = {provider["id"] for provider in raw["providers"]}
        for model in raw.get("models", []):
            model["targets"] = [
                target
                for target in model.get("targets", [])
                if target["provider"] in enabled
            ]
        raw["models"] = [model for model in raw.get("models", []) if model["targets"]]
        return cls.model_validate(expand_environment(raw, environment))


def expand_environment(value: Any, environment: Mapping[str, str] | None = None) -> Any:
    environment = os.environ if environment is None else environment
    if isinstance(value, dict):
        return {
            key: expand_environment(item, environment) for key, item in value.items()
        }
    if isinstance(value, list):
        return [expand_environment(item, environment) for item in value]
    if isinstance(value, str) and value.startswith("env."):
        name = value[4:]
        resolved = environment.get(name)
        if resolved is None:
            raise ValueError(f"environment variable is not set: {name}")
        return resolved
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = environment.get(name)
            if resolved is None:
                raise ValueError(f"environment variable is not set: {name}")
            return resolved

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)
    return value


@dataclass(frozen=True)
class ControlSettings:
    api_key: str
    config_file: Path | None
    database_path: Path
    ui_password: str
    ui_username: str
    secret_key: str = "test-control-secret-key-at-least-32-characters"

    @classmethod
    def from_env(cls) -> ControlSettings:
        api_key = required_secret("CONTROL_API_KEY")
        secret_key = required_secret("CONTROL_SECRET_KEY")
        if len(secret_key) < 32:
            raise ValueError("CONTROL_SECRET_KEY must contain at least 32 characters")
        return cls(
            api_key=api_key,
            config_file=(
                Path(os.environ["CONTROL_CONFIG"])
                if os.getenv("CONTROL_CONFIG")
                else None
            ),
            database_path=Path(os.getenv("CONTROL_DATABASE", "/data/comfy-control.db")),
            secret_key=secret_key,
            ui_password=required_secret("CONTROL_UI_PASSWORD"),
            ui_username=os.getenv("CONTROL_UI_USERNAME", "comfy"),
        )
