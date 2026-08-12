from __future__ import annotations

import asyncio
import json
import time
import uuid
from importlib import resources
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from jinja2 import Environment, select_autoescape

from .control_dashboard import SESSION_SECONDS, csrf_token, ui_authorised, valid_csrf
from .control_http import error
from .control_preferences import ConfigurationConflict

DASHBOARD_HTML = resources.files("comfy_control").joinpath("dashboard.html").read_text()
DASHBOARD_CSS = resources.files("comfy_control").joinpath("dashboard.css").read_text()
DASHBOARD_JS = resources.files("comfy_control").joinpath("dashboard.js").read_text()
PAGE_SIZE = 25
TEMPLATES = Environment(autoescape=select_autoescape(("html", "xml")))

router = APIRouter(tags=["dashboard"])


@router.get("/assets/dashboard.css", include_in_schema=False)
async def dashboard_css() -> Response:
    return Response(
        DASHBOARD_CSS,
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/assets/dashboard.js", include_in_schema=False)
async def dashboard_js() -> Response:
    return Response(
        DASHBOARD_JS,
        media_type="text/javascript",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def page_context(request: Request, page: str) -> dict[str, object]:
    settings = request.app.state.settings
    return {
        "csrf_token": csrf_token(settings, int(time.time()) + SESSION_SECONDS),
        "message": request.query_params.get("message", ""),
        "page": page,
    }


def paginate(
    request: Request, items: list[dict[str, object]], page: int
) -> dict[str, object]:
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    query = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key != "page"
    ]
    suffix = f"&{urlencode(query)}" if query else ""
    return {
        "items": items[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
        "page": page,
        "pages": pages,
        "query_suffix": suffix,
    }


def matches(item: dict[str, object], query: str) -> bool:
    return not query or query in json.dumps(item, default=str).casefold()


def settings_group(name: str) -> tuple[str, str]:
    if name.startswith("cliproxy_"):
        return "Providers", "CLI Proxy"
    if name.startswith("modal_"):
        return "Providers", "Modal"
    if name.startswith("runpod_"):
        return "Providers", "RunPod"
    if name.startswith("salad_"):
        return "Providers", "SaladCloud"
    if name.startswith("vast_"):
        return "Providers", "Vast.ai"
    if name == "local_pod_url":
        return "Providers", "Local Pod"
    if name in {
        "civitai_token",
        "comfy_ui_password",
        "comfy_ui_username",
        "hf_token",
        "worker_api_key",
    }:
        return "Worker", "Credentials"
    if name == "routes":
        return "Routing", "Provider Routes"
    if name == "control_maximum_request_bytes":
        return "Control", "Request Limits"
    return "Worker", "Runtime"


def title_label(value: object) -> str:
    label = str(value).title()
    for title, name in {
        "Api": "API",
        "Cli": "CLI",
        "Comfyui": "ComfyUI",
        "Gb": "GB",
        "Gpu": "GPU",
        "Id": "ID",
        "Runpod": "RunPod",
        "Saladcloud": "SaladCloud",
        "Url": "URL",
    }.items():
        label = label.replace(title, name)
    return label


def render(request: Request, page: str, **context: object) -> HTMLResponse:
    template = TEMPLATES.from_string(DASHBOARD_HTML)
    return HTMLResponse(
        template.render(**page_context(request, page), **context),
        headers={"Cache-Control": "no-store"},
    )


@router.get("/", include_in_schema=False)
async def home(request: Request) -> Response:
    if not ui_authorised(request, request.app.state.settings):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/media", status_code=303)


@router.get("/providers", include_in_schema=False)
async def providers(request: Request) -> Response:
    controller = request.app.state.controller
    if not ui_authorised(request, request.app.state.settings):
        return RedirectResponse("/login", status_code=303)
    async with controller.configuration_lock:
        statuses = await controller.provider_statuses()
    items = [
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
    return render(request, "providers", providers=items)


@router.get("/events", include_in_schema=False)
async def events(request: Request) -> Response:
    controller = request.app.state.controller
    if not ui_authorised(request, request.app.state.settings):
        return RedirectResponse("/login", status_code=303)
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        return error("page must be a number", 400, "invalid_request")
    query = request.query_params.get("q", "").strip().casefold()
    level = request.query_params.get("level", "")
    provider = request.query_params.get("provider", "")
    all_items = controller.store.events(2000)
    items = [
        item
        for item in all_items
        if matches(item, query)
        and (not level or item["level"] == level)
        and (not provider or item["provider"] == provider)
    ]
    return render(
        request,
        "events",
        events=paginate(request, items, page),
        filters={
            "level": level,
            "provider": provider,
            "q": request.query_params.get("q", ""),
        },
        levels=sorted({str(item["level"]) for item in all_items}),
        providers=sorted(
            {str(item["provider"]) for item in all_items if item["provider"]}
        ),
    )


@router.get("/history", include_in_schema=False)
async def history(request: Request) -> Response:
    controller = request.app.state.controller
    if not ui_authorised(request, request.app.state.settings):
        return RedirectResponse("/login", status_code=303)
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        return error("page must be a number", 400, "invalid_request")
    query = request.query_params.get("q", "").strip().casefold()
    operation = request.query_params.get("operation", "")
    provider = request.query_params.get("provider", "")
    status = request.query_params.get("status", "")
    all_items = controller.store.histories(5000)
    items = [
        item
        for item in all_items
        if matches(item, query)
        and (not operation or item["operation"] == operation)
        and (not provider or item["provider"] == provider)
        and (not status or item["status"] == status)
    ]
    return render(
        request,
        "history",
        filters={
            "operation": operation,
            "provider": provider,
            "q": request.query_params.get("q", ""),
            "status": status,
        },
        history=paginate(request, items, page),
        operations=sorted({str(item["operation"]) for item in all_items}),
        providers=sorted(
            {str(item["provider"]) for item in all_items if item["provider"]}
        ),
        statuses=sorted({str(item["status"]) for item in all_items}),
    )


@router.get("/settings", include_in_schema=False)
async def settings_page(request: Request) -> Response:
    controller = request.app.state.controller
    if not ui_authorised(request, request.app.state.settings):
        return RedirectResponse("/login", status_code=303)
    description = controller.describe_configuration()
    categories: dict[str, dict[str, list[dict[str, object]]]] = {}
    for field in description["fields"]:
        category, group = settings_group(str(field["name"]))
        field["label"] = title_label(field["label"])
        if field["name"] == "modal_gpu":
            field["options"] = ["A100", "H100", "L40S"]
        categories.setdefault(category, {}).setdefault(group, []).append(field)
    return render(
        request,
        "settings",
        settings={"categories": categories, "revision": description["revision"]},
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
    for field in controller.describe_configuration()["fields"]:
        if field.get("locked"):
            continue
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
    return RedirectResponse("/settings?message=Settings+Saved", status_code=303)


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
        f"/providers?message={quote(f'{provider_id} {action_name} completed'.title())}",
        status_code=303,
    )


@router.get("/providers/{provider_id}/logs", include_in_schema=False)
async def provider_logs(provider_id: str, request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not ui_authorised(request, settings):
        return Response(status_code=401)
    try:
        controller.provider_logs(provider_id, 1)
    except KeyError as exc:
        return error(str(exc), 404, "provider_not_found")

    async def stream():
        previous = ""
        while True:
            payload = json.dumps(
                controller.provider_logs(provider_id, 500), separators=(",", ":")
            )
            if payload != previous:
                yield f"data: {payload}\n\n"
                previous = payload
            await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
