from __future__ import annotations

import json
import time
import uuid
from importlib import resources
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, select_autoescape

from .control_dashboard import (
    SESSION_SECONDS,
    csrf_token,
    ui_authorised,
    valid_csrf,
)
from .control_http import error
from .control_preferences import ConfigurationConflict

DASHBOARD_HTML = resources.files("comfy_control").joinpath("dashboard.html").read_text()
PAGE_SIZE = 20
TEMPLATES = Environment(autoescape=select_autoescape(("html", "xml")))

router = APIRouter(tags=["dashboard"])


def pagination(view: str, page: int, count: int) -> str:
    pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
    links = ['<div class="pagination">']
    if page > 1:
        links.append(f'<a href="/?view={view}&page={page - 1}">Previous</a>')
    links.append(f"<span>Page {page} of {pages}</span>")
    if page < pages:
        links.append(f'<a href="/?view={view}&page={page + 1}">Next</a>')
    links.append("</div>")
    return "".join(links)


@router.get("/")
async def dashboard(request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
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
            PAGE_SIZE, (page - 1) * PAGE_SIZE
        )
        context["pagination"] = pagination(view, page, count)
    elif view == "events":
        count = controller.store.event_count()
        context["events"] = controller.store.events(PAGE_SIZE, (page - 1) * PAGE_SIZE)
        context["pagination"] = pagination(view, page, count)
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


@router.post("/settings", include_in_schema=False)
async def save_settings(request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
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
    return RedirectResponse("/?view=settings&message=Settings+saved", status_code=303)


@router.post("/providers/{provider_id}/actions/{action_name}", include_in_schema=False)
async def provider_action(
    provider_id: str, action_name: str, request: Request
) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
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


@router.get("/providers/{provider_id}/logs", include_in_schema=False)
async def provider_logs(provider_id: str, request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
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
