from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx

from comfy_control.control import registry as control_registry
from comfy_control.control.config import (
    ControlFile,
    ControlSettings,
    Provider,
    ProviderAction,
    RoutedModel,
    Target,
)
from comfy_control.control.http import exception_message
from comfy_control.control.media import ControlMedia
from comfy_control.control.preferences import ConfigurationManager, ControlPreferences
from comfy_control.control.store import ControlStore, Job
from comfy_control.providers.base import StartRecovery
from comfy_control.providers.cliproxy import CliproxyClient
from comfy_control.providers.deployment import deployment_options
from comfy_control.providers.deployment.common import DeploymentSelection
from comfy_control.providers.modal import (
    provider_action as modal_provider_action,
)
from comfy_control.providers.registry import (
    ProviderNotDeployed,
    available_provider_actions,
    provider_adapter,
    provider_panel_url,
)

SENSITIVE_FIELD_PARTS = (
    "accesskey",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)


def sensitive_field(name: str) -> bool:
    normalised = "".join(character for character in name.lower() if character.isalnum())
    return any(part in normalised for part in SENSITIVE_FIELD_PARTS)


def redacted(value: object) -> object:
    if isinstance(value, dict):
        environment_name = value.get("key")
        redact_environment_value = isinstance(
            environment_name, str
        ) and sensitive_field(environment_name)
        return {
            key: (
                "***"
                if (key == "value" and redact_environment_value) or sensitive_field(key)
                else redacted(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redacted(item) for item in value]
    return value


def history_parameters(value: object, key: str = "") -> object:
    if sensitive_field(key):
        return "***"
    if isinstance(value, dict):
        return {name: history_parameters(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [history_parameters(item, key) for item in value]
    if isinstance(value, str) and key.lower() in {
        "image",
        "images",
        "input_image",
        "mask",
    }:
        if value.startswith(("http://", "https://")):
            return value
        return f"<embedded media omitted: {len(value)} characters>"
    if isinstance(value, str) and len(value) > 64 * 1024:
        return f"<value omitted: {len(value)} characters>"
    return value


@dataclass
class ProviderRuntime:
    config: Provider
    client: httpx.AsyncClient
    active_requests: int = 0
    base_url: str | None = None
    last_used: float = field(default_factory=time.monotonic)
    lifecycle_revision: int = 0
    ready: bool = False
    state: str = "unknown"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    usage: dict[str, object] = field(default_factory=lambda: {"status": "unconfigured"})
    usage_checked_at: float = 0
    usage_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Controller:
    def __init__(self, settings: ControlSettings):
        self.settings = settings
        self.store = ControlStore(settings.database_path)
        initial_preferences = ControlPreferences()
        initial_preferences = ControlPreferences.model_validate(
            initial_preferences.model_dump()
            | {
                "routes": (
                    self.file_routes(settings.config_file)
                    if settings.config_file is not None
                    else control_registry.routes()
                )
            }
        )
        self.configuration = ConfigurationManager(
            self.store,
            settings.secret_key,
            initial_preferences,
            ControlPreferences.environment_overrides(),
        )
        self.preferences = self.configuration.preferences
        self.configure_modal_auth(self.preferences)
        self.available_providers = self.load_provider_catalogue()
        self.config = self.load_control_file(self.preferences)
        self.models = {model.id: model for model in self.config.models}
        self.providers = self.build_providers(self.config)
        self.provider_ids = self.build_provider_ids(self.config)
        self.reconcile_history_routes()
        self.configuration_lock = asyncio.Lock()
        self.lifecycle_client = httpx.AsyncClient(timeout=60)
        self.media = ControlMedia(self, settings.database_path.parent)
        self.video_tasks: set[asyncio.Task[None]] = set()

    def reconcile_history_routes(self) -> None:
        for row in self.store.history_routes():
            try:
                parameters = json.loads(str(row["parameters_json"]))
            except json.JSONDecodeError:
                continue
            requested_model = (
                str(parameters.get("model") or row["model"])
                if isinstance(parameters, dict)
                else str(row["model"])
            )
            requested_provider = (
                str(parameters.get("provider") or row["provider"] or "")
                if isinstance(parameters, dict)
                else str(row["provider"] or "")
            )
            try:
                _, targets = self.resolve_model(
                    requested_model,
                    str(row["operation"]),
                    requested_provider or None,
                )
            except (KeyError, ValueError):
                targets = [
                    target
                    for candidate in self.config.models
                    if candidate.operation == str(row["operation"])
                    and any(
                        candidate_target.model == requested_model
                        for candidate_target in candidate.targets
                    )
                    for target in candidate.targets
                    if target.provider in {requested_provider, str(row["provider"])}
                ]
                if not targets:
                    continue
            selected = next(
                (
                    target
                    for target in targets
                    if target.provider == str(row["provider"])
                ),
                targets[0],
            )
            self.store.reconcile_history_route(
                str(row["id"]),
                requested_model,
                selected.provider,
                selected.model,
            )

    def build_providers(self, config: ControlFile) -> dict[str, ProviderRuntime]:
        return {
            provider.id: ProviderRuntime(
                config=provider,
                base_url=provider.base_url,
                client=httpx.AsyncClient(
                    follow_redirects=False, timeout=provider.request_timeout
                ),
            )
            for provider in config.providers
        }

    def load_provider_catalogue(self) -> list[dict[str, str]]:
        if self.settings.config_file is None:
            return [dict(provider) for provider in control_registry.PROVIDER_CATALOGUE]
        import yaml

        value = yaml.safe_load(self.settings.config_file.read_text())
        providers = value.get("providers", []) if isinstance(value, dict) else []
        return [
            {
                "id": str(provider["id"]),
                "platform": str(provider.get("platform") or provider["id"]),
                "type": str(provider.get("type", "pod")),
            }
            for provider in providers
            if isinstance(provider, dict) and provider.get("id")
        ]

    def build_provider_ids(self, config: ControlFile) -> dict[str, str]:
        return {
            name: provider.id
            for provider in config.providers
            for name in (provider.id, *provider.aliases)
        }

    def load_control_file(self, preferences: ControlPreferences) -> ControlFile:
        if self.settings.config_file is None:
            return control_registry.control_file(
                preferences.environment(), preferences.routes
            )
        config = ControlFile.load(self.settings.config_file, preferences.environment())
        models = []
        for model in config.models:
            family = control_registry.ROUTE_FAMILIES.get(
                model.id,
                "videos" if model.operation == "video_generation" else "images",
            )
            route = preferences.routes.get(family)
            if route is None:
                models.append(model)
                continue
            targets = {target.provider: target for target in model.targets}
            selected = [
                targets[choice.provider]
                for choice in route
                if choice.provider in targets
            ]
            if selected:
                models.append(model.model_copy(update={"targets": selected}))
        return config.model_copy(update={"models": models})

    @staticmethod
    def file_routes(path: Path) -> dict[str, list[dict[str, str]]]:
        import yaml

        raw = yaml.safe_load(path.read_text())
        models = raw.get("models", []) if isinstance(raw, dict) else []
        routes: dict[str, list[str]] = {}
        for model in models:
            if not isinstance(model, dict) or not model.get("id"):
                continue
            operation = str(model.get("operation", ""))
            family = "videos" if operation == "video_generation" else "images"
            providers = [
                {
                    "model": str(target.get("model", "")),
                    "provider": str(target["provider"]),
                }
                for target in model.get("targets", [])
                if isinstance(target, dict) and target.get("provider")
            ]
            routes.setdefault(family, providers)
        return routes

    def describe_configuration(self) -> dict[str, object]:
        description = self.configuration.describe()
        fields = description.get("fields")
        if not isinstance(fields, list):
            return description
        for item in fields:
            if not isinstance(item, dict):
                continue
            if item["name"] == "routes":
                if not item["value"]:
                    item["value"] = control_registry.routes()
                item["providers"] = [
                    provider["id"] for provider in self.available_providers
                ]
                route_models = {
                    family: {
                        provider: [
                            package
                            for package in packages
                            if any(
                                control_registry.ROUTE_FAMILIES[operation] == family
                                for operation in control_registry.MODEL_PACKAGES[
                                    package
                                ]
                            )
                        ]
                        for provider, packages in control_registry.PROVIDER_MODEL_PACKAGES.items()
                    }
                    for family in ("images", "videos")
                }
                for family, targets in self.preferences.routes.items():
                    for target in targets:
                        available = route_models[family].setdefault(target.provider, [])
                        if target.model not in available:
                            available.append(target.model)
                item["models"] = route_models
                item["installed_models"] = self.preferences.model_profiles
            if item["name"] == "model_profiles":
                item["options"] = sorted(
                    package
                    for package in control_registry.MODEL_PACKAGES
                    if package != "grok-imagine"
                )
        return description

    @staticmethod
    def configure_modal_auth(preferences: ControlPreferences) -> None:
        for name, value in (
            ("MODAL_TOKEN_ID", preferences.modal_token_id),
            ("MODAL_TOKEN_SECRET", preferences.modal_token_secret),
        ):
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)

    async def update_preferences(
        self, values: dict[str, object], revision: int
    ) -> None:
        async with self.configuration_lock:
            if any(runtime.active_requests for runtime in self.providers.values()):
                raise RuntimeError(
                    "configuration cannot change while requests are active"
                )
            preferences = self.configuration.prepare_update(values, revision)
            config = self.load_control_file(preferences)
            providers = self.build_providers(config)
            old_providers = self.providers
            try:
                self.configuration.save(preferences, revision)
            except Exception:
                await asyncio.gather(
                    *(runtime.client.aclose() for runtime in providers.values())
                )
                raise
            self.configure_modal_auth(preferences)
            self.preferences = preferences
            self.config = config
            self.models = {model.id: model for model in config.models}
            self.providers = providers
            self.provider_ids = self.build_provider_ids(config)
            await asyncio.gather(
                *(runtime.client.aclose() for runtime in old_providers.values())
            )

    async def provider_usage(self, runtime: ProviderRuntime) -> dict[str, object]:
        probe = runtime.config.usage
        if probe is None:
            return runtime.usage
        if (
            runtime.usage_checked_at
            and time.monotonic() - runtime.usage_checked_at < 60
        ):
            return runtime.usage
        async with runtime.usage_lock:
            if (
                runtime.usage_checked_at
                and time.monotonic() - runtime.usage_checked_at < 60
            ):
                return runtime.usage
            try:
                adapter = provider_adapter(runtime.config)
                if adapter is None:
                    raise RuntimeError(
                        f"provider adapter is missing: {runtime.config.id}"
                    )
                metrics = await adapter.usage(
                    self.lifecycle_client,
                    runtime.config,
                    self.preferences,
                    self.store.history_usage,
                )
                runtime.usage = {
                    "metrics": sorted(
                        metrics, key=lambda metric: str(metric["label"]).casefold()
                    ),
                    "status": "ok",
                }
            except Exception as exc:  # noqa: BLE001 - optional account telemetry
                runtime.usage = {
                    "error": exception_message(exc),
                    "status": "unavailable",
                }
            runtime.usage_checked_at = time.monotonic()
            return runtime.usage

    async def usage(self) -> dict[str, dict[str, object]]:
        groups: dict[tuple[str, str | None], list[ProviderRuntime]] = {}
        for runtime in self.providers.values():
            probe = runtime.config.usage
            key = (
                (probe.kind, probe.url)
                if probe is not None
                else (runtime.config.id, None)
            )
            groups.setdefault(key, []).append(runtime)
        values = await asyncio.gather(
            *(self.bounded_provider_usage(runtimes[0]) for runtimes in groups.values())
        )
        result: dict[str, dict[str, object]] = {}
        checked_at = time.monotonic()
        for runtimes, value in zip(groups.values(), values, strict=True):
            for runtime in runtimes:
                runtime.usage = value
                runtime.usage_checked_at = checked_at
                result[runtime.config.id] = value
        return result

    async def bounded_provider_usage(
        self, runtime: ProviderRuntime
    ) -> dict[str, object]:
        try:
            return await asyncio.wait_for(self.provider_usage(runtime), timeout=8)
        except TimeoutError:
            return {"error": "account usage check timed out", "status": "unavailable"}

    async def provider_statuses(self) -> dict[str, dict[str, object]]:
        values = await asyncio.gather(
            *(
                self.bounded_provider_status(runtime)
                for runtime in self.providers.values()
            )
        )
        return dict(zip(self.providers, values, strict=True))

    async def bounded_provider_status(
        self, runtime: ProviderRuntime
    ) -> dict[str, object]:
        try:
            return await asyncio.wait_for(self.provider_status(runtime), timeout=8)
        except TimeoutError:
            runtime.state = "unavailable"
            return {
                "details": {},
                "error": "provider status check timed out",
                "panel_url": provider_panel_url(runtime.config, {}, runtime.base_url),
                "state": runtime.state,
                "status": "unavailable",
            }

    async def provider_status(self, runtime: ProviderRuntime) -> dict[str, object]:
        management = runtime.config.management
        details: dict[str, object] = {}
        try:
            resource = await self.refresh_endpoint(runtime, route=False)
            if management is None:
                response = await runtime.client.get(
                    self.worker_url(runtime, runtime.config.health_path),
                    headers=self.worker_headers(runtime),
                    timeout=4,
                )
                runtime.ready = response.is_success
                runtime.state = "ready" if runtime.ready else "unavailable"
            elif management.kind == "modal":
                adapter = provider_adapter(runtime.config)
                if adapter is None:
                    raise RuntimeError("Modal provider adapter is missing")
                runtime.state, details = await adapter.live_status(runtime.config)
            else:
                adapter = provider_adapter(runtime.config)
                if adapter is None:
                    raise RuntimeError(
                        f"provider adapter is missing: {management.kind}"
                    )
                runtime.state, details = adapter.status(resource)
            return {
                "details": redacted(details),
                "panel_url": provider_panel_url(
                    runtime.config, details, runtime.base_url
                ),
                "state": runtime.state,
                "status": "ok",
            }
        except ProviderNotDeployed:
            self.store.clear_provider_resource(runtime.config.id)
            runtime.base_url = None
            runtime.ready = False
            runtime.state = "not-deployed"
            return {
                "details": {},
                "panel_url": provider_panel_url(
                    runtime.config, details, runtime.base_url
                ),
                "state": runtime.state,
                "status": "ok",
            }
        except Exception as exc:  # noqa: BLE001 - provider control-plane boundary
            runtime.state = "unavailable"
            return {
                "details": {},
                "error": exception_message(exc),
                "panel_url": provider_panel_url(
                    runtime.config, details, runtime.base_url
                ),
                "state": runtime.state,
                "status": "unavailable",
            }

    async def provider_logs(self, provider: str, limit: int = 200) -> dict[str, object]:
        if provider not in self.providers:
            raise KeyError(f"unknown provider: {provider}")
        maximum = min(max(limit, 1), 500)
        entries: list[dict[str, object]] = [
            {**event, "source": "Controller"}
            for event in self.store.events(min(max(limit, 1), 500) * 10)
            if event.get("provider") == provider
        ][:maximum]
        runtime = self.providers[provider]
        worker_error: str | None = None
        try:
            if runtime.base_url is None:
                await self.refresh_endpoint(runtime)
            response = await runtime.client.get(
                self.worker_url(runtime, "/internal/logs"),
                headers=self.worker_headers(runtime),
                params={"limit": maximum},
                timeout=5,
            )
            response.raise_for_status()
            worker = response.json()
            worker_entries = worker.get("entries") if isinstance(worker, dict) else None
            if isinstance(worker_entries, list):
                entries.extend(
                    entry
                    for entry in worker_entries
                    if isinstance(entry, dict) and isinstance(entry.get("message"), str)
                )
        except Exception as exc:  # noqa: BLE001 - provider diagnostics boundary
            worker_error = exception_message(exc)
        entries.sort(key=lambda entry: int(entry.get("created_at", 0)), reverse=True)
        return {
            "entries": redacted(entries[:maximum]),
            "provider": provider,
            "source": "Controller and Worker",
            "worker_error": worker_error,
        }

    async def close(self) -> None:
        for task in self.video_tasks:
            task.cancel()
        await asyncio.gather(*self.video_tasks, return_exceptions=True)
        await asyncio.gather(
            *(runtime.client.aclose() for runtime in self.providers.values())
        )
        await self.lifecycle_client.aclose()
        await self.media.close()
        self.store.close()

    async def refresh_endpoint(
        self, runtime: ProviderRuntime, *, route: bool = True
    ) -> dict[str, object]:
        management = runtime.config.management
        if management is None:
            return {}
        adapter = provider_adapter(runtime.config)
        if adapter is None:
            raise RuntimeError(f"provider adapter is missing: {management.kind}")
        discovery = await adapter.discover(
            self.lifecycle_client,
            runtime.config,
            self.preferences,
            self.resource_id(runtime.config.id),
            route=route,
        )
        if discovery.base_url is not None:
            runtime.base_url = discovery.base_url
        self.store.save_provider_resource(runtime.config.id, discovery.resource_id)
        return discovery.resource

    def start_video(self, job: Job) -> None:
        task = asyncio.create_task(self.run_video(job))
        self.video_tasks.add(task)
        task.add_done_callback(self.video_tasks.discard)

    async def run_video(self, job: Job) -> None:
        request_id = job.id.removeprefix("video_")[:16]
        try:
            model = self.model(job.model, "video_generation")
            failures: list[str] = []
            targets = (
                [target for target in model.targets if target.provider == job.provider]
                if job.provider
                else model.targets
            )
            for target in targets:
                self.store.update_job(job.id, "in_progress", provider=target.provider)
                self.store.update_history(
                    job.id,
                    "in_progress",
                    provider=target.provider,
                    provider_model=target.model,
                )
                attempt_id = self.store.start_attempt(
                    job.id, target.provider, target.model
                )
                try:
                    response: httpx.Response | None = None
                    internal_outputs: list[tuple[bytes, str, str]] | None = None
                    if target.provider == "cliproxyapi":
                        data = await self.cliproxy_video(job, target)
                    else:
                        value = json.loads(job.request_json)
                        multipart = value.get("_control_multipart")
                        if multipart is None:
                            parameters = dict(value)
                            files = []
                        else:
                            parameters = dict(multipart["fields"])
                            files = [
                                (
                                    item["field"],
                                    (
                                        item["filename"],
                                        Path(item["path"]).read_bytes(),
                                        item["content_type"],
                                    ),
                                )
                                for item in multipart["files"]
                            ]
                        parameters.pop("model", None)
                        parameters.pop("provider", None)
                        if parameters.get("size") not in {None, "", "auto"}:
                            width, height = str(parameters.pop("size")).split("x", 1)
                            parameters["width"] = int(width)
                            parameters["height"] = int(height)
                        for name in ("height", "length", "seed", "steps", "width"):
                            if parameters.get(name) is not None:
                                parameters[name] = int(parameters[name])
                        if parameters.get("seconds") is not None:
                            seconds = float(parameters.pop("seconds"))
                            frames = max(5, round(seconds * 24))
                            parameters["length"] = frames + (5 - frames % 17) % 17
                        internal_outputs = await self.execute_internal(
                            target,
                            job.id,
                            "video_generation",
                            parameters,
                            files,
                        )
                        data = {
                            "id": job.id,
                            "model": job.model,
                            "status": "completed",
                        }
                except (httpx.HTTPError, RuntimeError, TimeoutError, ValueError) as exc:
                    message = exception_message(exc)
                    self.store.finish_attempt(attempt_id, "failed", message)
                    failures.append(f"{target.provider}: {message}")
                    self.store.event(
                        "error",
                        message,
                        provider=target.provider,
                        request_id=request_id,
                    )
                    continue
                if (response is None or response.is_success) and data.get(
                    "status"
                ) == "completed":
                    self.store.finish_attempt(attempt_id, "completed")
                    upstream_id = str(data.get("id", ""))
                    archive_data = dict(data)
                    data["id"] = job.id
                    data["model"] = job.model
                    self.store.update_job(
                        job.id,
                        "completed",
                        provider=target.provider,
                        upstream_id=upstream_id,
                        response_json=json.dumps(data, separators=(",", ":")),
                    )
                    self.store.update_history(
                        job.id, "completed", provider=target.provider
                    )
                    self.store.event(
                        "info",
                        "video completed",
                        provider=target.provider,
                        request_id=request_id,
                    )
                    self.remove_uploads(job.id)
                    if internal_outputs is None:
                        await self.media.archive_video(
                            job.id, target.provider, archive_data
                        )
                    else:
                        for content, content_type, filename in internal_outputs:
                            self.media.save(job.id, content, content_type, filename)
                    return
                message = data.get("error") or (
                    f"HTTP {response.status_code}"
                    if response is not None
                    else "video generation failed"
                )
                self.store.finish_attempt(attempt_id, "failed", str(message))
                failures.append(f"{target.provider}: {message}")
            failure = "; ".join(failures) or "all providers failed"
            self.store.update_job(job.id, "failed", error=failure)
            self.store.update_history(job.id, "failed", error=failure)
            self.remove_uploads(job.id)
        except Exception as exc:  # noqa: BLE001 - durable task boundary
            message = exception_message(exc)
            self.store.update_job(job.id, "failed", error=message)
            self.store.update_history(job.id, "failed", error=message)
            self.store.event("error", message, request_id=request_id)
            self.remove_uploads(job.id)

    async def cliproxy_video(self, job: Job, target: Target) -> dict[str, object]:
        runtime = self.providers[target.provider]
        if not runtime.base_url:
            raise RuntimeError("CLI Proxy API has no base URL")
        value = json.loads(job.request_json)
        multipart = value.pop("_control_multipart", None)
        if multipart is not None:
            value = dict(multipart["fields"])
            images = [item for item in multipart["files"] if item["field"] == "image"]
            if images:
                image = images[0]
                encoded = base64.b64encode(Path(image["path"]).read_bytes()).decode()
                value["image"] = {
                    "url": f"data:{image['content_type']};base64,{encoded}"
                }
        client = CliproxyClient(
            runtime.base_url,
            runtime.config.api_key,
            runtime.config.request_timeout,
        )
        try:
            output_url = await client.generate_video(value)
        finally:
            await client.close()
        return {
            "id": job.id,
            "model": target.model,
            "output_url": output_url,
            "status": "completed",
        }

    def remove_uploads(self, job_id: str) -> None:
        directory = self.media.uploads_path / job_id
        if not directory.is_dir():
            return
        for path in directory.iterdir():
            if path.is_file():
                path.unlink()
        directory.rmdir()

    async def action(
        self,
        action: ProviderAction,
        provider: str,
        action_name: str = "lifecycle",
        preferences: ControlPreferences | None = None,
        selection: DeploymentSelection | None = None,
    ) -> httpx.Response:
        selected_preferences = preferences or self.preferences
        if action_name == "deploy" and self.resource_id(provider) is not None:
            raise RuntimeError(f"provider {provider} is already deployed")
        if action.internal in {"modal-deploy", "modal-terminate"}:
            response = await asyncio.to_thread(
                modal_provider_action,
                action.internal,
                self.providers[provider].config,
                selected_preferences,
                self.settings,
            )
        elif action.internal == "provider-deploy":
            adapter = provider_adapter(self.providers[provider].config)
            if adapter is None:
                raise RuntimeError(f"provider adapter is missing: {provider}")
            response = await adapter.deploy(
                self.lifecycle_client,
                self.providers[provider].config,
                selected_preferences,
                self.settings,
                selection,
            )
        elif action.internal == "provider-terminate":
            resource_id = self.resource_id(provider)
            if resource_id is None:
                raise RuntimeError(f"provider {provider} is not deployed")
            adapter = provider_adapter(self.providers[provider].config)
            if adapter is None:
                raise RuntimeError(f"provider adapter is missing: {provider}")
            response = await adapter.terminate(
                self.lifecycle_client,
                self.providers[provider].config,
                self.preferences,
                resource_id,
            )
        else:
            if action.url is None:
                raise RuntimeError("provider action has no URL")
            response = await self.lifecycle_client.request(
                action.method,
                self.resolve_resource(action.url, provider),
                headers=action.headers,
                json=action.json_body,
            )
        resource_absent = (
            action_name in {"delete", "destroy", "terminate"}
            and response.status_code == 404
        )
        if not resource_absent:
            response.raise_for_status()
        if action.resource_id_path is not None:
            try:
                resource_id: object = response.json()
            except ValueError as exc:
                raise RuntimeError("provider returned a non-JSON deployment") from exc
            for key in action.resource_id_path.split("."):
                if not isinstance(resource_id, dict) or key not in resource_id:
                    raise RuntimeError(
                        f"provider response has no {action.resource_id_path}"
                    )
                resource_id = resource_id[key]
            if not isinstance(resource_id, (int, str)) or not str(resource_id):
                raise RuntimeError("provider returned an invalid resource id")
            self.store.save_provider_resource(provider, str(resource_id))
        if action_name in {"delete", "destroy", "terminate"}:
            self.store.clear_provider_resource(provider)
        self.store.event(
            "info", f"provider action {action_name} succeeded", provider=provider
        )
        return response

    async def deployment_options(self, provider: str) -> list[dict[str, object]]:
        runtime = self.providers.get(provider)
        if runtime is None:
            raise KeyError(f"unknown provider: {provider}")
        return await deployment_options(
            self.lifecycle_client, runtime.config, self.preferences
        )

    def resource_id(self, provider: str) -> str | None:
        return (
            self.store.provider_resource(provider)
            or self.providers[provider].config.resource_id
        )

    def resolve_resource(self, value: str, provider: str) -> str:
        if "{resource_id}" not in value:
            return value
        resource_id = self.resource_id(provider)
        if resource_id is None:
            raise RuntimeError(f"provider {provider} is not deployed")
        return value.replace("{resource_id}", quote(resource_id, safe=""))

    def worker_url(self, runtime: ProviderRuntime, path: str) -> str:
        if runtime.base_url is None:
            raise RuntimeError(f"provider {runtime.config.id} has no discovered URL")
        base_url = self.resolve_resource(runtime.base_url, runtime.config.id)
        if not path:
            return base_url.rstrip("/")
        return f"{base_url.rstrip('/')}/{path.lstrip('/')}"

    def worker_headers(
        self, runtime: ProviderRuntime, request_id: str | None = None
    ) -> dict[str, str]:
        adapter = provider_adapter(runtime.config)
        headers = (
            adapter.worker_headers(runtime.config, self.preferences)
            if adapter is not None
            else {"Authorization": f"Bearer {runtime.config.api_key}"}
        )
        if request_id:
            headers["x-request-id"] = request_id
        return headers

    def available_actions(self, provider: str) -> dict[str, ProviderAction]:
        runtime = self.providers[provider]
        return available_provider_actions(
            runtime.config,
            self.preferences,
            self.resource_id(provider),
        )

    async def run_provider_action(
        self,
        provider: str,
        action_name: str,
        request_id: str,
        preferences: ControlPreferences | None = None,
        selection: DeploymentSelection | None = None,
    ) -> dict[str, object]:
        async with self.configuration_lock:
            return await self._run_provider_action(
                provider, action_name, request_id, preferences, selection
            )

    async def _run_provider_action(
        self,
        provider: str,
        action_name: str,
        request_id: str,
        preferences: ControlPreferences | None = None,
        selection: DeploymentSelection | None = None,
    ) -> dict[str, object]:
        try:
            runtime = self.providers[provider]
            action = self.available_actions(provider)[action_name]
        except KeyError as exc:
            raise KeyError(
                f"unknown provider action: {provider}/{action_name}"
            ) from exc
        if action_name == "start":
            await self.ensure_ready(runtime, request_id)
            return {"action": action_name, "provider": provider, "state": runtime.state}
        async with runtime.lock:
            if (
                action_name in {"delete", "destroy", "stop", "terminate"}
                and runtime.active_requests
            ):
                raise RuntimeError("provider has active requests")
            if action_name == "stop":
                runtime.state = "stopping"
            response = await self.action(
                action,
                provider,
                action_name,
                preferences=preferences,
                selection=selection,
            )
            if action_name == "deploy":
                runtime.state = "starting"
            elif action_name in {"delete", "destroy", "terminate"}:
                runtime.lifecycle_revision += 1
                runtime.ready = False
                runtime.state = "stopped"
            if action_name == "stop":
                runtime.ready = False
                runtime.state = "stopped"
        content_type = response.headers.get("content-type", "")
        body: object | None = None
        if response.content and len(response.content) <= 64 * 1024:
            if "application/json" in content_type:
                try:
                    body = redacted(response.json())
                except ValueError:
                    body = response.text
            elif content_type.startswith("text/"):
                body = response.text
        return {
            "action": action_name,
            "body": body,
            "provider": provider,
            "state": runtime.state,
            "status": response.status_code,
        }

    async def check_ready(self, runtime: ProviderRuntime) -> bool:
        try:
            await self.refresh_endpoint(runtime)
            response = await runtime.client.get(
                self.worker_url(runtime, runtime.config.health_path),
                headers=self.worker_headers(runtime),
                timeout=10,
            )
            runtime.ready = response.is_success
        except ProviderNotDeployed:
            self.store.clear_provider_resource(runtime.config.id)
            runtime.base_url = None
            runtime.ready = False
        except Exception:  # noqa: BLE001 - provider discovery and health boundary
            runtime.ready = False
        runtime.state = "ready" if runtime.ready else "stopped"
        return runtime.ready

    async def ensure_ready(self, runtime: ProviderRuntime, request_id: str) -> None:
        if await self.check_ready(runtime):
            return
        recovery: StartRecovery | None = None
        recovery_adapter = None
        actions = self.available_actions(runtime.config.id)
        deploy_action = actions.get("deploy")
        start_action = actions.get("start")
        if (
            deploy_action is None
            and start_action is None
            and runtime.config.management is None
        ):
            runtime.state = "starting"
            return
        async with runtime.lock:
            if await self.check_ready(runtime):
                return
            runtime.state = "starting"
            if (
                deploy_action is not None
                and self.resource_id(runtime.config.id) is None
            ):
                self.store.event(
                    "info",
                    "deploying provider",
                    provider=runtime.config.id,
                    request_id=request_id,
                )
                await self.action(deploy_action, runtime.config.id, "deploy")
            elif start_action is not None:
                self.store.event(
                    "info",
                    "starting provider",
                    provider=runtime.config.id,
                    request_id=request_id,
                )
                try:
                    await self.action(start_action, runtime.config.id, "start")
                except httpx.HTTPStatusError as exc:
                    recovery_adapter = provider_adapter(runtime.config)
                    resource_id = self.resource_id(runtime.config.id)
                    if recovery_adapter is None or resource_id is None:
                        raise
                    recovery = await recovery_adapter.recover_start(
                        self.lifecycle_client,
                        runtime.config,
                        self.preferences,
                        self.settings,
                        resource_id,
                        exc.response,
                    )
                    if recovery is None:
                        raise
                    self.store.save_provider_resource(
                        runtime.config.id, recovery.new_resource_id
                    )
                    runtime.base_url = None
                    runtime.ready = False
                    self.store.event(
                        "warning",
                        "replacing provider resource after host capacity failure",
                        provider=runtime.config.id,
                        request_id=request_id,
                    )
        lifecycle_revision = runtime.lifecycle_revision
        deadline = time.monotonic() + runtime.config.startup_timeout
        try:
            while time.monotonic() < deadline:
                if await self.check_ready(runtime):
                    self.store.event(
                        "info",
                        "provider ready",
                        provider=runtime.config.id,
                        request_id=request_id,
                    )
                    if recovery is not None and recovery_adapter is not None:
                        try:
                            await recovery_adapter.terminate(
                                self.lifecycle_client,
                                runtime.config,
                                self.preferences,
                                recovery.old_resource_id,
                            )
                        except Exception as exc:  # noqa: BLE001 - cleanup boundary
                            self.store.event(
                                "warning",
                                f"old provider resource cleanup failed: {exception_message(exc)}",
                                provider=runtime.config.id,
                                request_id=request_id,
                            )
                        else:
                            self.store.event(
                                "info",
                                "old provider resource removed after replacement",
                                provider=runtime.config.id,
                                request_id=request_id,
                            )
                    return
                if runtime.lifecycle_revision != lifecycle_revision:
                    raise RuntimeError(
                        f"provider {runtime.config.id} was terminated while starting"
                    )
                await asyncio.sleep(2)
            runtime.state = "failed"
            raise TimeoutError(f"provider {runtime.config.id} did not become ready")
        except BaseException:
            if recovery is not None and recovery_adapter is not None:
                try:
                    await recovery_adapter.terminate(
                        self.lifecycle_client,
                        runtime.config,
                        self.preferences,
                        recovery.new_resource_id,
                    )
                except Exception as exc:  # noqa: BLE001 - rollback boundary
                    self.store.event(
                        "warning",
                        f"replacement rollback failed: {exception_message(exc)}",
                        provider=runtime.config.id,
                        request_id=request_id,
                    )
                self.store.save_provider_resource(
                    runtime.config.id, recovery.old_resource_id
                )
                runtime.base_url = None
                runtime.ready = False
            raise

    async def idle_reaper(self) -> None:
        while True:
            await asyncio.sleep(15)
            for runtime in self.providers.values():
                action = self.available_actions(runtime.config.id).get("stop")
                if (
                    action is None
                    or runtime.config.idle_seconds == 0
                    or runtime.active_requests
                    or time.monotonic() - runtime.last_used
                    < runtime.config.idle_seconds
                ):
                    continue
                async with runtime.lock:
                    if (
                        runtime.active_requests
                        or time.monotonic() - runtime.last_used
                        < runtime.config.idle_seconds
                        or not await self.check_ready(runtime)
                    ):
                        continue
                    runtime.state = "stopping"
                    try:
                        await self.action(action, runtime.config.id, "stop")
                        runtime.ready = False
                        runtime.state = "stopped"
                    except httpx.HTTPError as exc:
                        runtime.state = "failed"
                        self.store.event(
                            "error",
                            f"provider stop failed: {exc}",
                            provider=runtime.config.id,
                        )

    def model(self, model_id: str, operation: str) -> RoutedModel:
        try:
            model = self.models[model_id]
        except KeyError as exc:
            raise KeyError(f"unknown model: {model_id}") from exc
        if model.operation != operation:
            raise ValueError(f"model {model_id} does not support {operation}")
        return model

    def resolve_model(
        self, model_id: str, operation: str, provider: str | None = None
    ) -> tuple[RoutedModel, list[Target]]:
        provider_id = None
        if provider:
            try:
                provider_id = self.provider_ids[provider]
            except KeyError as exc:
                raise ValueError(f"unknown provider: {provider}") from exc
        try:
            model = self.model(model_id, operation)
        except KeyError:
            if not provider_id and "/" in model_id:
                provider_name, qualified_model = model_id.split("/", 1)
                provider_id = self.provider_ids.get(provider_name)
                if provider_id:
                    provider = provider_name
                    model_id = qualified_model
            if not provider_id:
                raise
            matches = [
                (candidate, target)
                for candidate in self.config.models
                if candidate.operation == operation
                for target in candidate.targets
                if target.provider == provider_id and target.model == model_id
            ]
            if not matches and "/" in model_id:
                provider_name, qualified_model = model_id.split("/", 1)
                if self.provider_ids.get(provider_name) == provider_id:
                    matches = [
                        (candidate, target)
                        for candidate in self.config.models
                        if candidate.operation == operation
                        for target in candidate.targets
                        if target.provider == provider_id
                        and target.model == qualified_model
                    ]
            if not matches:
                raise KeyError(f"unknown {provider}/{model_id} route for {operation}")
            model, target = matches[0]
            return model, [target]
        targets = (
            [target for target in model.targets if target.provider == provider_id]
            if provider_id
            else model.targets
        )
        if not targets:
            raise ValueError(
                f"provider '{provider}' is unavailable for model '{model_id}'"
            )
        return model, targets

    async def forward(
        self,
        target: Target,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
        request_id: str,
    ) -> httpx.Response:
        runtime = self.providers[target.provider]
        async with runtime.lock:
            runtime.active_requests += 1
        try:
            await self.ensure_ready(runtime, request_id)
            try:
                await self.refresh_endpoint(runtime)
            except (httpx.HTTPError, RuntimeError):
                raise
            except Exception as exc:
                raise RuntimeError(exception_message(exc)) from exc
            runtime.state = "busy"
            response = await runtime.client.request(
                method,
                self.worker_url(runtime, path),
                content=body,
                headers=headers | self.worker_headers(runtime, request_id),
            )
            runtime.ready = response.status_code not in {502, 503, 504}
            return response
        finally:
            async with runtime.lock:
                runtime.active_requests -= 1
                runtime.last_used = time.monotonic()
                runtime.state = "ready" if runtime.ready else "unknown"

    async def execute_internal(
        self,
        target: Target,
        execution_id: str,
        operation: str,
        parameters: dict[str, object],
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
    ) -> list[tuple[bytes, str, str]]:
        """Execute through the current controller-to-worker contract."""
        runtime = self.providers[target.provider]
        async with runtime.lock:
            runtime.active_requests += 1
        try:
            await self.ensure_ready(runtime, execution_id[:16])
            await self.refresh_endpoint(runtime)
            runtime.state = "busy"
            spec = json.dumps(
                {
                    "execution_id": execution_id,
                    "model": target.model,
                    "operation": operation,
                    "parameters": parameters,
                },
                separators=(",", ":"),
            )
            response = await runtime.client.post(
                self.worker_url(runtime, "/internal/executions"),
                data={"spec": spec},
                files=files or [],
                headers=self.worker_headers(runtime, execution_id[:16]),
            )
            response.raise_for_status()
            value = response.json()
            manifests = value.get("outputs") if isinstance(value, dict) else None
            if not isinstance(manifests, list):
                raise RuntimeError(  # noqa: TRY004 - invalid provider response
                    "worker returned an invalid execution manifest"
                )
            outputs: list[tuple[bytes, str, str]] = []
            base_url = self.worker_url(runtime, "")
            for manifest in manifests:
                if not isinstance(manifest, dict) or not manifest.get("url"):
                    raise RuntimeError("worker returned an invalid output manifest")
                url = urljoin(f"{base_url}/", str(manifest["url"]))
                if url != base_url and not url.startswith(f"{base_url}/"):
                    raise RuntimeError(
                        "worker output URL is outside its serving origin"
                    )
                output = await runtime.client.get(
                    url,
                    headers=self.worker_headers(runtime),
                )
                output.raise_for_status()
                outputs.append(
                    (
                        output.content,
                        output.headers.get("content-type")
                        or str(
                            manifest.get("content_type") or "application/octet-stream"
                        ),
                        str(manifest.get("filename") or "output"),
                    )
                )
            runtime.ready = True
            return outputs
        finally:
            async with runtime.lock:
                runtime.active_requests -= 1
                runtime.last_used = time.monotonic()
                runtime.state = "ready" if runtime.ready else "unknown"


def rewrite_json_model(body: bytes, model: str) -> bytes:
    value = json.loads(body)
    if not isinstance(value, dict):
        raise TypeError("request body must be an object")
    value["model"] = model
    return json.dumps(value, separators=(",", ":")).encode()
