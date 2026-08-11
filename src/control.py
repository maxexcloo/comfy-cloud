from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.background import BackgroundTask

from .control_config import ControlSettings
from .controller import (
    Controller,
    exception_message,
    history_parameters,
    rewrite_json_model,
)


class RequestBodyTooLarge(ValueError):
    pass


LOGIN_MAXIMUM_BYTES = 16 * 1024
SESSION_COOKIE = "comfy_control_session"
SESSION_SECONDS = 12 * 60 * 60


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


def selected_targets(model, provider: str | None):
    if not provider:
        return model.targets
    targets = [target for target in model.targets if target.provider == provider]
    if not targets:
        raise ValueError(f"provider '{provider}' is unavailable for model '{model.id}'")
    return targets


def bearer_authorised(request: Request, settings: ControlSettings) -> bool:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(value, settings.api_key)


def session_secret(settings: ControlSettings) -> bytes:
    return (settings.ui_password or settings.api_key).encode()


def session_token(settings: ControlSettings, expires: int) -> str:
    payload = str(expires)
    signature = hmac.new(
        session_secret(settings), payload.encode(), hashlib.sha512
    ).hexdigest()
    return f"{payload}.{signature}"


def ui_authorised(request: Request, settings: ControlSettings) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    payload, separator, signature = token.partition(".")
    if not separator:
        return False
    try:
        expires = int(payload)
    except ValueError:
        return False
    expected = session_token(settings, expires).partition(".")[2]
    return expires >= int(time.time()) and hmac.compare_digest(signature, expected)


def secure_cookie(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").partition(",")[0].strip()
    return request.url.scheme == "https" or forwarded == "https"


def login_html(settings: ControlSettings, invalid: bool = False) -> str:
    message = (
        '<p class="error" role="alert">Incorrect username or password.</p>'
        if invalid
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Sign in · Comfy Control</title><style>
:root{{color-scheme:dark;font:15px system-ui;background:#101418;color:#e8edf2}}
body{{display:grid;margin:0;min-height:100vh;place-items:center}}main{{background:#181e24;border:1px solid #2c3640;border-radius:10px;padding:2rem;width:min(320px,calc(100vw - 4rem))}}
h1{{font-size:1.5rem;margin-top:0}}label{{display:grid;gap:.35rem;margin:1rem 0}}input{{background:#101418;border:1px solid #42576b;border-radius:5px;color:#e8edf2;padding:.65rem}}
button{{background:#263442;border:1px solid #42576b;border-radius:5px;color:#e8edf2;padding:.65rem;width:100%;cursor:pointer}}.error{{color:#ff7b72}}
</style></head><body><main><h1>Comfy Control</h1>{message}<form method="post" action="/login">
<label>Username<input name="username" value="{escape(settings.ui_username)}" autocomplete="username" required></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required autofocus></label>
<button type="submit">Sign in</button></form></main></body></html>"""


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
header{align-items:center;display:flex;justify-content:space-between}h1{font-size:1.5rem}section{background:#181e24;border:1px solid #2c3640;border-radius:10px;margin:1rem 0;padding:1rem}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.55rem;border-bottom:1px solid #2c3640}th{color:#9fb0bf}
button{background:#263442;border:1px solid #42576b;border-radius:5px;color:#e8edf2;margin:.15rem;padding:.35rem .55rem;cursor:pointer}
pre{overflow:auto;white-space:pre-wrap}
dialog{background:#181e24;border:1px solid #42576b;border-radius:10px;color:#e8edf2;max-height:90vh;max-width:min(1000px,90vw);padding:1rem}
dialog::backdrop{background:#000b}.viewer-head{display:flex;justify-content:space-between;gap:1rem}.viewer-media{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))}
.viewer-media img,.viewer-media video{background:#0b0e11;border-radius:6px;max-height:65vh;max-width:100%;object-fit:contain;width:100%}
.ready,.completed{color:#67d391}.busy,.starting,.queued,.in_progress{color:#f2c166}.failed,.error{color:#ff7b72}
small{color:#9fb0bf}</style></head><body><header><h1>Comfy Control</h1><form method="post" action="/logout"><button>Sign out</button></form></header><small id="updated">Loading…</small>
<section><h2>Providers</h2><table><thead><tr><th>Provider</th><th>Type</th><th>Resource</th><th>State</th><th>Usage / credit</th><th>Actions</th></tr></thead><tbody id="providers"></tbody></table></section>
<section><h2>Action Result</h2><pre id="actionResult">No action run.</pre></section>
<section><h2>History</h2><table><thead><tr><th>Time</th><th>Operation</th><th>Model</th><th>Provider</th><th>Status</th><th>Media</th><th></th></tr></thead><tbody id="historyRows"></tbody></table></section>
<section><h2>Events</h2><table><thead><tr><th>Time</th><th>Level</th><th>Provider</th><th>Message</th></tr></thead><tbody id="events"></tbody></table></section>
<dialog id="viewer"><div class="viewer-head"><div><h2 id="viewerTitle"></h2><small id="viewerMeta"></small></div><button id="viewerClose">Close</button></div><div id="viewerMedia" class="viewer-media"></div><h3>Parameters</h3><pre id="viewerParameters"></pre></dialog>
<script>const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let historyData=[];
async function refresh(){const r=await fetch('/api/status');if(r.status===401){location='/login';return}if(!r.ok)return;const d=await r.json();
providers.innerHTML=d.providers.map(p=>`<tr><td>${esc(p.platform??p.id)}<br><small>${esc(p.id)}</small></td><td>${esc(p.type)}</td><td>${esc(p.resource_id??'—')}</td><td class="${esc(p.state)}">${esc(p.state)} (${p.active_requests})</td><td>${p.usage.status==='ok'?(p.usage.metrics.map(m=>`${esc(m.label)}: ${esc(m.value)}${m.unit?' '+esc(m.unit):''}`).join('<br>')||'No metrics'):esc(p.usage.error??p.usage.status)}</td><td>${p.actions.map(a=>`<button data-provider="${esc(p.id)}" data-action="${esc(a.name)}" data-confirmation="${esc(a.confirmation)}">${esc(a.name)}</button>`).join('')}</td></tr>`).join('');
providers.querySelectorAll('button').forEach(b=>b.onclick=()=>act(b.dataset.provider,b.dataset.action,b.dataset.confirmation));
historyData=d.history;historyRows.innerHTML=historyData.map(j=>`<tr><td>${new Date(j.created_at*1000).toLocaleString()}</td><td>${esc(j.operation)}</td><td>${esc(j.model)}</td><td>${esc(j.provider||'—')}</td><td class="${esc(j.status)}">${esc(j.status)}</td><td>${j.media.length}</td><td><button data-history="${esc(j.id)}">View</button></td></tr>`).join('');
historyRows.querySelectorAll('button').forEach(b=>b.onclick=()=>showHistory(b.dataset.history));
events.innerHTML=d.events.map(e=>`<tr><td>${new Date(e.created_at*1000).toLocaleString()}</td><td class="${esc(e.level)}">${esc(e.level)}</td><td>${esc(e.provider)}</td><td>${esc(e.message)}</td></tr>`).join('');
updated.textContent='Updated '+new Date().toLocaleTimeString()}
function showHistory(id){const item=historyData.find(j=>j.id===id);if(!item)return;viewerTitle.textContent=item.model;viewerMeta.textContent=`${item.operation} · ${item.provider||'no provider'} · ${item.status}`;viewerParameters.textContent=JSON.stringify({...item.parameters,error:item.error},null,2);viewerMedia.replaceChildren();for(const media of item.media){const element=document.createElement(media.content_type.startsWith('video/')?'video':'img');element.src='/api/history/'+encodeURIComponent(item.id)+'/media/'+media.id;element.title=media.filename;if(element.tagName==='VIDEO')element.controls=true;viewerMedia.appendChild(element)}viewer.showModal()}
viewerClose.onclick=()=>viewer.close();viewer.onclick=e=>{if(e.target===viewer)viewer.close()};
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

    @app.get("/health")
    async def health() -> dict[str, int | str]:
        return {
            "status": "ready",
            "models": len(controller.config.models),
            "providers": len(controller.providers),
        }

    @app.get("/")
    async def dashboard(request: Request) -> Response:
        if not ui_authorised(request, settings):
            return RedirectResponse("/login", status_code=303)
        return HTMLResponse(html(), headers={"Cache-Control": "no-store"})

    @app.get("/login")
    async def login(request: Request) -> Response:
        if ui_authorised(request, settings):
            return RedirectResponse("/", status_code=303)
        return HTMLResponse(login_html(settings), headers={"Cache-Control": "no-store"})

    @app.post("/login")
    async def create_session(request: Request) -> Response:
        try:
            await limited_body(request, LOGIN_MAXIMUM_BYTES)
            form = await request.form()
        except RequestBodyTooLarge:
            return HTMLResponse(
                login_html(settings, invalid=True),
                status_code=413,
                headers={"Cache-Control": "no-store"},
            )
        username = str(form.get("username", ""))
        password = str(form.get("password", ""))
        expected_password = settings.ui_password or settings.api_key
        valid = hmac.compare_digest(
            username, settings.ui_username
        ) and hmac.compare_digest(password, expected_password)
        if not valid:
            return HTMLResponse(
                login_html(settings, invalid=True),
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )
        expires = int(time.time()) + SESSION_SECONDS
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            session_token(settings, expires),
            httponly=True,
            max_age=SESSION_SECONDS,
            path="/",
            samesite="lax",
            secure=secure_cookie(request),
        )
        return response

    @app.post("/logout")
    async def delete_session() -> Response:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/api/status")
    async def status(request: Request) -> Response:
        if not ui_authorised(request, settings):
            return Response(status_code=401)
        usage = await controller.usage()
        return JSONResponse(
            {
                "providers": [
                    {
                        "id": runtime.config.id,
                        "platform": runtime.config.platform,
                        "type": runtime.config.type,
                        "usage": usage[runtime.config.id],
                        "resource_id": controller.resource_id(runtime.config.id),
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
                "history": controller.store.histories(100),
                "jobs": controller.store.jobs(50),
                "events": controller.store.events(100),
            }
        )

    @app.get("/api/history")
    async def history(request: Request) -> Response:
        if not ui_authorised(request, settings):
            return Response(status_code=401)
        return JSONResponse({"data": controller.store.histories(500)})

    @app.get("/api/history/{history_id}/media/{media_id}")
    async def history_media(
        history_id: str, media_id: int, request: Request
    ) -> Response:
        if not ui_authorised(request, settings):
            return Response(status_code=401)
        item = controller.store.media(media_id)
        if (
            item is None
            or item.history_id != history_id
            or not Path(item.path).is_file()
        ):
            return error("media was not found", 404, "not_found")
        return FileResponse(
            item.path,
            media_type=item.content_type,
            headers={
                "Content-Disposition": f'inline; filename="{item.filename}"',
                "X-Content-Type-Options": "nosniff",
            },
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
            provider = str(original.get("provider", "")).strip() or None
            targets = selected_targets(model, provider)
        except (AttributeError, json.JSONDecodeError, ValueError) as exc:
            return error(str(exc), 400, "invalid_request")
        except KeyError as exc:
            return error(str(exc), 404, "model_not_found")
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        history_id = f"image_{uuid.uuid4().hex}"
        controller.store.save_history(
            history_id,
            operation,
            model.id,
            json.dumps(history_parameters(original), separators=(",", ":")),
        )
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"authorization", "content-length", "host"}
        }
        failures = []
        forwarded = dict(original)
        forwarded.pop("provider", None)
        forwarded_body = json.dumps(forwarded, separators=(",", ":")).encode()
        for target in targets:
            controller.store.update_history(
                history_id, "in_progress", provider=target.provider
            )
            try:
                response = await controller.forward(
                    target,
                    request.method,
                    path,
                    rewrite_json_model(forwarded_body, target.model),
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
                await controller.archive_images(history_id, target.provider, response)
                controller.store.update_history(
                    history_id, "completed", provider=target.provider
                )
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
                        "x-comfy-history-id": history_id,
                        "x-request-id": request_id,
                    },
                )
            failures.append(f"{target.provider}: HTTP {response.status_code}")
            if response.status_code < 500 and response.status_code != 429:
                controller.store.update_history(
                    history_id,
                    "failed",
                    provider=target.provider,
                    error=f"HTTP {response.status_code}",
                )
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                )
        failure = "; ".join(failures) or "all providers failed"
        controller.store.update_history(history_id, "failed", error=failure)
        return error(failure, 502, "providers_failed")

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
            provider = str(form.get("provider", "")).strip() or None
            targets = selected_targets(model, provider)
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
            elif key not in {"model", "provider"}:
                fields.append((key, str(value)))
        history_id = f"edit_{uuid.uuid4().hex}"
        parameters = dict(fields)
        parameters["model"] = model_id
        if provider:
            parameters["provider"] = provider
        parameters["input_media"] = [
            {
                "content_type": content_type,
                "filename": filename,
                "size": len(content),
            }
            for _, (filename, content, content_type) in files
        ]
        controller.store.save_history(
            history_id,
            "image_edit",
            model.id,
            json.dumps(history_parameters(parameters), separators=(",", ":")),
        )
        failures: list[str] = []
        for target in targets:
            controller.store.update_history(
                history_id, "in_progress", provider=target.provider
            )
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
                await controller.archive_images(history_id, target.provider, response)
                controller.store.update_history(
                    history_id, "completed", provider=target.provider
                )
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                    headers={
                        "x-comfy-provider": target.provider,
                        "x-comfy-history-id": history_id,
                        "x-request-id": request_id,
                    },
                )
            failures.append(f"{target.provider}: HTTP {response.status_code}")
            if response.status_code < 500 and response.status_code != 429:
                controller.store.update_history(
                    history_id,
                    "failed",
                    provider=target.provider,
                    error=f"HTTP {response.status_code}",
                )
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    media_type=response.headers.get("content-type"),
                )
        failure = "; ".join(failures) or "all providers failed"
        controller.store.update_history(history_id, "failed", error=failure)
        return error(failure, 502, "providers_failed")

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
                provider = str(form.get("provider", "")).strip() or None
            else:
                values = json.loads(body)
                if not isinstance(values, dict):
                    raise TypeError("request body must be an object")
                model_id = values.get("model", "")
                provider = str(values.get("provider", "")).strip() or None
            model = controller.model(model_id, "video_generation")
            selected_targets(model, provider)
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
                elif key != "provider":
                    fields.append((key, str(value)))
            request_json = json.dumps(
                {"_control_multipart": {"fields": fields, "files": files}},
                separators=(",", ":"),
            )
            parameters: object = {
                **dict(fields),
                "input_media": [
                    {
                        "content_type": item["content_type"],
                        "filename": item["filename"],
                        "size": Path(item["path"]).stat().st_size,
                    }
                    for item in files
                ],
            }
        else:
            forwarded = dict(values)
            forwarded.pop("provider", None)
            request_json = json.dumps(forwarded, separators=(",", ":"))
            parameters = values
        controller.store.save_job(public_id, model_id, request_json, provider=provider)
        controller.store.save_history(
            public_id,
            "video_generation",
            model_id,
            json.dumps(history_parameters(parameters), separators=(",", ":")),
        )
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
        archived = controller.store.media_for_history(job_id)
        if archived:
            item = archived[0]
            return FileResponse(item.path, media_type=item.content_type)
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
