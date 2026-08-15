from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from control.dashboard.auth import ui_authorised
from control.dashboard.rendering import (
    pagination_context,
    render_dashboard,
)
from control.http import error

EVENT_SORTS = ("created_at", "level", "message", "provider", "request_id")
HISTORY_SORTS = ("created_at", "model", "operation", "provider", "status")
PAGE_SIZE = 25

router = APIRouter(tags=["dashboard"])


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
        filters={"level": level, "provider": provider, "q": query},
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
