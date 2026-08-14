from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from comfy_control.control.contracts import PreferenceDescription, PreferenceUpdate
from comfy_control.control.dashboard.auth import ui_authorised
from comfy_control.control.http import RequestBodyTooLarge, error, limited_body
from comfy_control.control.preferences import ConfigurationConflict

router = APIRouter(prefix="/ops/settings", tags=["operations"])


@router.get("", operation_id="get_preferences", response_model=PreferenceDescription)
async def get_preferences(request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not ui_authorised(request, settings):
        return Response(status_code=401)
    return JSONResponse(controller.describe_configuration())


@router.patch(
    "",
    operation_id="update_preferences",
    response_model=PreferenceDescription,
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {"schema": PreferenceUpdate.model_json_schema()}
            },
            "required": True,
        }
    },
)
async def update_preferences(request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not ui_authorised(request, settings):
        return Response(status_code=401)
    if request.headers.get("x-comfy-control-settings") != "update":
        return error("settings confirmation is missing", 400, "invalid_configuration")
    try:
        payload = json.loads(await limited_body(request, 256 * 1024))
        update = PreferenceUpdate.model_validate(payload)
        await controller.update_preferences(update.values, update.revision)
    except RequestBodyTooLarge:
        return error("settings request is too large", 413, "request_too_large")
    except ConfigurationConflict as exc:
        return error(str(exc), 409, "configuration_conflict")
    except RuntimeError as exc:
        status = 409 if "requests are active" in str(exc) else 500
        return error(str(exc), status, "configuration_update_failed")
    except ValidationError as exc:
        message = "; ".join(
            f"{'.'.join(map(str, item['loc']))}: {item['msg']}"
            for item in exc.errors(include_input=False, include_url=False)
        )
        return error(message, 400, "invalid_configuration")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return error(str(exc), 400, "invalid_configuration")
    controller.store.event("info", "control configuration updated")
    return JSONResponse(controller.describe_configuration())
