from __future__ import annotations

import asyncio
import base64
import json
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
                client=httpx.AsyncClient(
                    follow_redirects=False, timeout=provider.request_timeout
                ),
            )
            for provider in self.config.providers
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
                    response = await self.lifecycle_client.get(
                        probe.url, headers=probe.headers
                    )
                    response.raise_for_status()
                    metrics = normalise_usage(probe.kind, response.json())
                runtime.usage = {"metrics": metrics, "status": "ok"}
            except Exception as exc:  # noqa: BLE001 - optional account telemetry
                runtime.usage = {
                    "error": exception_message(exc),
                    "status": "unavailable",
                }
            runtime.usage_checked_at = time.monotonic()
            return runtime.usage

    async def usage(self) -> dict[str, dict[str, object]]:
        values = await asyncio.gather(
            *(self.provider_usage(runtime) for runtime in self.providers.values())
        )
        return dict(zip(self.providers, values, strict=True))

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

    def save_media(
        self,
        history_id: str,
        content: bytes,
        content_type: str,
        filename: str,
    ) -> None:
        if len(content) > self.settings.maximum_media_bytes:
            raise ValueError("generated media exceeds CONTROL_MAXIMUM_MEDIA_BYTES")
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
            size = 0
            chunks = []
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self.settings.maximum_media_bytes:
                    raise ValueError(
                        "generated media exceeds CONTROL_MAXIMUM_MEDIA_BYTES"
                    )
                chunks.append(chunk)
            filename = unquote(Path(urlparse(resolved).path).name) or fallback_name
            content_type = response.headers.get(
                "content-type", media_type_from_filename(filename)
            )
            self.save_media(history_id, b"".join(chunks), content_type, filename)

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
            targets = sorted(
                model.targets,
                key=lambda target: (
                    target.provider != job.provider if job.provider else False
                ),
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
        if action_name in {"delete", "destroy"}:
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
        base_url = self.resolve_resource(runtime.config.base_url, runtime.config.id)
        return f"{base_url}{path}"

    def available_actions(self, provider: str) -> dict[str, ProviderAction]:
        runtime = self.providers[provider]
        actions = dict(runtime.config.actions)
        if runtime.config.lifecycle.start is not None:
            actions["start"] = runtime.config.lifecycle.start
        if runtime.config.lifecycle.stop is not None:
            actions["stop"] = runtime.config.lifecycle.stop
        return dict(sorted(actions.items()))

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
            if action_name in {"delete", "destroy", "stop"} and runtime.active_requests:
                raise RuntimeError("provider has active requests")
            if action_name == "stop":
                runtime.state = "stopping"
            response = await self.action(action, provider, action_name)
            if action_name == "deploy":
                runtime.state = "starting"
            elif action_name in {"delete", "destroy"}:
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
            response = await runtime.client.get(
                self.worker_url(runtime, runtime.config.health_path),
                headers={"Authorization": f"Bearer {runtime.config.api_key}"},
                timeout=10,
            )
            runtime.ready = response.is_success
        except (httpx.HTTPError, RuntimeError):
            runtime.ready = False
        runtime.state = "ready" if runtime.ready else "stopped"
        return runtime.ready

    async def ensure_ready(self, runtime: ProviderRuntime, request_id: str) -> None:
        if await self.check_ready(runtime):
            return
        action = runtime.config.lifecycle.start
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
                action = runtime.config.lifecycle.stop
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
    if kind == "saladcloud":
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
