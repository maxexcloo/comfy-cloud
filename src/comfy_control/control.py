from __future__ import annotations

import asyncio
import base64
import hmac
import json
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .control_config import (
    ControlFile,
    ControlSettings,
    LifecycleAction,
    Provider,
    RoutedModel,
    Target,
)
from .control_store import ControlStore, Job

RETRYABLE_STATUS = {502, 503, 504}


def error(message: str, status: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": "server_error" if status >= 500 else "invalid_request_error",
                "code": code,
            }
        },
    )


def exception_message(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"lifecycle request returned HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.RequestError):
        return f"provider connection failed ({type(exc).__name__})"
    return str(exc)


def bearer_authorised(request: Request, settings: ControlSettings) -> bool:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value, settings.api_key)


def ui_authorised(request: Request, settings: ControlSettings) -> bool:
    if bearer_authorised(request, settings):
        return True
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        username, _, password = base64.b64decode(value).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    expected_password = settings.ui_password or settings.api_key
    return hmac.compare_digest(username, settings.ui_username) and hmac.compare_digest(
        password, expected_password
    )


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

    async def action(self, action: LifecycleAction, provider: str) -> None:
        response = await self.lifecycle_client.request(
            action.method,
            action.url,
            headers=action.headers,
            json=action.json_body,
        )
        response.raise_for_status()
        self.store.event(
            "info", f"lifecycle {action.method} succeeded", provider=provider
        )

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
            await self.action(action, runtime.config.id)
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
                        await self.action(action, runtime.config.id)
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
        await self.ensure_ready(runtime, request_id)
        deadline = time.monotonic() + runtime.config.startup_timeout
        runtime.active_requests += 1
        runtime.state = "busy"
        try:
            while True:
                try:
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
                except httpx.HTTPError:
                    if time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(2)
                    continue
                if response.status_code not in RETRYABLE_STATUS:
                    runtime.ready = True
                    return response
                await response.aclose()
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"provider {runtime.config.id} remained unavailable"
                    )
                runtime.state = "starting"
                await asyncio.sleep(2)
        finally:
            runtime.active_requests -= 1
            runtime.last_used = time.monotonic()
            runtime.state = "ready" if runtime.ready else "unknown"


def rewrite_json_model(body: bytes, model: str) -> bytes:
    value = json.loads(body)
    if not isinstance(value, dict):
        raise TypeError("request body must be an object")
    value["model"] = model
    return json.dumps(value, separators=(",", ":")).encode()


def html() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Comfy Control</title><style>
:root{color-scheme:dark;font:15px system-ui;background:#101418;color:#e8edf2}body{max-width:1100px;margin:0 auto;padding:2rem}
h1{font-size:1.5rem}section{background:#181e24;border:1px solid #2c3640;border-radius:10px;margin:1rem 0;padding:1rem}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.55rem;border-bottom:1px solid #2c3640}th{color:#9fb0bf}
.ready,.completed{color:#67d391}.busy,.starting,.queued,.in_progress{color:#f2c166}.failed,.error{color:#ff7b72}
small{color:#9fb0bf}</style></head><body><h1>Comfy Control</h1><small id="updated">Loading…</small>
<section><h2>Providers</h2><table><thead><tr><th>Provider</th><th>State</th><th>Active</th><th>Idle</th></tr></thead><tbody id="providers"></tbody></table></section>
<section><h2>Jobs</h2><table><thead><tr><th>Job</th><th>Model</th><th>Provider</th><th>Status</th></tr></thead><tbody id="jobs"></tbody></table></section>
<section><h2>Events</h2><table><thead><tr><th>Time</th><th>Level</th><th>Provider</th><th>Message</th></tr></thead><tbody id="events"></tbody></table></section>
<script>const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function refresh(){const r=await fetch('/api/status');if(!r.ok)return;const d=await r.json();
providers.innerHTML=d.providers.map(p=>`<tr><td>${esc(p.id)}</td><td class="${esc(p.state)}">${esc(p.state)}</td><td>${p.active_requests}</td><td>${p.idle_seconds}s</td></tr>`).join('');
jobs.innerHTML=d.jobs.map(j=>`<tr><td>${esc(j.id)}</td><td>${esc(j.model)}</td><td>${esc(j.provider)}</td><td class="${esc(j.status)}">${esc(j.status)}</td></tr>`).join('');
events.innerHTML=d.events.map(e=>`<tr><td>${new Date(e.created_at*1000).toLocaleString()}</td><td class="${esc(e.level)}">${esc(e.level)}</td><td>${esc(e.provider)}</td><td>${esc(e.message)}</td></tr>`).join('');
updated.textContent='Updated '+new Date().toLocaleTimeString()}refresh();setInterval(refresh,5000)</script></body></html>"""


def create_app(settings: ControlSettings | None = None) -> FastAPI:
    settings = settings or ControlSettings.from_env()
    controller = Controller(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        for pending_job in controller.store.pending_jobs():
            controller.start_video(pending_job)
        reaper = asyncio.create_task(controller.idle_reaper())
        yield
        reaper.cancel()
        await asyncio.gather(reaper, return_exceptions=True)
        await controller.close()

    app = FastAPI(title="Comfy Control", version="0.1.0", lifespan=lifespan)
    app.state.controller = controller

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/")
    async def dashboard(request: Request) -> Response:
        if not ui_authorised(request, settings):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Comfy Control"'},
            )
        return HTMLResponse(html())

    @app.get("/api/status")
    async def status(request: Request) -> Response:
        if not ui_authorised(request, settings):
            return Response(status_code=401)
        return JSONResponse(
            {
                "providers": [
                    {
                        "id": runtime.config.id,
                        "state": runtime.state,
                        "active_requests": runtime.active_requests,
                        "idle_seconds": runtime.config.idle_seconds,
                    }
                    for runtime in controller.providers.values()
                ],
                "jobs": controller.store.jobs(50),
                "events": controller.store.events(100),
            }
        )

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        if not bearer_authorised(request, settings):
            return error("invalid API key", 401, "invalid_api_key")
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": model.id,
                        "object": "model",
                        "created": 0,
                        "owned_by": "comfy-control",
                    }
                    for model in controller.config.models
                ],
            }
        )

    async def generation(request: Request, operation: str, path: str) -> Response:
        if not bearer_authorised(request, settings):
            return error("invalid API key", 401, "invalid_api_key")
        body = await request.body()
        if len(body) > settings.maximum_request_bytes:
            return error("request body is too large", 413, "request_too_large")
        try:
            original = json.loads(body)
            model = controller.model(original.get("model", ""), operation)
        except (AttributeError, json.JSONDecodeError, ValueError) as exc:
            return error(str(exc), 400, "invalid_request")
        except KeyError as exc:
            return error(str(exc), 404, "model_not_found")
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"authorization", "content-length", "host"}
        }
        failures = []
        for target in model.targets:
            try:
                response = await controller.forward(
                    target,
                    request.method,
                    path,
                    rewrite_json_model(body, target.model),
                    headers,
                    request_id,
                )
            except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
                message = exception_message(exc)
                failures.append(f"{target.provider}: {message}")
                controller.store.event(
                    "error",
                    message,
                    provider=target.provider,
                    request_id=request_id,
                )
                continue
            if response.is_success:
                controller.store.event(
                    "info",
                    f"{operation} completed",
                    provider=target.provider,
                    request_id=request_id,
                )
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                    headers={
                        "x-comfy-provider": target.provider,
                        "x-request-id": request_id,
                    },
                )
            failures.append(f"{target.provider}: HTTP {response.status_code}")
            if response.status_code < 500 and response.status_code != 429:
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                )
        return error(
            "; ".join(failures) or "all providers failed", 502, "providers_failed"
        )

    @app.post("/v1/images/generations")
    async def image_generations(request: Request) -> Response:
        return await generation(request, "image_generation", "/v1/images/generations")

    @app.post("/v1/images/edits")
    async def image_edits(request: Request) -> Response:
        if not bearer_authorised(request, settings):
            return error("invalid API key", 401, "invalid_api_key")
        body = await request.body()
        if len(body) > settings.maximum_request_bytes:
            return error("request body is too large", 413, "request_too_large")
        try:
            form = await request.form()
            model_id = str(form.get("model", ""))
            model = controller.model(model_id, "image_edit")
        except ValueError as exc:
            return error(str(exc), 400, "invalid_request")
        except KeyError as exc:
            return error(str(exc), 404, "model_not_found")
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        fields: list[tuple[str, str]] = []
        files: list[tuple[str, tuple[str, bytes, str]]] = []
        for key, value in form.multi_items():
            if hasattr(value, "read"):
                files.append(
                    (
                        key,
                        (
                            value.filename or "upload",
                            await value.read(),
                            value.content_type or "application/octet-stream",
                        ),
                    )
                )
            elif key != "model":
                fields.append((key, str(value)))
        failures: list[str] = []
        for target in model.targets:
            encoded = httpx.Request(
                "POST",
                "http://multipart.invalid",
                data=dict(fields) | {"model": target.model},
                files=files,
            )
            try:
                response = await controller.forward(
                    target,
                    "POST",
                    "/v1/images/edits",
                    encoded.read(),
                    {"content-type": encoded.headers["content-type"]},
                    request_id,
                )
            except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
                failures.append(f"{target.provider}: {exception_message(exc)}")
                continue
            if response.is_success:
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                    headers={
                        "x-comfy-provider": target.provider,
                        "x-request-id": request_id,
                    },
                )
            failures.append(f"{target.provider}: HTTP {response.status_code}")
            if response.status_code < 500 and response.status_code != 429:
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                )
        return error(
            "; ".join(failures) or "all providers failed", 502, "providers_failed"
        )

    @app.post("/v1/videos")
    async def create_video(request: Request) -> Response:
        if not bearer_authorised(request, settings):
            return error("invalid API key", 401, "invalid_api_key")
        body = await request.body()
        if len(body) > settings.maximum_request_bytes:
            return error("request body is too large", 413, "request_too_large")
        try:
            if request.headers.get("content-type", "").startswith(
                "multipart/form-data"
            ):
                form = await request.form()
                model_id = str(form.get("model", ""))
            else:
                values = json.loads(body)
                if not isinstance(values, dict):
                    raise TypeError("request body must be an object")
                model_id = values.get("model", "")
            controller.model(model_id, "video_generation")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return error(str(exc), 400, "invalid_request")
        except KeyError as exc:
            return error(str(exc), 404, "model_not_found")
        public_id = f"video_{uuid.uuid4().hex}"
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            directory = controller.uploads_path / public_id
            directory.mkdir(parents=True)
            fields: list[tuple[str, str]] = []
            files: list[dict[str, str]] = []
            for key, value in form.multi_items():
                if hasattr(value, "read"):
                    path = directory / str(len(files))
                    path.write_bytes(await value.read())
                    files.append(
                        {
                            "content_type": value.content_type
                            or "application/octet-stream",
                            "field": key,
                            "filename": value.filename or "upload",
                            "path": str(path),
                        }
                    )
                else:
                    fields.append((key, str(value)))
            request_json = json.dumps(
                {"_control_multipart": {"fields": fields, "files": files}},
                separators=(",", ":"),
            )
        else:
            request_json = body.decode()
        controller.store.save_job(public_id, model_id, request_json)
        job = controller.store.job(public_id)
        assert job is not None
        controller.start_video(job)
        return JSONResponse(
            {
                "id": public_id,
                "object": "video",
                "model": model_id,
                "status": "queued",
                "created_at": job.created_at,
            }
        )

    @app.get("/v1/videos/{job_id}")
    async def get_video(job_id: str, request: Request) -> Response:
        return await video_request(job_id, request, content=False)

    @app.get("/v1/videos/{job_id}/content")
    async def video_content(job_id: str, request: Request) -> Response:
        return await video_request(job_id, request, content=True)

    async def video_request(
        job_id: str, request: Request, *, content: bool
    ) -> Response:
        if not bearer_authorised(request, settings):
            return error("invalid API key", 401, "invalid_api_key")
        job = controller.store.job(job_id)
        if job is None:
            return error("video job was not found", 404, "not_found")
        if not content:
            if job.response_json:
                return JSONResponse(json.loads(job.response_json))
            return JSONResponse(
                {
                    "id": job.id,
                    "object": "video",
                    "model": job.model,
                    "status": job.status,
                    "created_at": job.created_at,
                    "error": job.error,
                    "output_url": None,
                }
            )
        if job.status != "completed":
            return error("video is not ready", 409, "video_not_ready")
        response_data = json.loads(job.response_json or "{}")
        if output_url := response_data.get("output_url"):
            return Response(status_code=302, headers={"Location": output_url})
        runtime = controller.providers[job.provider]
        await controller.ensure_ready(runtime, job.id[:16])
        upstream = await runtime.client.send(
            runtime.client.build_request(
                "GET",
                f"/v1/videos/{job.upstream_id}/content",
                headers={"Authorization": f"Bearer {runtime.config.api_key}"},
            ),
            stream=True,
        )
        if upstream.is_redirect:
            location = upstream.headers.get("location", "")
            await upstream.aclose()
            return Response(
                status_code=upstream.status_code, headers={"Location": location}
            )
        return StreamingResponse(
            upstream.aiter_bytes(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
            background=BackgroundTask(upstream.aclose),
        )

    return app
