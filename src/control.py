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
PROVIDER_TEST_MAXIMUM_BYTES = 16 * 1024
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


def archived_image_content(
    request: Request, controller: Controller, history_id: str, response: httpx.Response
) -> bytes:
    try:
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return response.content
        archived = iter(controller.store.media_for_history(history_id))
        for item in data:
            if not isinstance(item, dict):
                continue
            if not (item.get("b64_json") or item.get("url")):
                continue
            media = next(archived, None)
            if media is not None and item.get("url"):
                item["url"] = str(
                    request.url_for(
                        "history_media", history_id=history_id, media_id=media.id
                    )
                )
        return json.dumps(payload, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        return response.content


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
button,input,select,textarea{background:#263442;border:1px solid #42576b;border-radius:5px;color:#e8edf2;padding:.45rem}.button,button{cursor:pointer;display:inline-block;margin:.15rem;text-decoration:none}
label{display:grid;gap:.35rem;margin:.8rem 0}textarea{min-height:7rem;min-width:min(36rem,75vw)}
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
<dialog id="providerDialog"><div class="viewer-head"><h2 id="providerTitle"></h2><button data-close="providerDialog">Close</button></div><pre id="providerDetails"></pre></dialog>
<dialog id="logsDialog"><div class="viewer-head"><div><h2 id="logsTitle"></h2><small>Controller events are always available and contain no provider credentials.</small></div><button data-close="logsDialog">Close</button></div><button id="logsRefresh">Refresh</button><pre id="logsContent"></pre></dialog>
<dialog id="testDialog"><div class="viewer-head"><h2 id="testTitle"></h2><button data-close="testDialog">Close</button></div><form id="testForm"><label>Model<select id="testModel" required></select></label><label>Size<select id="testSize"><option>512x512</option><option>768x768</option><option>1024x1024</option></select></label><label>Prompt<textarea id="testPrompt" maxlength="2000" required>A cinematic photograph of a wombat operating a compact GPU server, studio lighting</textarea></label><button id="testSubmit">Send test request</button></form><pre id="testResult">No request sent.</pre><div id="testMedia" class="viewer-media"></div></dialog>
<script>const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let historyData=[],providerData=[],testProvider='',logsProvider='';
async function refresh(){const r=await fetch('/api/status');if(r.status===401){location='/login';return}if(!r.ok)return;const d=await r.json();providerData=d.providers;
providers.innerHTML=d.providers.map(p=>`<tr><td>${esc(p.platform??p.id)}<br><small>${esc(p.id)}</small></td><td>${esc(p.type)}</td><td>${esc(p.resource_id??'—')}</td><td class="${esc(p.state)}">${esc(p.state)} (${p.active_requests})${p.error?`<br><small>${esc(p.error)}</small>`:''}</td><td>${p.usage.status==='ok'?(p.usage.metrics.map(m=>`${esc(m.label)}: ${esc(m.value)}${m.unit?' '+esc(m.unit):''}`).join('<br>')||'No provider metric exposed'):esc(p.usage.error??p.usage.status)}</td><td>${p.actions.map(a=>`<button class="provider-action" data-provider="${esc(p.id)}" data-action="${esc(a.name)}" data-confirmation="${esc(a.confirmation)}">${esc(a.name)}</button>`).join('')}<button class="details" data-provider="${esc(p.id)}">status</button><button class="logs" data-provider="${esc(p.id)}">logs</button>${p.models.length?`<button class="test" data-provider="${esc(p.id)}">test</button>`:''}${p.panel_url?`<a class="button" href="${esc(p.panel_url)}" target="_blank" rel="noopener noreferrer">panel</a>`:''}</td></tr>`).join('');
providers.querySelectorAll('.provider-action').forEach(b=>b.onclick=()=>act(b.dataset.provider,b.dataset.action,b.dataset.confirmation));providers.querySelectorAll('.details').forEach(b=>b.onclick=()=>showProvider(b.dataset.provider));providers.querySelectorAll('.logs').forEach(b=>b.onclick=()=>showLogs(b.dataset.provider));providers.querySelectorAll('.test').forEach(b=>b.onclick=()=>showTest(b.dataset.provider));
historyData=d.history;historyRows.innerHTML=historyData.map(j=>`<tr><td>${new Date(j.created_at*1000).toLocaleString()}</td><td>${esc(j.operation)}</td><td>${esc(j.model)}</td><td>${esc(j.provider||'—')}</td><td class="${esc(j.status)}">${esc(j.status)}</td><td>${j.media.length}</td><td><button data-history="${esc(j.id)}">View</button></td></tr>`).join('');historyRows.querySelectorAll('button').forEach(b=>b.onclick=()=>showHistory(b.dataset.history));events.innerHTML=d.events.map(e=>`<tr><td>${new Date(e.created_at*1000).toLocaleString()}</td><td class="${esc(e.level)}">${esc(e.level)}</td><td>${esc(e.provider)}</td><td>${esc(e.message)}</td></tr>`).join('');updated.textContent='Updated '+new Date().toLocaleTimeString()}
function showHistory(id){const item=historyData.find(j=>j.id===id);if(!item)return;viewerTitle.textContent=item.model;viewerMeta.textContent=`${item.operation} · ${item.provider||'no provider'} · ${item.status}`;viewerParameters.textContent=JSON.stringify({...item.parameters,error:item.error},null,2);viewerMedia.replaceChildren();for(const media of item.media){const element=document.createElement(media.content_type.startsWith('video/')?'video':'img');element.src='/api/history/'+encodeURIComponent(item.id)+'/media/'+media.id;element.title=media.filename;if(element.tagName==='VIDEO')element.controls=true;viewerMedia.appendChild(element)}viewer.showModal()}
function showProvider(id){const p=providerData.find(item=>item.id===id);if(!p)return;providerTitle.textContent=(p.platform??p.id)+' status';providerDetails.textContent=JSON.stringify({id:p.id,type:p.type,state:p.state,resource_id:p.resource_id,active_requests:p.active_requests,idle_seconds:p.idle_seconds,details:p.details,usage:p.usage,error:p.error},null,2);providerDialog.showModal()}
async function loadLogs(){logsContent.textContent='Loading…';const r=await fetch('/api/providers/'+encodeURIComponent(logsProvider)+'/logs?limit=200');const d=await r.json().catch(()=>({}));logsContent.textContent=r.ok?(d.entries.length?d.entries.map(e=>`${new Date(e.created_at*1000).toLocaleString()} ${String(e.level).toUpperCase()} ${e.message}${e.request_id?' ['+e.request_id+']':''}`).join('\n'):'No controller events recorded for this provider.'):d.error?.message??'Unable to load logs'}
function showLogs(id){logsProvider=id;logsTitle.textContent=id+' logs';logsDialog.showModal();loadLogs()}
function showTest(id){const p=providerData.find(item=>item.id===id);if(!p)return;testProvider=id;testTitle.textContent='Test '+(p.platform??id);testModel.innerHTML=p.models.map(model=>`<option>${esc(model)}</option>`).join('');testResult.textContent='No request sent.';testMedia.replaceChildren();testDialog.showModal()}
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>document.getElementById(b.dataset.close).close());viewerClose.onclick=()=>viewer.close();logsRefresh.onclick=loadLogs;
testForm.onsubmit=async e=>{e.preventDefault();testSubmit.disabled=true;testResult.textContent='Sending request; this may start billable compute…';testMedia.replaceChildren();const r=await fetch('/api/providers/'+encodeURIComponent(testProvider)+'/test',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({model:testModel.value,prompt:testPrompt.value,size:testSize.value})});const d=await r.json().catch(()=>({}));testResult.textContent=JSON.stringify(d,null,2);if(r.ok)for(const media of d.media){const element=document.createElement('img');element.src='/api/history/'+encodeURIComponent(d.history_id)+'/media/'+media.id;element.title=media.filename;testMedia.appendChild(element)}testSubmit.disabled=false;await refresh()};
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
        if not (
            ui_authorised(request, settings) or bearer_authorised(request, settings)
        ):
            return Response(status_code=401)
        usage, provider_statuses = await asyncio.gather(
            controller.usage(), controller.provider_statuses()
        )
        return JSONResponse(
            {
                "providers": [
                    {
                        "id": runtime.config.id,
                        "platform": runtime.config.platform,
                        "type": runtime.config.type,
                        "usage": usage[runtime.config.id],
                        "details": provider_statuses[runtime.config.id]["details"],
                        "error": provider_statuses[runtime.config.id].get("error"),
                        "panel_url": provider_statuses[runtime.config.id]["panel_url"],
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
                        "models": sorted(
                            model.id
                            for model in controller.config.models
                            if model.operation == "image_generation"
                            and any(
                                target.provider == runtime.config.id
                                for target in model.targets
                            )
                        ),
                    }
                    for runtime in controller.providers.values()
                ],
                "history": controller.store.histories(100),
                "jobs": controller.store.jobs(50),
                "events": controller.store.events(100),
            }
        )

    @app.get("/api/providers/{provider_id}/logs")
    async def provider_logs(provider_id: str, request: Request) -> Response:
        if not (
            ui_authorised(request, settings) or bearer_authorised(request, settings)
        ):
            return Response(status_code=401)
        try:
            limit = min(max(int(request.query_params.get("limit", "200")), 1), 500)
            return JSONResponse(controller.provider_logs(provider_id, limit))
        except ValueError:
            return error("log limit must be a number", 400, "invalid_request")
        except KeyError as exc:
            return error(str(exc), 404, "provider_not_found")

    @app.get("/api/history")
    async def history(request: Request) -> Response:
        if not ui_authorised(request, settings):
            return Response(status_code=401)
        return JSONResponse({"data": controller.store.histories(500)})

    @app.get("/api/history/{history_id}/media/{media_id}")
    async def history_media(
        history_id: str, media_id: int, request: Request
    ) -> Response:
        if not (
            ui_authorised(request, settings) or bearer_authorised(request, settings)
        ):
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
        if not (
            ui_authorised(request, settings) or bearer_authorised(request, settings)
        ):
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

    @app.post("/api/providers/{provider_id}/test")
    async def provider_test(provider_id: str, request: Request) -> Response:
        if not (
            ui_authorised(request, settings) or bearer_authorised(request, settings)
        ):
            return Response(status_code=401)
        try:
            body = await limited_body(request, PROVIDER_TEST_MAXIMUM_BYTES)
            values = json.loads(body)
            if not isinstance(values, dict):
                raise TypeError("request body must be an object")
            prompt = str(values.get("prompt", "")).strip()
            if not prompt or len(prompt) > 2000:
                raise ValueError("prompt must contain between 1 and 2000 characters")
            requested_model = str(values.get("model", ""))
            size = str(values.get("size", "512x512"))
            if size not in {"512x512", "768x768", "1024x1024"}:
                raise ValueError("unsupported test size")
            model, targets = controller.resolve_model(
                requested_model, "image_generation", provider_id
            )
        except RequestBodyTooLarge:
            return error("request body is too large", 413, "request_too_large")
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return error(str(exc), 400, "invalid_request")
        except KeyError as exc:
            return error(str(exc), 404, "model_not_found")
        target = targets[0]
        request_id = uuid.uuid4().hex[:16]
        history_id = f"test_{uuid.uuid4().hex}"
        parameters = {
            "model": requested_model,
            "prompt": prompt,
            "provider": provider_id,
            "size": size,
        }
        controller.store.save_history(
            history_id,
            "image_generation",
            model.id,
            json.dumps(parameters, separators=(",", ":")),
        )
        controller.store.update_history(
            history_id, "in_progress", provider=target.provider
        )
        started = time.monotonic()
        try:
            response = await controller.forward(
                target,
                "POST",
                "/v1/images/generations",
                json.dumps(
                    {
                        "model": target.model,
                        "n": 1,
                        "prompt": prompt,
                        "response_format": "b64_json",
                        "size": size,
                    },
                    separators=(",", ":"),
                ).encode(),
                {"content-type": "application/json"},
                request_id,
            )
            if not response.is_success:
                raise RuntimeError(f"provider returned HTTP {response.status_code}")
            await controller.archive_images(history_id, target.provider, response)
            controller.store.update_history(
                history_id, "completed", provider=target.provider
            )
            controller.store.event(
                "info",
                "dashboard test completed",
                provider=target.provider,
                request_id=request_id,
            )
        except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
            message = exception_message(exc)
            controller.store.update_history(history_id, "failed", error=message)
            controller.store.event(
                "error", message, provider=target.provider, request_id=request_id
            )
            return error(message, 502, "test_failed")
        media = controller.store.media_for_history(history_id)
        return JSONResponse(
            {
                "duration_seconds": round(time.monotonic() - started, 2),
                "history_id": history_id,
                "media": [
                    {
                        "content_type": item.content_type,
                        "filename": item.filename,
                        "id": item.id,
                        "size": item.size,
                    }
                    for item in media
                ],
                "model": model.id,
                "provider": target.provider,
                "status": "completed",
            }
        )

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        if not bearer_authorised(request, settings):
            return error("invalid API key", 401, "invalid_api_key")
        model_ids = {model.id for model in controller.config.models}
        for model in controller.config.models:
            for target in model.targets:
                provider = controller.providers[target.provider].config
                for provider_name in (provider.id, *provider.aliases):
                    model_ids.add(f"{provider_name}/{target.model}")
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": model_id,
                        "object": "model",
                        "created": 0,
                        "owned_by": "comfy-control",
                    }
                    for model_id in sorted(model_ids)
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
            requested_model = str(original.get("model", ""))
            provider = str(original.get("provider", "")).strip() or None
            model, targets = controller.resolve_model(
                requested_model, operation, provider
            )
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
                    content=archived_image_content(
                        request, controller, history_id, response
                    ),
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
            provider = str(form.get("provider", "")).strip() or None
            model, targets = controller.resolve_model(model_id, "image_edit", provider)
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
                    content=archived_image_content(
                        request, controller, history_id, response
                    ),
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
            model, targets = controller.resolve_model(
                str(model_id), "video_generation", provider
            )
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
        selected_provider = targets[0].provider if provider else None
        controller.store.save_job(
            public_id, model.id, request_json, provider=selected_provider
        )
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
                response_data = json.loads(job.response_json)
                if controller.store.media_for_history(job_id):
                    response_data["output_url"] = str(
                        request.url_for("video_content", job_id=job_id)
                    )
                return JSONResponse(response_data)
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
