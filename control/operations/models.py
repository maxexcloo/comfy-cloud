from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from catalogue.profiles import profile_details
from control import registry as control_registry
from control.contracts import ModelPackageList, ModelPackageUpdate
from control.dashboard.auth import bearer_authorised, ui_authorised
from control.http import error
from control.preferences import ConfigurationConflict

router = APIRouter(prefix="/ops/model-packages", tags=["operations"])


def authorised(request: Request) -> bool:
    settings = request.app.state.settings
    return ui_authorised(request, settings) or bearer_authorised(request, settings)


def package_list(controller) -> dict[str, object]:
    installed = set(controller.preferences.model_profiles)
    return {
        "data": [
            {
                "id": package,
                "installed": package in installed,
                "operations": sorted(operations),
                **profile_details(package),
            }
            for package, operations in sorted(control_registry.MODEL_PACKAGES.items())
            if package != "grok-imagine"
        ],
        "revision": controller.configuration.revision,
    }


@router.get("", operation_id="list_model_packages", response_model=ModelPackageList)
async def list_model_packages(request: Request) -> Response:
    if not authorised(request):
        return Response(status_code=401)
    return JSONResponse(package_list(request.app.state.controller))


@router.put("", operation_id="set_model_packages", response_model=ModelPackageList)
async def set_model_packages(request: Request, update: ModelPackageUpdate) -> Response:
    if not authorised(request):
        return Response(status_code=401)
    controller = request.app.state.controller
    available = set(control_registry.MODEL_PACKAGES) - {"grok-imagine"}
    unknown = sorted(set(update.installed) - available)
    if unknown:
        return error(
            f"unknown model packages: {', '.join(unknown)}",
            400,
            "invalid_model_packages",
        )
    try:
        await controller.update_preferences(
            {"model_profiles": sorted(set(update.installed))}, update.revision
        )
    except ConfigurationConflict as exc:
        return error(str(exc), 409, "configuration_conflict")
    except (RuntimeError, TypeError, ValidationError, ValueError) as exc:
        return error(str(exc), 400, "invalid_model_packages")
    controller.store.event("info", "worker model packages updated")
    return JSONResponse(package_list(controller))
