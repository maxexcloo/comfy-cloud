from __future__ import annotations

import time
from importlib import resources
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from jinja2 import Environment, select_autoescape

from .control_dashboard import (
    SESSION_SECONDS,
    bearer_authorised,
    csrf_token,
    ui_authorised,
    valid_csrf,
)
from .control_http import error

MEDIA_LIBRARY_HTML = (
    resources.files("comfy_control").joinpath("media_library.html").read_text()
)
PAGE_SIZE = 20
TEMPLATES = Environment(autoescape=select_autoescape(("html", "xml")))

router = APIRouter(prefix="/media", tags=["dashboard"])


@router.get("", include_in_schema=False)
async def media_library(request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
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
        limit=PAGE_SIZE,
        offset=(page - 1) * PAGE_SIZE,
    )
    pages = max(1, (int(result["count"]) + PAGE_SIZE - 1) // PAGE_SIZE)
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


@router.post("/search", include_in_schema=False)
async def media_search(request: Request) -> Response:
    settings = request.app.state.settings
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


@router.get("/{asset_id}/content", include_in_schema=False)
async def media_content(asset_id: int, request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not (ui_authorised(request, settings) or bearer_authorised(request, settings)):
        return Response(status_code=401)
    asset = controller.store.media_asset(asset_id)
    if asset is None or not Path(asset.path).is_file():
        return error("media was not found", 404, "not_found")
    return FileResponse(
        asset.path,
        media_type=asset.content_type,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.get("/{asset_id}", include_in_schema=False)
async def media_detail(asset_id: int, request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not ui_authorised(request, settings):
        return Response(status_code=401)
    detail = controller.store.media_detail(asset_id)
    if detail is None:
        return error("media was not found", 404, "not_found")
    detail.pop("path", None)
    return JSONResponse(detail, headers={"Cache-Control": "no-store"})


@router.get("/{asset_id}/lineage", include_in_schema=False)
async def media_lineage(asset_id: int, request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not ui_authorised(request, settings):
        return Response(status_code=401)
    if controller.store.media_asset(asset_id) is None:
        return error("media was not found", 404, "not_found")
    return JSONResponse(controller.store.media_lineage(asset_id))
