from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .control_contracts import ProviderLogs
from .control_dashboard import bearer_authorised, ui_authorised

router = APIRouter(prefix="/ops/providers", tags=["operations"])


@router.get(
    "/{provider_id}/logs", operation_id="provider_logs", response_model=ProviderLogs
)
async def provider_logs(provider_id: str, request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not (ui_authorised(request, settings) or bearer_authorised(request, settings)):
        return Response(status_code=401)
    try:
        limit = min(max(int(request.query_params.get("limit", "200")), 1), 500)
        return JSONResponse(controller.provider_logs(provider_id, limit))
    except ValueError:
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_request",
                    "message": "log limit must be a number",
                }
            },
            status_code=400,
        )
    except KeyError as exc:
        return JSONResponse(
            {"error": {"code": "provider_not_found", "message": str(exc)}},
            status_code=404,
        )
