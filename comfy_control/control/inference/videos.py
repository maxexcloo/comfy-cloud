from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from comfy_control.control.contracts import VideoCreateRequest, VideoJob
from comfy_control.control.dashboard.auth import bearer_authorised
from comfy_control.control.http import RequestBodyTooLarge, error, limited_body
from comfy_control.control.service import history_parameters

router = APIRouter(prefix="/v1", tags=["inference"])


@router.post(
    "/videos",
    operation_id="create_video",
    response_model=VideoJob,
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {"schema": VideoCreateRequest.model_json_schema()},
                "multipart/form-data": {
                    "schema": {
                        "additionalProperties": True,
                        "properties": {
                            "image": {"format": "binary", "type": "string"},
                            "model": {"type": "string"},
                            "prompt": {"type": "string"},
                            "provider": {"type": "string"},
                        },
                        "required": ["model", "prompt"],
                        "type": "object",
                    }
                },
            },
            "required": True,
        }
    },
)
async def create_video(request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not bearer_authorised(request, settings):
        return error("invalid API key", 401, "invalid_api_key")
    try:
        body = await limited_body(request, controller.preferences.maximum_request_bytes)
    except RequestBodyTooLarge:
        return error("request body is too large", 413, "request_too_large")
    try:
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            form = await request.form()
            model_id = str(form.get("model", ""))
            provider = str(form.get("provider", "")).strip() or None
        else:
            values = json.loads(body)
            if not isinstance(values, dict):
                raise TypeError("request body must be an object")
            model_id = values.get("model", "")
            provider = str(values.get("provider", "")).strip() or None
        model, targets = controller.resolve_model(
            str(model_id), "video_generation", provider
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return error(str(exc), 400, "invalid_request")
    except KeyError as exc:
        return error(str(exc), 404, "model_not_found")
    public_id = f"video_{uuid.uuid4().hex}"
    files: list[dict[str, str]] = []
    remote_source: str | None = None
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        directory = controller.media.uploads_path / public_id
        directory.mkdir(parents=True)
        fields: list[tuple[str, str]] = []
        for key, value in form.multi_items():
            if hasattr(value, "read"):
                path = directory / str(len(files))
                path.write_bytes(await value.read())
                files.append(
                    {
                        "content_type": value.content_type
                        or "application/octet-stream",
                        "field": key,
                        "filename": value.filename or "upload",
                        "path": str(path),
                    }
                )
            elif key != "provider":
                fields.append((key, str(value)))
        request_json = json.dumps(
            {"_control_multipart": {"fields": fields, "files": files}},
            separators=(",", ":"),
        )
        parameters: object = {
            **dict(fields),
            "input_media": [
                {
                    "content_type": item["content_type"],
                    "filename": item["filename"],
                    "size": Path(item["path"]).stat().st_size,
                }
                for item in files
            ],
        }
    else:
        forwarded = dict(values)
        forwarded.pop("provider", None)
        image_reference = forwarded.pop("image", None)
        if image_reference is None:
            request_json = json.dumps(forwarded, separators=(",", ":"))
            parameters = values
        else:
            try:
                (
                    content,
                    content_type,
                    filename,
                    remote_source,
                ) = await controller.media.resolve_input(image_reference)
            except (httpx.HTTPError, OSError, ValueError) as exc:
                return error(str(exc), 400, "invalid_image")
            directory = controller.media.uploads_path / public_id
            directory.mkdir(parents=True)
            path = directory / "0"
            path.write_bytes(content)
            files = [
                {
                    "content_type": content_type,
                    "field": "image",
                    "filename": filename,
                    "path": str(path),
                }
            ]
            fields = [(str(key), str(value)) for key, value in forwarded.items()]
            request_json = json.dumps(
                {"_control_multipart": {"fields": fields, "files": files}},
                separators=(",", ":"),
            )
            parameters = dict(forwarded) | {
                "input_media": [
                    {
                        "content_type": content_type,
                        "filename": filename,
                        "size": len(content),
                        "source_url": remote_source,
                    }
                ]
            }
    selected_provider = targets[0].provider if provider else None
    controller.store.save_job(
        public_id, model.id, request_json, provider=selected_provider
    )
    controller.store.save_history(
        public_id,
        "video_generation",
        str(model_id),
        json.dumps(history_parameters(parameters), separators=(",", ":")),
    )
    for item in files:
        controller.media.save_input(
            public_id,
            Path(item["path"]).read_bytes(),
            item["content_type"],
            item["filename"],
            item["field"],
            source_url=remote_source,
        )
    job = controller.store.job(public_id)
    assert job is not None
    controller.start_video(job)
    return JSONResponse(
        {
            "id": public_id,
            "object": "video",
            "model": model_id,
            "status": "queued",
            "created_at": job.created_at,
        }
    )


@router.get("/videos/{job_id}", operation_id="get_video", response_model=VideoJob)
async def get_video(job_id: str, request: Request) -> Response:
    return await video_response(job_id, request, content=False)


@router.get("/videos/{job_id}/content", operation_id="get_video_content")
async def video_content(job_id: str, request: Request) -> Response:
    return await video_response(job_id, request, content=True)


async def video_response(job_id: str, request: Request, *, content: bool) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not bearer_authorised(request, settings):
        return error("invalid API key", 401, "invalid_api_key")
    job = controller.store.job(job_id)
    if job is None:
        return error("video job was not found", 404, "not_found")
    if not content:
        if job.response_json:
            response_data = json.loads(job.response_json)
            if controller.store.media_for_history(job_id):
                response_data["output_url"] = str(
                    request.url_for("video_content", job_id=job_id)
                )
            return JSONResponse(response_data)
        return JSONResponse(
            {
                "id": job.id,
                "object": "video",
                "model": job.model,
                "status": job.status,
                "created_at": job.created_at,
                "error": job.error,
                "output_url": None,
            }
        )
    if job.status != "completed":
        return error("video is not ready", 409, "video_not_ready")
    response_data = json.loads(job.response_json or "{}")
    archived = controller.store.media_for_history(job_id)
    if archived:
        item = archived[0]
        return FileResponse(item.path, media_type=item.content_type)
    if output_url := response_data.get("output_url"):
        return Response(status_code=302, headers={"Location": output_url})
    return error("video output was not archived", 409, "video_output_missing")
