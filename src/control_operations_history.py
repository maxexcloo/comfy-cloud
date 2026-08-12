from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .control_contracts import HistoryPage
from .control_dashboard import bearer_authorised, ui_authorised
from .control_http import error

router = APIRouter(prefix="/ops/history", tags=["operations"])


@router.get("", operation_id="generation_history", response_model=HistoryPage)
async def generation_history(request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not ui_authorised(request, settings):
        return Response(status_code=401)
    try:
        page = max(1, int(request.query_params.get("page", "1")))
        page_size = min(max(1, int(request.query_params.get("page_size", "100"))), 500)
    except ValueError:
        return error("page must be a number", 400, "invalid_request")
    count = controller.store.history_count()
    return JSONResponse(
        {
            "data": controller.store.histories(page_size, (page - 1) * page_size),
            "pagination": {
                "count": count,
                "page": page,
                "pages": max(1, (count + page_size - 1) // page_size),
            },
        }
    )


@router.get("/{history_id}/media/{media_id}", operation_id="generation_media")
async def history_media(history_id: str, media_id: int, request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not (ui_authorised(request, settings) or bearer_authorised(request, settings)):
        return Response(status_code=401)
    item = controller.store.media(media_id)
    if item is None or item.history_id != history_id or not Path(item.path).is_file():
        return error("media was not found", 404, "not_found")
    return FileResponse(
        item.path,
        media_type=item.content_type,
        headers={
            "Content-Disposition": f'inline; filename="{item.filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
