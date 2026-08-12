from __future__ import annotations

import asyncio
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
from .control_dashboard import (
    SESSION_SECONDS,
    bearer_authorised,
    csrf_token,
    ui_authorised,
    valid_csrf,
)
from .control_dashboard_sessions import router as dashboard_sessions_router
from .control_health import router as health_router
from .control_http import error
from .control_inference_routes import router as inference_router
from .control_operations_history import router as history_operations_router
from .control_operations_media import router as media_operations_router
from .control_operations_providers import router as provider_operations_router
from .control_operations_settings import router as settings_operations_router
from .control_operations_status import router as status_operations_router
from .control_preferences import ConfigurationConflict
from .controller import Controller

DASHBOARD_HTML = resources.files("comfy_control").joinpath("dashboard.html").read_text()
MEDIA_LIBRARY_HTML = (
    resources.files("comfy_control").joinpath("media_library.html").read_text()
)
TEMPLATES = Environment(autoescape=select_autoescape(("html", "xml")))
DASHBOARD_PAGE_SIZE = 20


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
    app.include_router(dashboard_sessions_router)
    app.include_router(health_router)
    app.include_router(inference_router)
    app.include_router(history_operations_router)
    app.include_router(media_operations_router)
    app.include_router(settings_operations_router)
    app.include_router(provider_operations_router)
    app.include_router(status_operations_router)

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
