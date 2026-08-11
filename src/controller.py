from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx

from .control_config import (
    ControlFile,
    ControlSettings,
    Provider,
    ProviderAction,
    RoutedModel,
    Target,
    UsageProbe,
)
from .control_store import ControlStore, Job

SENSITIVE_FIELD_PARTS = (
    "accesskey",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)


def exception_message(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"provider API returned HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.RequestError):
        return f"provider connection failed ({type(exc).__name__})"
    return str(exc)


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


def required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required to manage this provider")
    return value


def required_mapping_value(value: object, key: str) -> object:
    if not isinstance(value, dict) or key not in value or value[key] in (None, ""):
        raise RuntimeError(f"provider response has no {key}")
    return value[key]


def named_resource(
    value: object, name: str, name_key: str, resource_id: str | None
) -> dict[str, object]:
    if not isinstance(value, list):
        raise RuntimeError(  # noqa: TRY004 - provider boundary failure
            "provider returned an invalid resource list"
        )
    resources = [item for item in value if isinstance(item, dict)]
    if resource_id is not None:
        matches = [
            item
            for item in resources
            if str(item.get("id")) == resource_id and item.get(name_key) == name
        ]
        if len(matches) == 1:
            return matches[0]
    matches = [item for item in resources if item.get(name_key) == name]
    if not matches:
        raise RuntimeError(f"provider resource not found: {name}")
    if len(matches) > 1:
        raise RuntimeError(f"provider resource name is ambiguous: {name}")
    return matches[0]


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
    ready: bool = False
    state: str = "unknown"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    usage: dict[str, object] = field(default_factory=lambda: {"status": "unconfigured"})
    usage_checked_at: float = 0
    usage_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Controller:
    def __init__(self, settings: ControlSettings):
        self.settings = settings
        self.config = ControlFile.load(settings.config_file)
        self.models = {model.id: model for model in self.config.models}
        self.providers = {
            provider.id: ProviderRuntime(
                config=provider,
                base_url=provider.base_url,
                client=httpx.AsyncClient(
                    follow_redirects=False, timeout=provider.request_timeout
                ),
            )
            for provider in self.config.providers
        }
        self.provider_ids = {
            name: provider.id
            for provider in self.config.providers
            for name in (provider.id, *provider.aliases)
        }
        self.lifecycle_client = httpx.AsyncClient(timeout=60)
        self.media_client = httpx.AsyncClient(follow_redirects=True, timeout=120)
        self.store = ControlStore(settings.database_path)
        self.media_path = settings.database_path.parent / "media"
        self.media_path.mkdir(parents=True, exist_ok=True)
        self.uploads_path = settings.database_path.parent / "uploads"
        self.video_tasks: set[asyncio.Task[None]] = set()

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
                if probe.kind == "modal":
                    metrics = await asyncio.to_thread(modal_usage)
                else:
                    url = probe.url
                    headers = probe.headers
                    if probe.kind == "salad" and url is None:
                        organisation = required_environment("SALAD_ORGANISATION")
                        url = (
                            "https://api.salad.com/api/public/organizations/"
                            f"{quote(organisation, safe='')}/quotas"
                        )
                        headers = {
                            "Salad-Api-Key": required_environment("SALAD_API_KEY")
                        }
                    response = await self.lifecycle_client.get(url, headers=headers)
                    response.raise_for_status()
                    if probe.kind == "cliproxyapi":
                        metrics = normalise_usage(
                            probe.kind,
                            self.store.history_usage(runtime.config.id),
                        )
                        try:
                            metrics.extend(await self.cliproxy_xai_quotas(probe))
                        except Exception:  # noqa: BLE001 - optional quota telemetry
                            metrics.append(
                                {"label": "Grok allowances", "value": "unavailable"}
                            )
                    else:
                        metrics = normalise_usage(probe.kind, response.json())
                runtime.usage = {"metrics": metrics, "status": "ok"}
            except Exception as exc:  # noqa: BLE001 - optional account telemetry
                runtime.usage = {
                    "error": exception_message(exc),
                    "status": "unavailable",
                }
            runtime.usage_checked_at = time.monotonic()
            return runtime.usage

    async def cliproxy_xai_quotas(self, probe: UsageProbe) -> list[dict[str, object]]:
        management_url = str(probe.url).rsplit("/", 1)[0]
        response = await self.lifecycle_client.get(
            f"{management_url}/auth-files", headers=probe.headers
        )
        response.raise_for_status()
        payload = response.json()
        files = payload.get("files", []) if isinstance(payload, dict) else []
        accounts = [
            item
            for item in files
            if isinstance(item, dict)
            and str(item.get("type", "")).casefold() == "xai"
            and item.get("disabled") is not True
            and (item.get("auth_index") or item.get("authIndex"))
        ]
        results = await asyncio.gather(
            *(
                self.cliproxy_xai_quota_account(
                    management_url, probe.headers, account, index
                )
                for index, account in enumerate(accounts, 1)
            ),
            return_exceptions=True,
        )
        return [
            metric
            for result in results
            if isinstance(result, list)
            for metric in result
        ]

    async def cliproxy_xai_quota_account(
        self,
        management_url: str,
        headers: dict[str, str],
        account: dict[str, object],
        index: int,
    ) -> list[dict[str, object]]:
        auth_index = str(account.get("auth_index") or account.get("authIndex"))
        request_headers = {
            "Authorization": "Bearer $TOKEN$",
            "accept": "*/*",
            "user-agent": "grok-pager/0.2.91 grok-shell/0.2.91 (macos; aarch64)",
            "x-grok-client-version": "0.2.91",
            "x-xai-token-auth": "xai-grok-cli",
        }
        user_id = xai_user_id(account)
        if user_id:
            request_headers["x-userid"] = user_id
        calls = await asyncio.gather(
            *(
                self.lifecycle_client.post(
                    f"{management_url}/api-call",
                    headers=headers,
                    json={
                        "authIndex": auth_index,
                        "method": "GET",
                        "url": url,
                        "header": request_headers,
                    },
                )
                for url in (
                    "https://cli-chat-proxy.grok.com/v1/billing?format=credits",
                    "https://cli-chat-proxy.grok.com/v1/billing",
                )
            ),
            return_exceptions=True,
        )
        metrics: list[dict[str, object]] = []
        for response in calls:
            if not isinstance(response, httpx.Response) or not response.is_success:
                continue
            result = response.json()
            if (
                not isinstance(result, dict)
                or not 200 <= int(result.get("status_code", 0)) < 300
            ):
                continue
            body = result.get("body")
            if isinstance(body, str):
                try:
                    body = json.loads(body)
                except json.JSONDecodeError:
                    continue
            metrics.extend(normalise_xai_quota(body, index))
        return deduplicate_metrics(metrics)

    async def usage(self) -> dict[str, dict[str, object]]:
        values = await asyncio.gather(
            *(self.provider_usage(runtime) for runtime in self.providers.values())
        )
        return dict(zip(self.providers, values, strict=True))

    async def provider_statuses(self) -> dict[str, dict[str, object]]:
        values = await asyncio.gather(
            *(self.provider_status(runtime) for runtime in self.providers.values())
        )
        return dict(zip(self.providers, values, strict=True))

    async def provider_status(self, runtime: ProviderRuntime) -> dict[str, object]:
        management = runtime.config.management
        details: dict[str, object] = {}
        try:
            resource = await self.refresh_endpoint(runtime, route=False)
            if management is None:
                response = await runtime.client.get(
                    self.worker_url(runtime, runtime.config.health_path),
                    headers={"Authorization": f"Bearer {runtime.config.api_key}"},
                    timeout=4,
                )
                runtime.ready = response.is_success
                runtime.state = "ready" if runtime.ready else "unavailable"
            elif management.kind == "modal":
                details = await asyncio.to_thread(
                    modal_status, management.name, management.function or ""
                )
                runtime.state = str(details.pop("state"))
            elif management.kind == "runpod-pod":
                runtime.state = str(resource.get("desiredStatus", "unknown")).lower()
                details = selected_fields(
                    resource,
                    "costPerHr",
                    "desiredStatus",
                    "gpuCount",
                    "gpuTypeId",
                    "lastStartedAt",
                    "machineId",
                    "vcpuCount",
                )
            elif management.kind == "runpod-serverless":
                workers = resource.get("workers")
                details = selected_fields(
                    resource,
                    "executionTimeoutMs",
                    "gpuIds",
                    "idleTimeout",
                    "maxWorkers",
                    "minWorkers",
                    "workers",
                )
                active = first_number(workers, "running", "ready")
                runtime.state = "ready" if active else "scaled-down"
            elif management.kind == "salad":
                current = resource.get("current_state")
                runtime.state = str(
                    (current.get("status") if isinstance(current, dict) else None)
                    or "unknown"
                ).lower()
                details = selected_fields(
                    resource,
                    "autostart_policy",
                    "current_state",
                    "display_name",
                    "replicas",
                )
            elif management.kind == "vast-serverless":
                runtime.state = str(resource.get("endpoint_state", "unknown")).lower()
                details = selected_fields(
                    resource,
                    "endpoint_state",
                    "max_workers",
                    "min_load",
                    "num_workers",
                    "target_util",
                )
            else:
                runtime.state = str(resource.get("actual_status", "unknown")).lower()
                details = selected_fields(
                    resource,
                    "actual_status",
                    "cur_state",
                    "dph_total",
                    "gpu_name",
                    "gpu_util",
                    "num_gpus",
                )
            return {
                "details": redacted(details),
                "panel_url": provider_panel_url(runtime, details),
                "state": runtime.state,
                "status": "ok",
            }
        except Exception as exc:  # noqa: BLE001 - provider control-plane boundary
            runtime.state = "unavailable"
            return {
                "details": {},
                "error": exception_message(exc),
                "panel_url": provider_panel_url(runtime, details),
                "state": runtime.state,
                "status": "unavailable",
            }

    def provider_logs(self, provider: str, limit: int = 200) -> dict[str, object]:
        if provider not in self.providers:
            raise KeyError(f"unknown provider: {provider}")
        entries = [
            event
            for event in self.store.events(min(max(limit, 1), 500) * 10)
            if event.get("provider") == provider
        ][: min(max(limit, 1), 500)]
        return {
            "entries": redacted(entries),
            "provider": provider,
            "source": "Comfy Control",
        }

    async def close(self) -> None:
        for task in self.video_tasks:
            task.cancel()
        await asyncio.gather(*self.video_tasks, return_exceptions=True)
        await asyncio.gather(
            *(runtime.client.aclose() for runtime in self.providers.values())
        )
        await self.lifecycle_client.aclose()
        await self.media_client.aclose()
        self.store.close()

    async def refresh_endpoint(
        self, runtime: ProviderRuntime, *, route: bool = True
    ) -> dict[str, object]:
        management = runtime.config.management
        if management is None:
            return {}
        if management.kind == "modal":
            runtime.base_url = await asyncio.to_thread(
                modal_web_url, management.name, management.function or ""
            )
            self.store.save_provider_resource(runtime.config.id, management.name)
            return {}

        if management.kind.startswith("runpod-"):
            api_key = required_environment("RUNPOD_API_KEY")
            collection = "pods" if management.kind == "runpod-pod" else "endpoints"
            response = await self.lifecycle_client.get(
                f"https://rest.runpod.io/v1/{collection}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            resource = named_resource(
                response.json(),
                management.name,
                "name",
                self.resource_id(runtime.config.id),
            )
            resource_id = required_mapping_value(resource, "id")
            self.store.save_provider_resource(runtime.config.id, str(resource_id))
            if management.kind == "runpod-serverless":
                runtime.base_url = f"https://{resource_id}.api.runpod.ai"
            else:
                runtime.base_url = (
                    f"https://{resource_id}-{management.port}.proxy.runpod.net"
                )
            return resource

        if management.kind == "salad":
            api_key = required_environment("SALAD_API_KEY")
            organisation = management.organisation or required_environment(
                "SALAD_ORGANISATION"
            )
            project = management.project or required_environment("SALAD_PROJECT")
            response = await self.lifecycle_client.get(
                "https://api.salad.com/api/public/organizations/"
                f"{quote(organisation, safe='')}/projects/{quote(project, safe='')}"
                f"/containers/{quote(management.name, safe='')}",
                headers={"Salad-Api-Key": api_key},
            )
            response.raise_for_status()
            resource = response.json()
            self.store.save_provider_resource(
                runtime.config.id, str(required_mapping_value(resource, "id"))
            )
            networking = required_mapping_value(resource, "networking")
            if not isinstance(networking, dict):
                raise RuntimeError("SaladCloud provider has invalid networking data")
            dns = str(required_mapping_value(networking, "dns"))
            runtime.base_url = dns if "://" in dns else f"https://{dns}"
            return resource if isinstance(resource, dict) else {}

        api_key = required_environment("VAST_API_KEY")
        headers = {"Authorization": f"Bearer {api_key}"}
        if management.kind == "vast-serverless":
            response = await self.lifecycle_client.get(
                "https://console.vast.ai/api/v0/endptjobs/", headers=headers
            )
            response.raise_for_status()
            payload = response.json()
            resources = required_mapping_value(payload, "results")
            resource = named_resource(
                resources,
                management.name,
                "endpoint_name",
                self.resource_id(runtime.config.id),
            )
            self.store.save_provider_resource(
                runtime.config.id, str(required_mapping_value(resource, "id"))
            )
            if route:
                route_response = await self.lifecycle_client.post(
                    "https://run.vast.ai/route/",
                    headers=headers,
                    json={"cost": 100, "endpoint": management.name},
                )
                route_response.raise_for_status()
                runtime.base_url = str(
                    required_mapping_value(route_response.json(), "url")
                ).rstrip("/")
            return resource

        response = await self.lifecycle_client.get(
            "https://console.vast.ai/api/v1/instances/", headers=headers
        )
        response.raise_for_status()
        payload = response.json()
        resources = required_mapping_value(payload, "instances")
        resource = named_resource(
            resources,
            management.name,
            "label",
            self.resource_id(runtime.config.id),
        )
        resource_id = required_mapping_value(resource, "id")
        self.store.save_provider_resource(runtime.config.id, str(resource_id))
        address = required_mapping_value(resource, "public_ipaddr")
        ports = required_mapping_value(resource, "ports")
        mapping = (
            ports.get(f"{management.port}/tcp") if isinstance(ports, dict) else None
        )
        if (
            not isinstance(mapping, list)
            or not mapping
            or not isinstance(mapping[0], dict)
        ):
            raise RuntimeError(
                f"Vast.ai provider has no public mapping for port {management.port}"
            )
        port = required_mapping_value(mapping[0], "HostPort")
        runtime.base_url = f"http://{address}:{port}"
        return resource

    def save_media(
        self,
        history_id: str,
        content: bytes,
        content_type: str,
        filename: str,
    ) -> None:
        extension = media_extension(content_type)
        directory = self.media_path / history_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{uuid.uuid4().hex}{extension}"
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(content)
        temporary.replace(path)
        self.store.save_media(
            history_id,
            content_type.split(";", 1)[0],
            safe_filename(filename, extension),
            path,
            len(content),
        )

    async def download_media(
        self,
        history_id: str,
        provider: str,
        url: str,
        fallback_name: str,
    ) -> None:
        runtime = self.providers[provider]
        base_url = self.worker_url(runtime, "")
        resolved = urljoin(f"{base_url}/", url)
        headers = {}
        local = resolved == base_url or resolved.startswith(f"{base_url}/")
        if local:
            headers["Authorization"] = f"Bearer {runtime.config.api_key}"
        client = runtime.client if local else self.media_client
        async with client.stream("GET", resolved, headers=headers) as response:
            if response.is_redirect and response.headers.get("location"):
                redirect = response.headers["location"]
                await self.download_media(history_id, provider, redirect, fallback_name)
                return
            response.raise_for_status()
            filename = unquote(Path(urlparse(resolved).path).name) or fallback_name
            content_type = response.headers.get(
                "content-type", media_type_from_filename(filename)
            )
            extension = media_extension(content_type)
            directory = self.media_path / history_id
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{uuid.uuid4().hex}{extension}"
            temporary = path.with_suffix(path.suffix + ".part")
            size = 0
            try:
                with temporary.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        file.write(chunk)
                        size += len(chunk)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            self.store.save_media(
                history_id,
                content_type.split(";", 1)[0],
                safe_filename(filename, extension),
                path,
                size,
            )

    async def archive_images(
        self, history_id: str, provider: str, response: httpx.Response
    ) -> None:
        try:
            value = response.json()
            data = value.get("data", []) if isinstance(value, dict) else []
            for index, item in enumerate(data):
                if not isinstance(item, dict):
                    continue
                if encoded := item.get("b64_json"):
                    content = base64.b64decode(encoded, validate=True)
                    content_type = image_media_type(content)
                    self.save_media(
                        history_id,
                        content,
                        content_type,
                        f"image-{index + 1}{media_extension(content_type)}",
                    )
                elif url := item.get("url"):
                    await self.download_media(
                        history_id, provider, str(url), f"image-{index + 1}.png"
                    )
        except Exception as exc:  # noqa: BLE001 - archival must not fail inference
            self.store.event(
                "error",
                f"media archival failed: {exception_message(exc)}",
                provider=provider,
                request_id=history_id,
            )

    async def archive_video(
        self, history_id: str, provider: str, response: dict[str, object]
    ) -> None:
        try:
            if output_url := response.get("output_url"):
                await self.download_media(
                    history_id, provider, str(output_url), "video.mp4"
                )
                return
            runtime = self.providers[provider]
            upstream_id = str(response.get("id", ""))
            await self.download_media(
                history_id,
                provider,
                self.worker_url(runtime, f"/v1/videos/{upstream_id}/content"),
                "video.mp4",
            )
        except Exception as exc:  # noqa: BLE001 - archival must not fail inference
            self.store.event(
                "error",
                f"media archival failed: {exception_message(exc)}",
                provider=provider,
                request_id=history_id,
            )

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
                try:
                    response = await self.forward(
                        target,
                        "POST",
                        "/v1/videos?wait=true",
                        *self.video_request(job, target.model),
                        request_id,
                    )
                    data = response.json()
                except (httpx.HTTPError, RuntimeError, TimeoutError, ValueError) as exc:
                    message = exception_message(exc)
                    failures.append(f"{target.provider}: {message}")
                    self.store.event(
                        "error",
                        message,
                        provider=target.provider,
                        request_id=request_id,
                    )
                    continue
                if response.is_success and data.get("status") == "completed":
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
                    await self.archive_video(job.id, target.provider, archive_data)
                    return
                message = data.get("error") or f"HTTP {response.status_code}"
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

    def video_request(self, job: Job, model: str) -> tuple[bytes, dict[str, str]]:
        value = json.loads(job.request_json)
        multipart = value.get("_control_multipart")
        if multipart is None:
            return rewrite_json_model(job.request_json.encode(), model), {
                "content-type": "application/json",
                "x-comfy-job-id": job.id,
            }
        fields = [
            (key, model if key == "model" else item)
            for key, item in multipart["fields"]
        ]
        if not any(key == "model" for key, _ in fields):
            fields.append(("model", model))
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
        encoded = httpx.Request(
            "POST", "http://multipart.invalid", data=dict(fields), files=files
        )
        return encoded.read(), {
            "content-type": encoded.headers["content-type"],
            "x-comfy-job-id": job.id,
        }

    def remove_uploads(self, job_id: str) -> None:
        directory = self.uploads_path / job_id
        if not directory.is_dir():
            return
        for path in directory.iterdir():
            if path.is_file():
                path.unlink()
        directory.rmdir()

    async def action(
        self, action: ProviderAction, provider: str, action_name: str = "lifecycle"
    ) -> httpx.Response:
        if action_name == "deploy" and self.resource_id(provider) is not None:
            raise RuntimeError(f"provider {provider} is already deployed")
        if action.internal:
            response = await asyncio.to_thread(
                modal_provider_action, action.internal, self.providers[provider]
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
        return f"{base_url}{path}"

    def available_actions(self, provider: str) -> dict[str, ProviderAction]:
        runtime = self.providers[provider]
        actions = dict(runtime.config.actions)
        if runtime.config.lifecycle.start is not None:
            actions["start"] = runtime.config.lifecycle.start
        if runtime.config.lifecycle.stop is not None:
            actions["stop"] = runtime.config.lifecycle.stop
        management = runtime.config.management
        if management is not None and management.kind == "runpod-pod":
            headers = {
                "authorization": f"Bearer {required_environment('RUNPOD_API_KEY')}"
            }
            actions.setdefault(
                "start",
                ProviderAction(
                    headers=headers,
                    url="https://rest.runpod.io/v1/pods/{resource_id}/start",
                ),
            )
            actions.setdefault(
                "stop",
                ProviderAction(
                    confirmation="Stop the RunPod Pod?",
                    headers=headers,
                    url="https://rest.runpod.io/v1/pods/{resource_id}/stop",
                ),
            )
        if management is not None and management.kind == "salad":
            organisation = management.organisation or os.getenv("SALAD_ORGANISATION")
            project = management.project or os.getenv("SALAD_PROJECT")
            if organisation and project:
                base_url = (
                    "https://api.salad.com/api/public/organizations/"
                    f"{quote(organisation, safe='')}/projects/{quote(project, safe='')}"
                    f"/containers/{quote(management.name, safe='')}"
                )
                headers = {"Salad-Api-Key": required_environment("SALAD_API_KEY")}
                actions.setdefault(
                    "start", ProviderAction(headers=headers, url=f"{base_url}/start")
                )
                actions.setdefault(
                    "stop",
                    ProviderAction(
                        confirmation="Stop the SaladCloud container group?",
                        headers=headers,
                        url=f"{base_url}/stop",
                    ),
                )
        if management is not None and management.kind == "vast-pod":
            headers = {
                "authorization": f"Bearer {required_environment('VAST_API_KEY')}"
            }
            url = "https://console.vast.ai/api/v0/instances/{resource_id}/"
            actions.setdefault(
                "start",
                ProviderAction(
                    headers=headers,
                    json={"state": "running"},
                    method="PUT",
                    url=url,
                ),
            )
            actions.setdefault(
                "stop",
                ProviderAction(
                    confirmation="Stop the Vast.ai Pod?",
                    headers=headers,
                    json={"state": "stopped"},
                    method="PUT",
                    url=url,
                ),
            )
        resource_id = self.resource_id(provider)
        return dict(
            sorted(
                (name, action)
                for name, action in actions.items()
                if not (name == "deploy" and resource_id)
                and not (
                    resource_id is None
                    and name in {"delete", "destroy", "start", "stop", "terminate"}
                )
                and not (
                    resource_id is None and action.url and "{resource_id}" in action.url
                )
            )
        )

    async def run_provider_action(
        self, provider: str, action_name: str, request_id: str
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
            response = await self.action(action, provider, action_name)
            if action_name == "deploy":
                runtime.state = "starting"
            elif action_name in {"delete", "destroy", "terminate"}:
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
                headers={"Authorization": f"Bearer {runtime.config.api_key}"},
                timeout=10,
            )
            runtime.ready = response.is_success
        except Exception:  # noqa: BLE001 - provider discovery and health boundary
            runtime.ready = False
        runtime.state = "ready" if runtime.ready else "stopped"
        return runtime.ready

    async def ensure_ready(self, runtime: ProviderRuntime, request_id: str) -> None:
        if await self.check_ready(runtime):
            return
        action = self.available_actions(runtime.config.id).get("start")
        if action is None:
            runtime.state = "starting"
            return
        async with runtime.lock:
            if await self.check_ready(runtime):
                return
            runtime.state = "starting"
            self.store.event(
                "info",
                "starting provider",
                provider=runtime.config.id,
                request_id=request_id,
            )
            await self.action(action, runtime.config.id, "start")
            deadline = time.monotonic() + runtime.config.startup_timeout
            while time.monotonic() < deadline:
                if await self.check_ready(runtime):
                    self.store.event(
                        "info",
                        "provider ready",
                        provider=runtime.config.id,
                        request_id=request_id,
                    )
                    return
                await asyncio.sleep(2)
            runtime.state = "failed"
            raise TimeoutError(f"provider {runtime.config.id} did not become ready")

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
                headers=headers
                | {
                    "Authorization": f"Bearer {runtime.config.api_key}",
                    "x-request-id": request_id,
                },
            )
            runtime.ready = response.status_code not in {502, 503, 504}
            return response
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


def media_extension(content_type: str) -> str:
    media_type = content_type.split(";", 1)[0].lower()
    return {
        "image/gif": ".gif",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
    }.get(media_type, ".bin")


def image_media_type(content: bytes) -> str:
    if content.startswith(b"GIF8"):
        return "image/gif"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def media_type_from_filename(filename: str) -> str:
    return {
        ".gif": "image/gif",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".mov": "video/quicktime",
        ".mp4": "video/mp4",
        ".png": "image/png",
        ".webm": "video/webm",
        ".webp": "image/webp",
    }.get(Path(filename).suffix.lower(), "application/octet-stream")


def safe_filename(filename: str, extension: str) -> str:
    name = "".join(
        character
        for character in Path(filename).name.strip()
        if character.isalnum() or character in {" ", "-", ".", "_"}
    )
    return name or f"media{extension}"


def first_number(value: object, *paths: str) -> float | int | None:
    for path in paths:
        current = value
        for key in path.split("."):
            if not isinstance(current, dict) or key not in current:
                break
            current = current[key]
        else:
            if isinstance(current, (float, int)) and not isinstance(current, bool):
                return current
    return None


def normalise_usage(kind: str, value: object) -> list[dict[str, object]]:
    metrics: list[dict[str, object]] = []
    if kind == "runpod":
        records = value if isinstance(value, list) else []
        amount = sum(
            float(record.get("amount", 0))
            for record in records
            if isinstance(record, dict)
        )
        return [
            {"label": "Reported spend", "unit": "USD", "value": round(amount, 4)},
            {"label": "Billing records", "value": len(records)},
        ]
    if kind == "vast":
        credit = first_number(value, "credit")
        if credit is not None:
            metrics.append({"label": "Credit", "unit": "USD", "value": credit})
        return metrics
    if kind == "salad":
        used = first_number(value, "container_groups_quotas.container_replicas_used")
        quota = first_number(value, "container_groups_quotas.container_replicas_quota")
        if used is not None:
            metrics.append({"label": "Replicas used", "value": used})
        if quota is not None:
            metrics.append({"label": "Replica quota", "value": quota})
        return metrics
    if kind == "cliproxyapi":
        for label, paths in (
            ("Requests", ("usage.total_requests", "total_requests")),
            ("Successful", ("usage.successful_requests", "successful_requests")),
            ("Failed", ("usage.failed_requests", "failed_requests")),
            ("Tokens", ("usage.total_tokens", "total_tokens")),
        ):
            found = first_number(value, *paths)
            if found is not None:
                metrics.append({"label": label, "value": found})
        return metrics
    raise ValueError(f"unsupported usage kind: {kind}")


def xai_user_id(account: dict[str, object]) -> str | None:
    records = [account]
    for key in ("attributes", "metadata", "oauth", "user"):
        value = account.get(key)
        if isinstance(value, dict):
            records.append(value)
    for record in records:
        for key in ("sub", "subject", "user_id", "userId", "id"):
            value = record.get(key)
            if isinstance(value, (int, str)) and str(value).strip():
                return str(value).strip()
    return None


def cent_value(value: object) -> float | None:
    if isinstance(value, dict):
        value = value.get("val")
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def normalise_xai_quota(value: object, account: int) -> list[dict[str, object]]:
    if not isinstance(value, dict):
        return []
    config = value.get("config")
    if not isinstance(config, dict):
        return []
    prefix = f"Grok account {account}"
    metrics: list[dict[str, object]] = []
    usage_percent = first_number(config, "creditUsagePercent", "credit_usage_percent")
    if usage_percent is not None:
        metrics.append(
            {
                "label": f"{prefix} weekly remaining",
                "unit": "%",
                "value": max(0, round(100 - usage_percent, 2)),
            }
        )
    products = config.get("productUsage") or config.get("product_usage")
    if isinstance(products, list):
        for product in products:
            if not isinstance(product, dict):
                continue
            name = str(product.get("product") or "product").strip()
            used = first_number(product, "usagePercent", "usage_percent")
            if used is not None:
                metrics.append(
                    {
                        "label": f"{prefix} {name} remaining",
                        "unit": "%",
                        "value": max(0, round(100 - used, 2)),
                    }
                )
    monthly_limit = cent_value(config.get("monthlyLimit", config.get("monthly_limit")))
    used = cent_value(config.get("used"))
    if monthly_limit is not None and used is not None:
        metrics.append(
            {
                "label": f"{prefix} monthly included remaining",
                "unit": "USD",
                "value": max(0, round((monthly_limit - used) / 100, 2)),
            }
        )
    on_demand_cap = cent_value(config.get("onDemandCap", config.get("on_demand_cap")))
    on_demand_used = cent_value(
        config.get("onDemandUsed", config.get("on_demand_used"))
    )
    if on_demand_cap is not None and on_demand_used is not None:
        metrics.append(
            {
                "label": f"{prefix} on-demand remaining",
                "unit": "USD",
                "value": max(0, round((on_demand_cap - on_demand_used) / 100, 2)),
            }
        )
    return metrics


def deduplicate_metrics(
    metrics: list[dict[str, object]],
) -> list[dict[str, object]]:
    return list({str(metric.get("label")): metric for metric in metrics}.values())


def selected_fields(value: object, *names: str) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {name: value[name] for name in names if name in value}


def provider_panel_url(
    runtime: ProviderRuntime, details: dict[str, object]
) -> str | None:
    management = runtime.config.management
    if management is None:
        if runtime.config.type == "proxy" and runtime.base_url:
            return f"{runtime.base_url}/management.html"
        return runtime.base_url
    if management.kind == "modal":
        return str(details.get("panel_url") or "https://modal.com/apps")
    if management.kind.startswith("runpod-"):
        collection = "pods" if management.kind == "runpod-pod" else "serverless"
        return f"https://console.runpod.io/{collection}"
    if management.kind == "salad":
        return "https://portal.salad.com/"
    if management.kind == "vast-pod":
        return "https://cloud.vast.ai/instances/"
    return "https://cloud.vast.ai/serverless/"


def modal_web_url(app_name: str, function_name: str) -> str:
    import modal

    url = modal.Function.from_name(app_name, function_name).get_web_url()
    if not url:
        raise RuntimeError(f"Modal web function has no URL: {app_name}/{function_name}")
    return url.rstrip("/")


def modal_provider_action(action: str, runtime: ProviderRuntime) -> httpx.Response:
    import modal

    management = runtime.config.management
    if management is None or management.kind != "modal":
        raise RuntimeError("Modal action requires Modal provider management")
    if action == "modal-terminate":
        modal.App.lookup(management.name).stop()
        return httpx.Response(200, json={"status": "terminated"})
    path = Path(
        os.getenv("CONTROL_MODAL_APP", "/opt/comfy-control/deploy/modal/app.py")
    )
    if not path.is_file():
        raise RuntimeError(f"Modal deployment asset was not found: {path}")
    specification = importlib.util.spec_from_file_location("comfy_control_modal", path)
    if specification is None or specification.loader is None:
        raise RuntimeError("Modal deployment asset could not be loaded")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    module.app.deploy(name=management.name)
    return httpx.Response(200, json={"status": "deployed"})


def modal_status(app_name: str, function_name: str) -> dict[str, object]:
    import modal

    function = modal.Function.from_name(app_name, function_name)
    stats = function.get_current_stats()
    values = {
        name: getattr(stats, name)
        for name in (
            "backlog",
            "num_active_runners",
            "num_total_runners",
        )
        if hasattr(stats, name)
    }
    active = values.get("num_active_runners") or values.get("num_total_runners")
    values["state"] = "ready" if active else "scaled-down"
    get_dashboard_url = getattr(function, "get_dashboard_url", None)
    if callable(get_dashboard_url):
        values["panel_url"] = get_dashboard_url()
    return values


def modal_usage() -> list[dict[str, object]]:
    import modal

    summary = modal.Workspace.from_context().billing.summary()
    metrics = [
        {"label": "Billed", "unit": "USD", "value": float(summary.billed_cost)},
        {"label": "Metered", "unit": "USD", "value": float(summary.metered_cost)},
    ]
    credit = sum(
        abs(float(value))
        for key, value in summary.adjustments.items()
        if "credit" in key.lower()
    )
    if credit:
        metrics.append({"label": "Credits used", "unit": "USD", "value": credit})
    return metrics
