from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from control.contracts import ProviderRoutes, ProviderRouteUpdate
from control.dashboard.auth import bearer_authorised, ui_authorised
from control.http import error
from control.preferences import ConfigurationConflict

router = APIRouter(prefix="/ops/provider-routes", tags=["operations"])


def authorised(request: Request) -> bool:
    settings = request.app.state.settings
    return ui_authorised(request, settings) or bearer_authorised(request, settings)


def route_list(controller) -> dict[str, object]:
    routes = controller.preferences.model_dump()["routes"]
    return {
        "images": routes.get("images", []),
        "revision": controller.configuration.revision,
        "videos": routes.get("videos", []),
    }


@router.get("", operation_id="get_provider_routes", response_model=ProviderRoutes)
async def get_provider_routes(request: Request) -> Response:
    if not authorised(request):
        return Response(status_code=401)
    return JSONResponse(route_list(request.app.state.controller))


@router.put("", operation_id="set_provider_routes", response_model=ProviderRoutes)
async def set_provider_routes(
    request: Request, update: ProviderRouteUpdate
) -> Response:
    if not authorised(request):
        return Response(status_code=401)
    controller = request.app.state.controller
    try:
        await controller.update_preferences(
            {
                "routes": {
                    "images": [target.model_dump() for target in update.images],
                    "videos": [target.model_dump() for target in update.videos],
                }
            },
            update.revision,
        )
    except ConfigurationConflict as exc:
        return error(str(exc), 409, "configuration_conflict")
    except (RuntimeError, TypeError, ValidationError, ValueError) as exc:
        return error(str(exc), 400, "invalid_provider_routes")
    controller.store.event("info", "provider routes updated")
    return JSONResponse(route_list(controller))
