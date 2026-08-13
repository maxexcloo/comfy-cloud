from __future__ import annotations

import asyncio
import json
import uuid
from importlib import resources
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from .control_dashboard import ui_authorised, valid_csrf
from .control_dashboard_templates import pagination_context, render_dashboard
from .control_http import error
from .control_preferences import ConfigurationConflict
from .provider_adapters import provider_panel_url

DASHBOARD_CSS = resources.files("comfy_control").joinpath("dashboard.css").read_text()
DASHBOARD_JS = resources.files("comfy_control").joinpath("dashboard.js").read_text()
PAGE_SIZE = 25
PROVIDER_SETTINGS_PREFIXES = {
    "cliproxyapi": "cliproxy_",
    "modal": "modal_",
    "runpod": "runpod_",
    "runpod-pod": "runpod_",
    "salad": "salad_",
    "vast": "vast_",
    "vast-pod": "vast_",
}
EVENT_SORTS = ("created_at", "level", "message", "provider", "request_id")
HISTORY_SORTS = ("created_at", "model", "operation", "provider", "status")


def sort_links(
    request: Request, columns: tuple[str, ...], current: str, direction: str
) -> dict[str, str]:
    values = [
        (key, value)
        for key, value in request.query_params.multi_items()
        if key not in {"direction", "page", "sort"}
    ]
    return {
        column: "?"
        + urlencode(
            [
                *values,
                ("sort", column),
                (
                    "direction",
                    "asc" if current == column and direction == "desc" else "desc",
                ),
            ]
        )
        for column in columns
    }


router = APIRouter(tags=["dashboard"])


@router.get("/assets/dashboard.css", include_in_schema=False)
async def dashboard_css() -> Response:
    return Response(
        DASHBOARD_CSS,
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/assets/dashboard.js", include_in_schema=False)
async def dashboard_js() -> Response:
    return Response(
        DASHBOARD_JS,
        media_type="text/javascript",
        headers={"Cache-Control": "no-store"},
    )


def settings_group(name: str) -> tuple[str, str]:
    if name.startswith("cliproxy_"):
        return "Providers", "CLI Proxy API"
    if name.startswith("modal_"):
        return "Providers", "Modal"
    if name.startswith("runpod_"):
        return "Providers", "RunPod"
    if name.startswith("salad_"):
        return "Providers", "SaladCloud"
    if name.startswith("vast_"):
        return "Providers", "Vast"
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


def prepare_field(field: dict[str, object]) -> dict[str, object]:
    prepared = dict(field)
    prepared["label"] = title_label(prepared["label"])
    if prepared["name"] == "modal_gpu":
        prepared["options"] = ["A100", "H100", "L40S"]
    return prepared


def provider_fields(
    description: dict[str, object], provider_id: str
) -> list[dict[str, object]]:
    prefix = PROVIDER_SETTINGS_PREFIXES.get(provider_id, "")
    label_prefixes = {
        "modal": "Modal ",
        "runpod": "RunPod ",
        "runpod-pod": "RunPod ",
        "salad": "SaladCloud ",
        "vast": "Vast.Ai ",
        "vast-pod": "Vast.Ai ",
    }
    cliproxy_labels = {
        "cliproxy_api_key": "API Key",
        "cliproxy_management_key": "Management Key",
        "cliproxy_url": "URL",
    }
    fields = []
    for field in description["fields"]:
        name = str(field["name"])
        if not prefix or not name.startswith(prefix):
            continue
        prepared = prepare_field(field)
        if name in cliproxy_labels:
            prepared["label"] = cliproxy_labels[name]
        elif label_prefix := label_prefixes.get(provider_id):
            prepared["label"] = str(prepared["label"]).removeprefix(label_prefix)
        fields.append(prepared)
    return sorted(
        fields,
        key=lambda item: str(item["label"]).casefold(),
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
    refresh = request.query_params.get("telemetry") == "true"
    if refresh:
        async with controller.configuration_lock:
            usage, statuses = await asyncio.gather(
                controller.usage(), controller.provider_statuses()
            )
    else:
        usage = {name: runtime.usage for name, runtime in controller.providers.items()}
        statuses = {
            name: {
                "panel_url": provider_panel_url(runtime.config, {}, runtime.base_url)
            }
            for name, runtime in controller.providers.items()
        }
    description = controller.describe_configuration()
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
            "settings": provider_fields(description, runtime.config.id),
            "state": runtime.state,
            "type": runtime.config.type,
            "usage": usage[runtime.config.id],
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
            "settings": provider_fields(description, str(provider["id"])),
            "state": "unconfigured",
            "type": provider["type"],
            "usage": {"status": "unconfigured"},
        }
        for provider in controller.available_providers
        if provider["id"] not in controller.providers
    ]
    return render_dashboard(
        request,
        "dashboard_providers.html",
        "providers",
        providers=items,
        refresh=refresh,
        revision=description["revision"],
    )


@router.get("/events", include_in_schema=False)
async def events(request: Request) -> Response:
    controller = request.app.state.controller
    if not ui_authorised(request, request.app.state.settings):
        return RedirectResponse("/login", status_code=303)
    try:
        page = max(1, int(request.query_params.get("page", "1")))
    except ValueError:
        return error("page must be a number", 400, "invalid_request")
    query = request.query_params.get("q", "").strip()
    level = request.query_params.get("level", "")
    provider = request.query_params.get("provider", "")
    sort = request.query_params.get("sort", "created_at")
    direction = request.query_params.get("direction", "desc")
    if sort not in EVENT_SORTS or direction not in {"asc", "desc"}:
        return error("invalid event sort", 400, "invalid_sort")
    result = controller.store.event_page(
        direction=direction,
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
        level=level,
        provider=provider,
        query=query,
        sort=sort,
    )
    facets = controller.store.event_facets()
    return render_dashboard(
        request,
        "dashboard_events.html",
        "events",
        events=pagination_context(request, result, page, PAGE_SIZE),
        filters={
            "level": level,
            "provider": provider,
            "q": query,
        },
        levels=facets["level"],
        providers=facets["provider"],
        sort=sort,
        sort_direction=direction,
        sort_links=sort_links(request, EVENT_SORTS, sort, direction),
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
    query = request.query_params.get("q", "").strip()
    operation = request.query_params.get("operation", "")
    model = request.query_params.get("model", "")
    provider = request.query_params.get("provider", "")
    status = request.query_params.get("status", "")
    sort = request.query_params.get("sort", "created_at")
    direction = request.query_params.get("direction", "desc")
    if sort not in HISTORY_SORTS or direction not in {"asc", "desc"}:
        return error("invalid history sort", 400, "invalid_sort")
    result = controller.store.history_page(
        direction=direction,
        limit=PAGE_SIZE,
        model=model,
        offset=(page - 1) * PAGE_SIZE,
        operation=operation,
        provider=provider,
        query=query,
        sort=sort,
        status=status,
    )
    facets = controller.store.history_facets()
    return render_dashboard(
        request,
        "dashboard_history.html",
        "history",
        filters={
            "model": model,
            "operation": operation,
            "provider": provider,
            "q": query,
            "status": status,
        },
        history=pagination_context(request, result, page, PAGE_SIZE),
        models=facets["model"],
        operations=facets["operation"],
        providers=facets["provider"],
        sort=sort,
        sort_direction=direction,
        sort_links=sort_links(request, HISTORY_SORTS, sort, direction),
        statuses=facets["status"],
    )


@router.get("/settings", include_in_schema=False)
async def settings_page(request: Request) -> Response:
    controller = request.app.state.controller
    if not ui_authorised(request, request.app.state.settings):
        return RedirectResponse("/login", status_code=303)
    description = controller.describe_configuration()
    unordered: dict[str, dict[str, list[dict[str, object]]]] = {}
    for field in description["fields"]:
        category, group = settings_group(str(field["name"]))
        if category == "Providers":
            continue
        prepared = prepare_field(field)
        unordered.setdefault(category, {}).setdefault(group, []).append(prepared)
    category_order = {"Providers": 0, "Routing": 1, "Worker": 2, "Control": 3}
    categories = {
        category: {
            group: sorted(fields, key=lambda item: str(item["label"]).casefold())
            for group, fields in sorted(groups.items())
        }
        for category, groups in sorted(
            unordered.items(),
            key=lambda item: (category_order.get(item[0], 99), item[0]),
        )
    }
    return render_dashboard(
        request,
        "dashboard_settings.html",
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


@router.post("/providers/{provider_id}/settings", include_in_schema=False)
async def save_provider_settings(provider_id: str, request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not ui_authorised(request, settings):
        return Response(status_code=401)
    prefix = PROVIDER_SETTINGS_PREFIXES.get(provider_id)
    if prefix is None:
        return error("unknown provider", 404, "unknown_provider")
    form = await request.form()
    if not valid_csrf(request, settings, str(form.get("csrf_token", ""))):
        return error("invalid CSRF token", 403, "invalid_csrf")
    values: dict[str, object] = {}
    for field in controller.describe_configuration()["fields"]:
        name = str(field["name"])
        if not name.startswith(prefix) or field.get("locked"):
            continue
        raw = str(form.get(name, ""))
        if field.get("secret") and not raw:
            continue
        if field.get("type") == "number":
            values[name] = float(raw) if "." in raw else int(raw)
        elif field.get("type") == "list":
            values[name] = [item.strip() for item in raw.split(",") if item.strip()]
        else:
            values[name] = raw
    try:
        await controller.update_preferences(values, int(str(form["revision"])))
    except (ConfigurationConflict, RuntimeError, TypeError, ValueError) as exc:
        return error(str(exc), 400, "invalid_configuration")
    return RedirectResponse("/providers?message=Settings+Saved", status_code=303)


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
    preferences = None
    if action_name == "deploy" and (gpu := str(form.get("gpu", "")).strip()):
        updates: dict[str, object] = {}
        if provider_id == "modal":
            updates["modal_gpu"] = gpu
        elif provider_id in {"runpod", "runpod-pod"}:
            updates["runpod_gpu_types"] = [gpu]
        elif provider_id == "salad":
            updates["salad_gpu_classes"] = [gpu]
        elif provider_id in {"vast", "vast-pod"}:
            memory = str(form.get("memory_gb", "")).strip()
            if memory:
                updates["vast_minimum_gpu_memory_gb"] = max(1, int(float(memory)))
        if updates:
            preferences = controller.preferences.model_copy(update=updates)
    try:
        await controller.run_provider_action(
            provider_id,
            action_name,
            uuid.uuid4().hex[:16],
            preferences=preferences,
        )
    except Exception as exc:  # noqa: BLE001 - provider SDK boundary
        return error(str(exc), 400, "provider_action_failed")
    return RedirectResponse(
        f"/providers?message={quote(f'{provider_id} {action_name} completed'.title())}",
        status_code=303,
    )


@router.get("/providers/{provider_id}/deployment-options", include_in_schema=False)
async def provider_deployment_options(provider_id: str, request: Request) -> Response:
    controller = request.app.state.controller
    if not ui_authorised(request, request.app.state.settings):
        return Response(status_code=401)
    try:
        options = await controller.deployment_options(provider_id)
    except (KeyError, RuntimeError) as exc:
        return error(str(exc), 400, "deployment_options_unavailable")
    return JSONResponse({"options": options, "provider": provider_id})


@router.get("/providers/{provider_id}/logs", include_in_schema=False)
async def provider_logs(provider_id: str, request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not ui_authorised(request, settings):
        return Response(status_code=401)
    try:
        await controller.provider_logs(provider_id, 1)
    except KeyError as exc:
        return error(str(exc), 404, "provider_not_found")

    async def stream():
        previous = ""
        while True:
            payload = json.dumps(
                await controller.provider_logs(provider_id, 500),
                separators=(",", ":"),
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
