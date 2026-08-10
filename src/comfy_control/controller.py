from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .control_config import (
    ControlFile,
    ControlSettings,
    LifecycleAction,
    Provider,
    RoutedModel,
    Target,
)
from .control_store import ControlStore, Job


def exception_message(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"lifecycle request returned HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.RequestError):
        return f"provider connection failed ({type(exc).__name__})"
    return str(exc)


@dataclass
class ProviderRuntime:
    config: Provider
    client: httpx.AsyncClient
    active_requests: int = 0
    last_used: float = field(default_factory=time.monotonic)
    ready: bool = False
    state: str = "unknown"
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Controller:
    def __init__(self, settings: ControlSettings):
        self.settings = settings
        self.config = ControlFile.load(settings.config_file)
        self.models = {model.id: model for model in self.config.models}
        self.providers = {
            provider.id: ProviderRuntime(
                config=provider,
                client=httpx.AsyncClient(
                    base_url=provider.base_url,
                    follow_redirects=False,
                    timeout=provider.request_timeout,
                ),
            )
            for provider in self.config.providers
        }
        self.lifecycle_client = httpx.AsyncClient(timeout=60)
        self.store = ControlStore(settings.database_path)
        self.uploads_path = settings.database_path.parent / "uploads"
        self.video_tasks: set[asyncio.Task[None]] = set()

    async def close(self) -> None:
        for task in self.video_tasks:
            task.cancel()
        await asyncio.gather(*self.video_tasks, return_exceptions=True)
        await asyncio.gather(
            *(runtime.client.aclose() for runtime in self.providers.values())
        )
        await self.lifecycle_client.aclose()
        self.store.close()

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
                    data["id"] = job.id
                    data["model"] = job.model
                    self.store.update_job(
                        job.id,
                        "completed",
                        provider=target.provider,
                        upstream_id=upstream_id,
                        response_json=json.dumps(data, separators=(",", ":")),
                    )
                    self.store.event(
                        "info",
                        "video completed",
                        provider=target.provider,
                        request_id=request_id,
                    )
                    self.remove_uploads(job.id)
                    return
                message = data.get("error") or f"HTTP {response.status_code}"
                failures.append(f"{target.provider}: {message}")
            failure = "; ".join(failures) or "all providers failed"
            self.store.update_job(job.id, "failed", error=failure)
            self.remove_uploads(job.id)
        except Exception as exc:  # noqa: BLE001 - durable task boundary
            message = exception_message(exc)
            self.store.update_job(job.id, "failed", error=message)
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
        self, action: LifecycleAction, provider: str, action_name: str = "lifecycle"
    ) -> httpx.Response:
        response = await self.lifecycle_client.request(
            action.method,
            action.url,
            headers=action.headers,
            json=action.json_body,
        )
        response.raise_for_status()
        self.store.event(
            "info", f"provider action {action_name} succeeded", provider=provider
        )
        return response

    def available_actions(self, provider: str) -> dict[str, LifecycleAction]:
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
            if action_name == "stop":
                runtime.ready = False
                runtime.state = "stopped"
        content_type = response.headers.get("content-type", "")
        body: object | None = None
        if response.content and len(response.content) <= 64 * 1024:
            if "application/json" in content_type:
                try:
                    body = response.json()
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
                runtime.config.health_path,
                headers={"Authorization": f"Bearer {runtime.config.api_key}"},
                timeout=10,
            )
            runtime.ready = response.is_success
        except httpx.HTTPError:
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
                path,
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
