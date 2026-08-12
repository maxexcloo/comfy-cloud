from __future__ import annotations

import asyncio
import hmac
import json
import time
import uuid
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from jinja2 import Environment, select_autoescape

from .control_config import ControlSettings
from .control_contracts import (
    OperationsStatus,
    ProviderActionResult,
    ProviderTestRequest,
    ProviderTestResult,
)
from .control_dashboard import (
    SESSION_COOKIE,
    SESSION_SECONDS,
    bearer_authorised,
    csrf_token,
    login_html,
    secure_cookie,
    session_token,
    ui_authorised,
    valid_csrf,
)
from .control_http import RequestBodyTooLarge, error, limited_body
from .control_inference import (
    archived_image_content,
    canonical_parameters,
    internal_image_content,
    normalise_grok_image_options,
)
from .control_operations_history import router as history_operations_router
from .control_operations_media import router as media_operations_router
from .control_operations_providers import router as provider_operations_router
from .control_operations_settings import router as settings_operations_router
from .control_preferences import ConfigurationConflict
from .controller import (
    Controller,
    exception_message,
    history_parameters,
    rewrite_json_model,
)

DASHBOARD_HTML = resources.files("comfy_control").joinpath("dashboard.html").read_text()
MEDIA_LIBRARY_HTML = (
    resources.files("comfy_control").joinpath("media_library.html").read_text()
)
TEMPLATES = Environment(autoescape=select_autoescape(("html", "xml")))
DASHBOARD_PAGE_SIZE = 20
LOGIN_MAXIMUM_BYTES = 16 * 1024
PROVIDER_TEST_MAXIMUM_BYTES = 16 * 1024


def html() -> str:
    return DASHBOARD_HTML


def dashboard_pagination(view: str, page: int, count: int) -> str:
    pages = max(1, (count + DASHBOARD_PAGE_SIZE - 1) // DASHBOARD_PAGE_SIZE)
    links = ['<div class="pagination">']
    if page > 1:
        links.append(f'<a href="/?view={view}&page={page - 1}">Previous</a>')
    links.append(f"<span>Page {page} of {pages}</span>")
    if page < pages:
        links.append(f'<a href="/?view={view}&page={page + 1}">Next</a>')
    links.append("</div>")
    return "".join(links)


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

    app = FastAPI(title="Comfy Control", version="current", lifespan=lifespan)
    app.state.controller = controller
    app.state.settings = settings
    app.include_router(history_operations_router)
    app.include_router(media_operations_router)
    app.include_router(settings_operations_router)
    app.include_router(provider_operations_router)

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
        view = request.query_params.get("view", "providers")
        if view not in {"events", "history", "providers", "settings"}:
            return RedirectResponse("/?view=providers", status_code=303)
        try:
            page = max(1, int(request.query_params.get("page", "1")))
        except ValueError:
            return error("page must be a number", 400, "invalid_request")
        context: dict[str, object] = {
            "csrf_token": csrf_token(settings, int(time.time()) + SESSION_SECONDS),
            "events": [],
            "history": [],
            "message": request.query_params.get("message", ""),
            "pagination": "",
            "providers": [],
            "settings": {},
            "view": view,
        }
        if view == "providers":
            async with controller.configuration_lock:
                statuses = await controller.provider_statuses()
            context["providers"] = [
                {
                    "actions": [
                        {"name": name, "confirmation": action.confirmation}
                        for name, action in controller.available_actions(
                            runtime.config.id
                        ).items()
                    ],
                    "configured": True,
                    "id": runtime.config.id,
                    "panel_url": statuses[runtime.config.id]["panel_url"],
                    "platform": runtime.config.platform or runtime.config.id,
                    "resource_id": controller.resource_id(runtime.config.id),
                    "state": runtime.state,
                    "type": runtime.config.type,
                }
                for runtime in controller.providers.values()
            ] + [
                {
                    "actions": [],
                    "configured": False,
                    "id": provider["id"],
                    "panel_url": None,
                    "platform": provider["platform"],
                    "resource_id": None,
                    "state": "unconfigured",
                    "type": provider["type"],
                }
                for provider in controller.available_providers
                if provider["id"] not in controller.providers
            ]
        elif view == "history":
            count = controller.store.history_count()
            context["history"] = controller.store.histories(
                DASHBOARD_PAGE_SIZE, (page - 1) * DASHBOARD_PAGE_SIZE
            )
            context["pagination"] = dashboard_pagination(view, page, count)
        elif view == "events":
            count = controller.store.event_count()
            context["events"] = controller.store.events(
                DASHBOARD_PAGE_SIZE, (page - 1) * DASHBOARD_PAGE_SIZE
            )
            context["pagination"] = dashboard_pagination(view, page, count)
        else:
            description = controller.describe_configuration()
            sections: dict[str, list[dict[str, object]]] = {}
            for field in description["fields"]:
                sections.setdefault(str(field["section"]), []).append(field)
            context["settings"] = {
                "revision": description["revision"],
                "sections": sections,
            }
        template = TEMPLATES.from_string(DASHBOARD_HTML)
        return HTMLResponse(
            template.render(**context), headers={"Cache-Control": "no-store"}
        )

    @app.post("/settings", include_in_schema=False)
    async def save_settings(request: Request) -> Response:
        if not ui_authorised(request, settings):
            return Response(status_code=401)
        form = await request.form()
        if not valid_csrf(request, settings, str(form.get("csrf_token", ""))):
            return error("invalid CSRF token", 403, "invalid_csrf")
        values: dict[str, object] = {}
        description = controller.describe_configuration()
        for field in description["fields"]:
            name = str(field["name"])
            raw = str(form.get(name, ""))
            if field.get("secret") and not raw:
                continue
            if field.get("type") == "number":
                values[name] = float(raw) if "." in raw else int(raw)
            elif field.get("type") == "list":
                values[name] = [item.strip() for item in raw.split(",") if item.strip()]
            elif field.get("type") == "routes":
                values[name] = json.loads(raw)
            else:
                values[name] = raw
        try:
            await controller.update_preferences(values, int(str(form["revision"])))
        except (ConfigurationConflict, RuntimeError, TypeError, ValueError) as exc:
            return error(str(exc), 400, "invalid_configuration")
        return RedirectResponse(
            "/?view=settings&message=Settings+saved", status_code=303
        )

    @app.post("/providers/{provider_id}/actions/{action_name}", include_in_schema=False)
    async def dashboard_provider_action(
        provider_id: str, action_name: str, request: Request
    ) -> Response:
        if not ui_authorised(request, settings):
            return Response(status_code=401)
        form = await request.form()
        if not valid_csrf(request, settings, str(form.get("csrf_token", ""))):
            return error("invalid CSRF token", 403, "invalid_csrf")
        try:
            await controller.run_provider_action(
                provider_id, action_name, uuid.uuid4().hex[:16]
            )
        except (KeyError, RuntimeError, httpx.HTTPError) as exc:
            return error(str(exc), 400, "provider_action_failed")
        return RedirectResponse(
            f"/?view=providers&message={quote(f'{provider_id} {action_name} completed')}",
            status_code=303,
        )

    @app.get("/providers/{provider_id}/logs", include_in_schema=False)
    async def dashboard_provider_logs(provider_id: str, request: Request) -> Response:
        if not ui_authorised(request, settings):
            return RedirectResponse("/login", status_code=303)
        try:
            logs = controller.provider_logs(provider_id, 500)
        except KeyError as exc:
            return error(str(exc), 404, "provider_not_found")
        template = TEMPLATES.from_string(
            """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{{ provider }} logs · Comfy Control</title></head><body><a href="/?view=providers">← Providers</a><h1>{{ provider }} logs</h1><pre>{% for item in entries %}{{ item.created_at }} {{ item.level.upper() }} {{ item.message }}{% if item.request_id %} [{{ item.request_id }}]{% endif %}\n{% endfor %}</pre></body></html>"""
        )
        return HTMLResponse(
            template.render(provider=provider_id, entries=logs["entries"]),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/media", include_in_schema=False)
    async def media_library(request: Request) -> Response:
        if not ui_authorised(request, settings):
            return RedirectResponse("/login", status_code=303)
        try:
            page = max(1, int(request.query_params.get("page", "1")))
            include_inputs = request.query_params.get("include_inputs") == "true"
            query = request.query_params.get("q", "").strip()
            sort = request.query_params.get("sort", "relevance" if query else "newest")
            filters = []
            for name in ("model", "operation", "provider", "status"):
                if value := request.query_params.get(name):
                    filters.append({"path": name, "operator": "equals", "value": value})
            for value in request.query_params.getlist("filter"):
                parts = value.split("|", 2)
                if len(parts) == 3 and parts[0]:
                    parameter_value: object = parts[2]
                    try:
                        parameter_value = float(parts[2])
                    except ValueError:
                        pass
                    filters.append(
                        {
                            "path": parts[0],
                            "operator": parts[1],
                            "value": parameter_value,
                        }
                    )
        except ValueError as exc:
            return error(str(exc), 400, "invalid_filter")
        result = controller.store.media_library(
            query=query,
            filters=filters,
            include_inputs=include_inputs,
            sort=sort,
            limit=DASHBOARD_PAGE_SIZE,
            offset=(page - 1) * DASHBOARD_PAGE_SIZE,
        )
        pages = max(
            1, (int(result["count"]) + DASHBOARD_PAGE_SIZE - 1) // DASHBOARD_PAGE_SIZE
        )
        template = TEMPLATES.from_string(MEDIA_LIBRARY_HTML)
        return HTMLResponse(
            template.render(
                active_filters=request.query_params.getlist("filter"),
                facets=controller.store.media_facets(),
                include_inputs=include_inputs,
                items=result["data"],
                page=page,
                pages=pages,
                query=query,
                query_string=urlencode(
                    [
                        (key, value)
                        for key, value in request.query_params.multi_items()
                        if key != "page"
                    ]
                ),
                selections={
                    name: request.query_params.get(name, "")
                    for name in ("model", "operation", "provider", "status")
                },
                sort=sort,
                csrf_token=csrf_token(settings, int(time.time()) + SESSION_SECONDS),
            ),
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/media/search", include_in_schema=False)
    async def media_search(request: Request) -> Response:
        if not ui_authorised(request, settings):
            return Response(status_code=401)
        form = await request.form()
        if not valid_csrf(request, settings, str(form.get("csrf_token", ""))):
            return error("invalid CSRF token", 403, "invalid_csrf")
        values: list[tuple[str, str]] = []
        for name in (
            "filter",
            "include_inputs",
            "model",
            "operation",
            "provider",
            "q",
            "sort",
            "status",
        ):
            values.extend(
                (name, str(value)) for value in form.getlist(name) if str(value).strip()
            )
        return RedirectResponse(f"/media?{urlencode(values)}", status_code=303)

    @app.get("/media/{asset_id}/content", include_in_schema=False)
    async def media_asset_content(asset_id: int, request: Request) -> Response:
        if not (
            ui_authorised(request, settings) or bearer_authorised(request, settings)
        ):
            return Response(status_code=401)
        asset = controller.store.media_asset(asset_id)
        if asset is None or not Path(asset.path).is_file():
            return error("media was not found", 404, "not_found")
        return FileResponse(
            asset.path,
            media_type=asset.content_type,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.get("/media/{asset_id}", include_in_schema=False)
    async def media_asset_detail(asset_id: int, request: Request) -> Response:
        if not ui_authorised(request, settings):
            return RedirectResponse("/login", status_code=303)
        detail = controller.store.media_detail(asset_id)
        if detail is None:
            return error("media was not found", 404, "not_found")
        template = TEMPLATES.from_string(
            resources.files("comfy_control").joinpath("media_detail.html").read_text()
        )
        return HTMLResponse(
            template.render(item=detail), headers={"Cache-Control": "no-store"}
        )

    @app.get("/media/{asset_id}/lineage", include_in_schema=False)
    async def media_asset_lineage(asset_id: int, request: Request) -> Response:
        if not ui_authorised(request, settings):
            return Response(status_code=401)
        if controller.store.media_asset(asset_id) is None:
            return error("media was not found", 404, "not_found")
        return JSONResponse(controller.store.media_lineage(asset_id))

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

    @app.get(
        "/ops/status",
        tags=["operations"],
        operation_id="operations_status",
        response_model=OperationsStatus,
    )
    async def status(request: Request) -> Response:
        if not (
            ui_authorised(request, settings) or bearer_authorised(request, settings)
        ):
            return Response(status_code=401)
        try:
            event_page = max(1, int(request.query_params.get("event_page", "1")))
            history_page = max(1, int(request.query_params.get("history_page", "1")))
        except ValueError:
            return error("page must be a number", 400, "invalid_request")
        event_count = controller.store.event_count()
        history_count = controller.store.history_count()
        async with controller.configuration_lock:
            usage, provider_statuses = await asyncio.gather(
                controller.usage(), controller.provider_statuses()
            )
        return JSONResponse(
            {
                "providers": [
                    {
                        "id": runtime.config.id,
                        "configured": True,
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
                ]
                + [
                    {
                        "actions": [],
                        "active_requests": 0,
                        "configured": False,
                        "details": {},
                        "error": None,
                        "id": provider["id"],
                        "idle_seconds": 0,
                        "models": [],
                        "panel_url": None,
                        "platform": provider["platform"],
                        "resource_id": None,
                        "state": "not-configured",
                        "type": provider["type"],
                        "usage": {"status": "unconfigured"},
                    }
                    for provider in controller.available_providers
                    if provider["id"] not in controller.providers
                ],
                "history": controller.store.histories(
                    DASHBOARD_PAGE_SIZE,
                    (history_page - 1) * DASHBOARD_PAGE_SIZE,
                ),
                "history_pagination": {
                    "count": history_count,
                    "page": history_page,
                    "pages": max(
                        1,
                        (history_count + DASHBOARD_PAGE_SIZE - 1)
                        // DASHBOARD_PAGE_SIZE,
                    ),
                },
                "jobs": controller.store.jobs(50),
                "events": controller.store.events(
                    DASHBOARD_PAGE_SIZE,
                    (event_page - 1) * DASHBOARD_PAGE_SIZE,
                ),
                "event_pagination": {
                    "count": event_count,
                    "page": event_page,
                    "pages": max(
                        1,
                        (event_count + DASHBOARD_PAGE_SIZE - 1) // DASHBOARD_PAGE_SIZE,
                    ),
                },
            }
        )

    @app.post(
        "/ops/providers/{provider_id}/actions/{action_name}",
        tags=["operations"],
        operation_id="provider_action",
        response_model=ProviderActionResult,
    )
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
        request_id = uuid.uuid4().hex[:16]
        controller.store.event(
            "info",
            f"provider action {action_name} started",
            provider=provider_id,
            request_id=request_id,
        )
        try:
            result = await controller.run_provider_action(
                provider_id, action_name, request_id
            )
        except KeyError as exc:
            controller.store.event(
                "error",
                f"provider action {action_name} failed: {exception_message(exc)}",
                provider=provider_id,
                request_id=request_id,
            )
            return error(str(exc), 404, "action_not_found")
        except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
            controller.store.event(
                "error",
                f"provider action {action_name} failed: {exception_message(exc)}",
                provider=provider_id,
                request_id=request_id,
            )
            return error(exception_message(exc), 502, "action_failed")
        return JSONResponse(result)

    @app.post(
        "/ops/providers/{provider_id}/test",
        tags=["operations"],
        operation_id="test_provider",
        response_model=ProviderTestResult,
        openapi_extra={
            "requestBody": {
                "content": {
                    "application/json": {
                        "schema": ProviderTestRequest.model_json_schema()
                    }
                },
                "required": True,
            }
        },
    )
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
        attempt_id = controller.store.start_attempt(history_id, target.provider)
        started = time.monotonic()
        try:
            if controller.providers[target.provider].config.type == "proxy":
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
            else:
                width, height = size.split("x", 1)
                outputs = await controller.execute_internal(
                    target,
                    history_id,
                    "image_generation",
                    {"height": int(height), "prompt": prompt, "width": int(width)},
                )
                for content, content_type, filename in outputs:
                    controller.save_media(history_id, content, content_type, filename)
            controller.store.finish_attempt(attempt_id, "completed")
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
            controller.store.finish_attempt(attempt_id, "failed", message)
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
            body = await limited_body(
                request, controller.preferences.control_maximum_request_bytes
            )
        except RequestBodyTooLarge:
            return error("request body is too large", 413, "request_too_large")
        try:
            original = json.loads(body)
            if not isinstance(original, dict):
                raise TypeError("request body must be an object")
            original = normalise_grok_image_options(original)
            requested_model = str(original.get("model", ""))
            provider = str(original.get("provider", "")).strip() or None
            model, targets = controller.resolve_model(
                requested_model, operation, provider
            )
            canonical_parameters(original)
            count = int(original.get("n", 1))
            if count < 1 or count > 4:
                raise ValueError("n must be between 1 and 4")
            if original.get("response_format", "b64_json") not in {
                "b64_json",
                "url",
            }:
                raise ValueError("response_format must be b64_json or url")
        except (AttributeError, json.JSONDecodeError, TypeError, ValueError) as exc:
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
            attempt_id = controller.store.start_attempt(history_id, target.provider)
            try:
                is_proxy = controller.providers[target.provider].config.type == "proxy"
                if not is_proxy:
                    parameters = canonical_parameters(forwarded)
                    count = int(forwarded.get("n", 1))
                    outputs = []
                    for index in range(count):
                        indexed = dict(parameters)
                        if indexed.get("seed") is not None:
                            indexed["seed"] = int(indexed["seed"]) + index
                        result = await controller.execute_internal(
                            target,
                            f"{history_id}_{index}",
                            operation,
                            indexed,
                        )
                        outputs.extend(result)
                    response = None
                else:
                    outputs = None
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
                controller.store.finish_attempt(attempt_id, "failed", message)
                failures.append(f"{target.provider}: {message}")
                controller.store.event(
                    "error",
                    message,
                    provider=target.provider,
                    request_id=request_id,
                )
                continue
            if response is None or response.is_success:
                controller.store.finish_attempt(attempt_id, "completed")
                if response is not None:
                    await controller.archive_images(
                        history_id, target.provider, response
                    )
                controller.store.update_history(
                    history_id, "completed", provider=target.provider
                )
                controller.store.event(
                    "info",
                    f"{operation} completed",
                    provider=target.provider,
                    request_id=request_id,
                )
                content = (
                    archived_image_content(request, controller, history_id, response)
                    if response is not None
                    else internal_image_content(
                        request,
                        controller,
                        history_id,
                        outputs,
                        str(forwarded.get("response_format", "b64_json")),
                    )
                )
                return Response(
                    content=content,
                    status_code=response.status_code if response is not None else 200,
                    media_type=(
                        response.headers.get("content-type")
                        if response is not None
                        else "application/json"
                    ),
                    headers={
                        "x-comfy-provider": target.provider,
                        "x-comfy-history-id": history_id,
                        "x-request-id": request_id,
                    },
                )
            assert response is not None
            controller.store.finish_attempt(
                attempt_id, "failed", f"HTTP {response.status_code}"
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
            await limited_body(
                request, controller.preferences.control_maximum_request_bytes
            )
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
        normalised_fields = normalise_grok_image_options(
            {"model": model_id, **dict(fields)}
        )
        fields = [
            (key, str(value))
            for key, value in normalised_fields.items()
            if key not in {"model", "provider"}
        ]
        field_values = dict(fields)
        try:
            count = int(field_values.get("n", 1))
            if count < 1 or count > 4:
                raise ValueError("n must be between 1 and 4")
            if field_values.get("response_format", "b64_json") not in {
                "b64_json",
                "url",
            }:
                raise ValueError("response_format must be b64_json or url")
            if field_values.get("size", "") not in {"", "auto"}:
                raise ValueError("size is not supported for image edits")
            canonical_parameters(field_values)
        except (TypeError, ValueError) as exc:
            return error(str(exc), 400, "invalid_request")
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
        for field_name, (filename, content, content_type) in files:
            controller.save_input_media(
                history_id,
                content,
                content_type,
                filename,
                field_name,
            )
        failures: list[str] = []
        for target in targets:
            controller.store.update_history(
                history_id, "in_progress", provider=target.provider
            )
            attempt_id = controller.store.start_attempt(history_id, target.provider)
            encoded = httpx.Request(
                "POST",
                "http://multipart.invalid",
                data=dict(fields) | {"model": target.model},
                files=files,
            )
            try:
                is_proxy = controller.providers[target.provider].config.type == "proxy"
                if not is_proxy:
                    parameters = canonical_parameters(field_values)
                    outputs = []
                    for index in range(count):
                        indexed = dict(parameters)
                        if indexed.get("seed") is not None:
                            indexed["seed"] = int(indexed["seed"]) + index
                        result = await controller.execute_internal(
                            target,
                            f"{history_id}_{index}",
                            "image_edit",
                            indexed,
                            files,
                        )
                        outputs.extend(result)
                    response = None
                else:
                    outputs = None
                    response = await controller.forward(
                        target,
                        "POST",
                        "/v1/images/edits",
                        encoded.read(),
                        {"content-type": encoded.headers["content-type"]},
                        request_id,
                    )
            except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
                message = exception_message(exc)
                controller.store.finish_attempt(attempt_id, "failed", message)
                failures.append(f"{target.provider}: {message}")
                continue
            if response is None or response.is_success:
                controller.store.finish_attempt(attempt_id, "completed")
                if response is not None:
                    await controller.archive_images(
                        history_id, target.provider, response
                    )
                controller.store.update_history(
                    history_id, "completed", provider=target.provider
                )
                content = (
                    archived_image_content(request, controller, history_id, response)
                    if response is not None
                    else internal_image_content(
                        request,
                        controller,
                        history_id,
                        outputs,
                        field_values.get("response_format", "b64_json"),
                    )
                )
                return Response(
                    content=content,
                    status_code=response.status_code if response is not None else 200,
                    media_type=(
                        response.headers.get("content-type")
                        if response is not None
                        else "application/json"
                    ),
                    headers={
                        "x-comfy-provider": target.provider,
                        "x-comfy-history-id": history_id,
                        "x-request-id": request_id,
                    },
                )
            assert response is not None
            controller.store.finish_attempt(
                attempt_id, "failed", f"HTTP {response.status_code}"
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
            body = await limited_body(
                request, controller.preferences.control_maximum_request_bytes
            )
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
        files: list[dict[str, str]] = []
        remote_source: str | None = None
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            directory = controller.uploads_path / public_id
            directory.mkdir(parents=True)
            fields: list[tuple[str, str]] = []
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
            image_reference = forwarded.pop("image", None)
            if image_reference is None:
                request_json = json.dumps(forwarded, separators=(",", ":"))
                parameters = values
            else:
                try:
                    (
                        content,
                        content_type,
                        filename,
                        remote_source,
                    ) = await controller.remote_input(image_reference)
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    return error(str(exc), 400, "invalid_image")
                directory = controller.uploads_path / public_id
                directory.mkdir(parents=True)
                path = directory / "0"
                path.write_bytes(content)
                files = [
                    {
                        "content_type": content_type,
                        "field": "image",
                        "filename": filename,
                        "path": str(path),
                    }
                ]
                fields = [(str(key), str(value)) for key, value in forwarded.items()]
                request_json = json.dumps(
                    {"_control_multipart": {"fields": fields, "files": files}},
                    separators=(",", ":"),
                )
                parameters = dict(forwarded) | {
                    "input_media": [
                        {
                            "content_type": content_type,
                            "filename": filename,
                            "size": len(content),
                            "source_url": remote_source,
                        }
                    ]
                }
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
        if files:
            for item in files:
                controller.save_input_media(
                    public_id,
                    Path(item["path"]).read_bytes(),
                    item["content_type"],
                    item["filename"],
                    item["field"],
                    source_url=remote_source,
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
        return error("video output was not archived", 409, "video_output_missing")

    def current_openapi() -> dict[str, object]:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                description=(
                    "Current OpenAI-compatible inference and Comfy Control "
                    "operations contract."
                ),
                routes=app.routes,
            )
            schema["paths"] = {
                path: value
                for path, value in schema["paths"].items()
                if path.startswith(("/ops/", "/v1/"))
            }
            components = schema.setdefault("components", {})
            components.setdefault("securitySchemes", {})["bearerAuth"] = {
                "scheme": "bearer",
                "type": "http",
            }
            for value in schema["paths"].values():
                for operation in value.values():
                    operation["security"] = [{"bearerAuth": []}]
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = current_openapi  # type: ignore[method-assign]
    return app
