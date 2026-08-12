from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .control_contracts import (
    ImageGenerationRequest,
    ImageGenerationResponse,
    VideoCreateRequest,
    VideoJob,
)
from .control_dashboard import bearer_authorised
from .control_http import RequestBodyTooLarge, error, limited_body
from .control_inference import (
    archived_image_content,
    canonical_parameters,
    internal_image_content,
    normalise_grok_image_options,
)
from .controller import exception_message, history_parameters, rewrite_json_model

router = APIRouter(prefix="/v1", tags=["inference"])


async def generate_image(request: Request, operation: str, path: str) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not bearer_authorised(request, settings):
        return error("invalid API key", 401, "invalid_api_key")
    try:
        body = await limited_body(
            request, controller.preferences.control_maximum_request_bytes
        )
    except RequestBodyTooLarge:
        return error("request body is too large", 413, "request_too_large")
    try:
        original = json.loads(body)
        if not isinstance(original, dict):
            raise TypeError("request body must be an object")
        original = normalise_grok_image_options(original)
        requested_model = str(original.get("model", ""))
        provider = str(original.get("provider", "")).strip() or None
        model, targets = controller.resolve_model(requested_model, operation, provider)
        canonical_parameters(original)
        count = int(original.get("n", 1))
        if count < 1 or count > 4:
            raise ValueError("n must be between 1 and 4")
        if original.get("response_format", "b64_json") not in {"b64_json", "url"}:
            raise ValueError("response_format must be b64_json or url")
    except (AttributeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return error(str(exc), 400, "invalid_request")
    except KeyError as exc:
        return error(str(exc), 404, "model_not_found")
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    history_id = f"image_{uuid.uuid4().hex}"
    controller.store.save_history(
        history_id,
        operation,
        model.id,
        json.dumps(history_parameters(original), separators=(",", ":")),
    )
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"authorization", "content-length", "host"}
    }
    failures = []
    forwarded = dict(original)
    forwarded.pop("provider", None)
    forwarded_body = json.dumps(forwarded, separators=(",", ":")).encode()
    for target in targets:
        controller.store.update_history(
            history_id, "in_progress", provider=target.provider
        )
        attempt_id = controller.store.start_attempt(history_id, target.provider)
        try:
            is_proxy = controller.providers[target.provider].config.type == "proxy"
            if not is_proxy:
                parameters = canonical_parameters(forwarded)
                count = int(forwarded.get("n", 1))
                outputs = []
                for index in range(count):
                    indexed = dict(parameters)
                    if indexed.get("seed") is not None:
                        indexed["seed"] = int(indexed["seed"]) + index
                    result = await controller.execute_internal(
                        target, f"{history_id}_{index}", operation, indexed
                    )
                    outputs.extend(result)
                response = None
            else:
                outputs = None
                response = await controller.forward(
                    target,
                    request.method,
                    path,
                    rewrite_json_model(forwarded_body, target.model),
                    headers,
                    request_id,
                )
        except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
            message = exception_message(exc)
            controller.store.finish_attempt(attempt_id, "failed", message)
            failures.append(f"{target.provider}: {message}")
            controller.store.event(
                "error", message, provider=target.provider, request_id=request_id
            )
            continue
        if response is None or response.is_success:
            controller.store.finish_attempt(attempt_id, "completed")
            if response is not None:
                await controller.archive_images(history_id, target.provider, response)
            controller.store.update_history(
                history_id, "completed", provider=target.provider
            )
            controller.store.event(
                "info",
                f"{operation} completed",
                provider=target.provider,
                request_id=request_id,
            )
            content = (
                archived_image_content(request, controller, history_id, response)
                if response is not None
                else internal_image_content(
                    request,
                    controller,
                    history_id,
                    outputs,
                    str(forwarded.get("response_format", "b64_json")),
                )
            )
            return Response(
                content=content,
                status_code=response.status_code if response is not None else 200,
                media_type=(
                    response.headers.get("content-type")
                    if response is not None
                    else "application/json"
                ),
                headers={
                    "x-comfy-provider": target.provider,
                    "x-comfy-history-id": history_id,
                    "x-request-id": request_id,
                },
            )
        controller.store.finish_attempt(
            attempt_id, "failed", f"HTTP {response.status_code}"
        )
        failures.append(f"{target.provider}: HTTP {response.status_code}")
        if response.status_code < 500 and response.status_code != 429:
            controller.store.update_history(
                history_id,
                "failed",
                provider=target.provider,
                error=f"HTTP {response.status_code}",
            )
            return Response(
                content=response.content,
                status_code=response.status_code,
                media_type=response.headers.get("content-type"),
            )
    failure = "; ".join(failures) or "all providers failed"
    controller.store.update_history(history_id, "failed", error=failure)
    return error(failure, 502, "providers_failed")


@router.post(
    "/images/generations",
    operation_id="create_image",
    response_model=ImageGenerationResponse,
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": ImageGenerationRequest.model_json_schema()
                }
            },
            "required": True,
        }
    },
)
async def image_generations(request: Request) -> Response:
    return await generate_image(request, "image_generation", "/v1/images/generations")


@router.post(
    "/images/edits",
    operation_id="edit_image",
    response_model=ImageGenerationResponse,
    openapi_extra={
        "requestBody": {
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "additionalProperties": True,
                        "properties": {
                            "image": {
                                "format": "binary",
                                "items": {"format": "binary", "type": "string"},
                                "type": "array",
                            },
                            "model": {"type": "string"},
                            "prompt": {"type": "string"},
                            "provider": {"type": "string"},
                        },
                        "required": ["image", "model", "prompt"],
                        "type": "object",
                    }
                }
            },
            "required": True,
        }
    },
)
async def image_edits(request: Request) -> Response:
    controller = request.app.state.controller
    settings = request.app.state.settings
    if not bearer_authorised(request, settings):
        return error("invalid API key", 401, "invalid_api_key")
    try:
        await limited_body(
            request, controller.preferences.control_maximum_request_bytes
        )
    except RequestBodyTooLarge:
        return error("request body is too large", 413, "request_too_large")
    try:
        form = await request.form()
        model_id = str(form.get("model", ""))
        provider = str(form.get("provider", "")).strip() or None
        model, targets = controller.resolve_model(model_id, "image_edit", provider)
    except ValueError as exc:
        return error(str(exc), 400, "invalid_request")
    except KeyError as exc:
        return error(str(exc), 404, "model_not_found")
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    fields: list[tuple[str, str]] = []
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    for key, value in form.multi_items():
        if hasattr(value, "read"):
            files.append(
                (
                    key,
                    (
                        value.filename or "upload",
                        await value.read(),
                        value.content_type or "application/octet-stream",
                    ),
                )
            )
        elif key not in {"model", "provider"}:
            fields.append((key, str(value)))
    normalised_fields = normalise_grok_image_options(
        {"model": model_id, **dict(fields)}
    )
    fields = [
        (key, str(value))
        for key, value in normalised_fields.items()
        if key not in {"model", "provider"}
    ]
    field_values = dict(fields)
    try:
        count = int(field_values.get("n", 1))
        if count < 1 or count > 4:
            raise ValueError("n must be between 1 and 4")
        if field_values.get("response_format", "b64_json") not in {
            "b64_json",
            "url",
        }:
            raise ValueError("response_format must be b64_json or url")
        if field_values.get("size", "") not in {"", "auto"}:
            raise ValueError("size is not supported for image edits")
        canonical_parameters(field_values)
    except (TypeError, ValueError) as exc:
        return error(str(exc), 400, "invalid_request")
    history_id = f"edit_{uuid.uuid4().hex}"
    parameters = dict(fields)
    parameters["model"] = model_id
    if provider:
        parameters["provider"] = provider
    parameters["input_media"] = [
        {
            "content_type": content_type,
            "filename": filename,
            "size": len(content),
        }
        for _, (filename, content, content_type) in files
    ]
    controller.store.save_history(
        history_id,
        "image_edit",
        model.id,
        json.dumps(history_parameters(parameters), separators=(",", ":")),
    )
    for field_name, (filename, content, content_type) in files:
        controller.save_input_media(
            history_id, content, content_type, filename, field_name
        )
    failures: list[str] = []
    for target in targets:
        controller.store.update_history(
            history_id, "in_progress", provider=target.provider
        )
        attempt_id = controller.store.start_attempt(history_id, target.provider)
        encoded = httpx.Request(
            "POST",
            "http://multipart.invalid",
            data=dict(fields) | {"model": target.model},
            files=files,
        )
        try:
            is_proxy = controller.providers[target.provider].config.type == "proxy"
            if not is_proxy:
                parameters = canonical_parameters(field_values)
                outputs = []
                for index in range(count):
                    indexed = dict(parameters)
                    if indexed.get("seed") is not None:
                        indexed["seed"] = int(indexed["seed"]) + index
                    result = await controller.execute_internal(
                        target,
                        f"{history_id}_{index}",
                        "image_edit",
                        indexed,
                        files,
                    )
                    outputs.extend(result)
                response = None
            else:
                outputs = None
                response = await controller.forward(
                    target,
                    "POST",
                    "/v1/images/edits",
                    encoded.read(),
                    {"content-type": encoded.headers["content-type"]},
                    request_id,
                )
        except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
            message = exception_message(exc)
            controller.store.finish_attempt(attempt_id, "failed", message)
            failures.append(f"{target.provider}: {message}")
            continue
        if response is None or response.is_success:
            controller.store.finish_attempt(attempt_id, "completed")
            if response is not None:
                await controller.archive_images(history_id, target.provider, response)
            controller.store.update_history(
                history_id, "completed", provider=target.provider
            )
            content = (
                archived_image_content(request, controller, history_id, response)
                if response is not None
                else internal_image_content(
                    request,
                    controller,
                    history_id,
                    outputs,
                    field_values.get("response_format", "b64_json"),
                )
            )
            return Response(
                content=content,
                status_code=response.status_code if response is not None else 200,
                media_type=(
                    response.headers.get("content-type")
                    if response is not None
                    else "application/json"
                ),
                headers={
                    "x-comfy-provider": target.provider,
                    "x-comfy-history-id": history_id,
                    "x-request-id": request_id,
                },
            )
        controller.store.finish_attempt(
            attempt_id, "failed", f"HTTP {response.status_code}"
        )
        failures.append(f"{target.provider}: HTTP {response.status_code}")
        if response.status_code < 500 and response.status_code != 429:
            controller.store.update_history(
                history_id,
                "failed",
                provider=target.provider,
                error=f"HTTP {response.status_code}",
            )
            return Response(
                content=response.content,
                status_code=response.status_code,
                media_type=response.headers.get("content-type"),
            )
    failure = "; ".join(failures) or "all providers failed"
    controller.store.update_history(history_id, "failed", error=failure)
    return error(failure, 502, "providers_failed")


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
        body = await limited_body(
            request, controller.preferences.control_maximum_request_bytes
        )
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
        directory = controller.uploads_path / public_id
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
                ) = await controller.remote_input(image_reference)
            except (httpx.HTTPError, OSError, ValueError) as exc:
                return error(str(exc), 400, "invalid_image")
            directory = controller.uploads_path / public_id
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
        controller.save_input_media(
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
