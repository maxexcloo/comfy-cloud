from __future__ import annotations

import asyncio
import base64
import hmac
import json
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from .control_config import ControlSettings
from .controller import Controller, exception_message, rewrite_json_model


class RequestBodyTooLarge(ValueError):
    pass


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


async def limited_body(request: Request, maximum_bytes: int) -> bytes:
    try:
        content_length = int(request.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    if content_length > maximum_bytes:
        raise RequestBodyTooLarge
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > maximum_bytes:
            raise RequestBodyTooLarge
    request._body = bytes(body)  # Starlette form parsing reuses the bounded body.
    return request._body


def html() -> str:
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Comfy Control</title><style>
:root{color-scheme:dark;font:15px system-ui;background:#101418;color:#e8edf2}body{max-width:1100px;margin:0 auto;padding:2rem}
h1{font-size:1.5rem}section{background:#181e24;border:1px solid #2c3640;border-radius:10px;margin:1rem 0;padding:1rem}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.55rem;border-bottom:1px solid #2c3640}th{color:#9fb0bf}
button{background:#263442;border:1px solid #42576b;border-radius:5px;color:#e8edf2;margin:.15rem;padding:.35rem .55rem;cursor:pointer}
pre{overflow:auto;white-space:pre-wrap}
.ready,.completed{color:#67d391}.busy,.starting,.queued,.in_progress{color:#f2c166}.failed,.error{color:#ff7b72}
small{color:#9fb0bf}</style></head><body><h1>Comfy Control</h1><small id="updated">Loading…</small>
<section><h2>Providers</h2><table><thead><tr><th>Provider</th><th>State</th><th>Active</th><th>Idle</th><th>Actions</th></tr></thead><tbody id="providers"></tbody></table></section>
<section><h2>Action Result</h2><pre id="actionResult">No action run.</pre></section>
<section><h2>Jobs</h2><table><thead><tr><th>Job</th><th>Model</th><th>Provider</th><th>Status</th></tr></thead><tbody id="jobs"></tbody></table></section>
<section><h2>Events</h2><table><thead><tr><th>Time</th><th>Level</th><th>Provider</th><th>Message</th></tr></thead><tbody id="events"></tbody></table></section>
<script>const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function refresh(){const r=await fetch('/api/status');if(!r.ok)return;const d=await r.json();
providers.innerHTML=d.providers.map(p=>`<tr><td>${esc(p.id)}</td><td class="${esc(p.state)}">${esc(p.state)}</td><td>${p.active_requests}</td><td>${p.idle_seconds}s</td><td>${p.actions.map(a=>`<button data-provider="${esc(p.id)}" data-action="${esc(a.name)}" data-confirmation="${esc(a.confirmation)}">${esc(a.name)}</button>`).join('')}</td></tr>`).join('');
providers.querySelectorAll('button').forEach(b=>b.onclick=()=>act(b.dataset.provider,b.dataset.action,b.dataset.confirmation));
jobs.innerHTML=d.jobs.map(j=>`<tr><td>${esc(j.id)}</td><td>${esc(j.model)}</td><td>${esc(j.provider)}</td><td class="${esc(j.status)}">${esc(j.status)}</td></tr>`).join('');
events.innerHTML=d.events.map(e=>`<tr><td>${new Date(e.created_at*1000).toLocaleString()}</td><td class="${esc(e.level)}">${esc(e.level)}</td><td>${esc(e.provider)}</td><td>${esc(e.message)}</td></tr>`).join('');
updated.textContent='Updated '+new Date().toLocaleTimeString()}
async function act(provider,action,confirmation){if(confirmation&&confirmation!=='null'&&!confirm(confirmation))return;const key=provider+'/'+action;const r=await fetch('/api/providers/'+encodeURIComponent(provider)+'/actions/'+encodeURIComponent(action),{method:'POST',headers:{'x-comfy-control-action':key}});const d=await r.json().catch(()=>({}));actionResult.textContent=JSON.stringify(d,null,2);if(!r.ok)alert(d.error?.message??'Provider action failed');await refresh()}refresh();setInterval(refresh,5000)</script></body></html>"""


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
                        "actions": [
                            {
                                "name": name,
                                "confirmation": action.confirmation,
                            }
                            for name, action in controller.available_actions(
                                runtime.config.id
                            ).items()
                        ],
                        "idle_seconds": runtime.config.idle_seconds,
                    }
                    for runtime in controller.providers.values()
                ],
                "jobs": controller.store.jobs(50),
                "events": controller.store.events(100),
            }
        )

    @app.post("/api/providers/{provider_id}/actions/{action_name}")
    async def provider_action(
        provider_id: str, action_name: str, request: Request
    ) -> Response:
        if not ui_authorised(request, settings):
            return Response(status_code=401)
        expected = f"{provider_id}/{action_name}"
        if not hmac.compare_digest(
            request.headers.get("x-comfy-control-action", ""), expected
        ):
            return error(
                "provider action confirmation is missing", 400, "invalid_action"
            )
        try:
            result = await controller.run_provider_action(
                provider_id, action_name, uuid.uuid4().hex[:16]
            )
        except KeyError as exc:
            return error(str(exc), 404, "action_not_found")
        except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
            return error(exception_message(exc), 502, "action_failed")
        return JSONResponse(result)

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
        try:
            body = await limited_body(request, settings.maximum_request_bytes)
        except RequestBodyTooLarge:
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
        try:
            await limited_body(request, settings.maximum_request_bytes)
        except RequestBodyTooLarge:
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
        try:
            body = await limited_body(request, settings.maximum_request_bytes)
        except RequestBodyTooLarge:
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
