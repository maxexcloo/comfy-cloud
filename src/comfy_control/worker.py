from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qsl, urlencode

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from starlette.background import BackgroundTask

from .auth import request_authorised, websocket_authorised
from .catalogue import WorkflowModel
from .comfy import OutputRef
from .config import Settings
from .runtime import GenerationQueueFull, Runtime, VideoJob

HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
NATIVE_API_PREFIXES = (
    "prompt",
    "history",
    "view",
    "queue",
    "object_info",
    "system_stats",
    "upload",
    "interrupt",
    "free",
    "userdata",
    "api",
)


class ParameterError(ValueError):
    def __init__(self, parameter: str, message: str):
        super().__init__(message)
        self.parameter = parameter


def integer_parameter(
    value: Any,
    parameter: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ParameterError(parameter, f"{parameter} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ParameterError(parameter, f"{parameter} must be an integer") from exc
    if minimum is not None and parsed < minimum:
        raise ParameterError(parameter, f"{parameter} must be at least {minimum}")
    if maximum is not None and parsed > maximum:
        raise ParameterError(parameter, f"{parameter} must be at most {maximum}")
    return parsed


def dimensions_parameter(value: Any) -> tuple[int | None, int | None]:
    if value in (None, "", "auto"):
        return None, None
    try:
        width, height = (int(part) for part in value.lower().split("x", 1))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ParameterError("size", "size must be WIDTHxHEIGHT or auto") from exc
    if width <= 0 or height <= 0:
        raise ParameterError("size", "size dimensions must be positive")
    return width, height


def openai_error(
    message: str, status: int, code: str, param: str | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "param": param,
                "code": code,
            }
        },
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    runtime = Runtime(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        for task in runtime.background_tasks:
            task.cancel()
        if runtime.background_tasks:
            await asyncio.gather(*runtime.background_tasks, return_exceptions=True)
        await runtime.comfy.close()

    app = FastAPI(title="Comfy Control", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime

    def require(request: Request) -> JSONResponse | None:
        if not request_authorised(request, settings):
            return JSONResponse(
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
                content={
                    "error": {
                        "message": "invalid API key",
                        "type": "authentication_error",
                        "code": "invalid_api_key",
                    }
                },
            )
        return None

    async def limited_body(request: Request) -> bytes:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > settings.maximum_request_bytes:
                raise ParameterError("body", "Request body is too large")
        return bytes(body)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            content_length = 0
        if content_length > settings.maximum_request_bytes:
            response = openai_error(
                "Request body is too large", 413, "request_too_large"
            )
        else:
            response = await call_next(request)
        response.headers["x-request-id"] = request_id
        status = str(response.status_code)
        runtime.requests.inc()
        runtime.requests_by_status.labels(status).inc()
        return response

    @app.get("/health/live")
    async def live() -> JSONResponse:
        return JSONResponse({"status": "alive"})

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        ready = await runtime.comfy.ready()
        return JSONResponse(
            {"status": "ready" if ready else "starting"},
            status_code=200 if ready else 503,
        )

    @app.get("/health")
    async def health() -> JSONResponse:
        ready = await runtime.comfy.ready()
        object_info = await runtime.object_info()
        installed = runtime.available_models(object_info)
        return JSONResponse(
            {
                "status": "ready" if ready else "starting",
                "models": len(installed),
                "unavailable_models": len(runtime.catalogue.list()) - len(installed),
            },
            status_code=200 if ready else 503,
        )

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(
            generate_latest(runtime.metric_registry),
            media_type=CONTENT_TYPE_LATEST,
        )

    @app.get("/ping", include_in_schema=False)
    async def ping() -> Response:
        """RunPod load-balancing readiness contract: 204 loading, 200 ready."""
        if await runtime.comfy.ready():
            return Response(status_code=200)
        return Response(status_code=204)

    def model_object(model: WorkflowModel) -> dict[str, Any]:
        capabilities: dict[str, Any] = {
            "operation": model.operation,
            "input_modalities": ["text", "image"]
            if model.reference_image_count or "image" in model.input_map
            else ["text"],
            "output_modalities": [model.output.type],
            "parameters": sorted(model.input_map),
        }
        if model.operation == "image_edit":
            capabilities["reference_images"] = {
                "minimum": model.reference_image_count,
                "maximum": model.reference_image_count,
            }
        return {
            "id": model.id,
            "object": "model",
            "created": 0,
            "owned_by": model.owned_by,
            "capabilities": capabilities,
        }

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        if denied := require(request):
            return denied
        object_info = await runtime.object_info()
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    model_object(model)
                    for model in runtime.available_models(object_info)
                ],
            }
        )

    @app.get("/v1/models/{model_id:path}")
    async def retrieve_model(model_id: str, request: Request) -> Response:
        if denied := require(request):
            return denied
        try:
            model = await runtime.model(model_id)
        except KeyError:
            return openai_error(
                f"The model '{model_id}' does not exist",
                404,
                "model_not_found",
                "model",
            )
        return JSONResponse(model_object(model))

    @app.post("/v1/images/generations")
    async def image_generations(request: Request) -> Response:
        if denied := require(request):
            return denied
        try:
            body = json.loads(await limited_body(request))
            if not isinstance(body, dict):
                raise TypeError
            model = await runtime.model(body.get("model", ""))
        except ParameterError:
            return openai_error("Request body is too large", 413, "request_too_large")
        except KeyError:
            return openai_error(
                "Requested model was not found", 404, "model_not_found", "model"
            )
        except (TypeError, ValueError):
            return openai_error("Request body must be JSON", 400, "invalid_json")
        if model.operation != "image_generation":
            return openai_error(
                f"Model '{model.id}' does not support image generation",
                400,
                "unsupported_operation",
                "model",
            )
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            return openai_error(
                "prompt is required", 400, "missing_required_parameter", "prompt"
            )
        try:
            width, height = dimensions_parameter(body.get("size"))
            count = integer_parameter(
                body.get("n"), "n", default=1, minimum=1, maximum=4
            )
            seed = integer_parameter(body.get("seed"), "seed", minimum=0)
            steps = integer_parameter(body.get("steps"), "steps", minimum=1)
        except ParameterError as exc:
            return openai_error(str(exc), 400, "invalid_value", exc.parameter)
        response_format = body.get("response_format", "b64_json")
        if response_format not in ("b64_json", "url"):
            return openai_error(
                "response_format must be b64_json or url",
                400,
                "invalid_value",
                "response_format",
            )
        values = {
            "prompt": prompt,
            "negative_prompt": body.get("negative_prompt"),
            "width": width,
            "height": height,
            "seed": seed,
            "steps": steps,
        }
        try:
            await runtime.reserve_generation()
        except GenerationQueueFull as exc:
            return openai_error(str(exc), 429, "rate_limit_exceeded")
        try:
            started = time.monotonic()
            refs: list[OutputRef] = []
            for index in range(count):
                if seed is not None:
                    values["seed"] = seed + index
                refs.extend(await runtime.run(model, values))
            runtime.generations.inc()
            runtime.generation_duration.observe(time.monotonic() - started)
            return JSONResponse(
                {
                    "created": int(time.time()),
                    "data": await image_data(
                        refs[:count], response_format, request_base_url(request)
                    ),
                }
            )
        except (RuntimeError, TimeoutError, httpx.HTTPError) as exc:
            return openai_error(str(exc), 500, "generation_failed")
        finally:
            await runtime.release_generation()

    @app.post("/v1/images/edits")
    async def image_edits(request: Request) -> Response:
        if denied := require(request):
            return denied
        form = await request.form()
        try:
            model = await runtime.model(str(form.get("model", "")))
        except KeyError:
            return openai_error(
                "Requested model was not found", 404, "model_not_found", "model"
            )
        if model.operation != "image_edit":
            return openai_error(
                f"Model '{model.id}' does not support image editing",
                400,
                "unsupported_operation",
                "model",
            )
        images = list(form.getlist("image"))
        required_images = model.reference_image_count
        if len(images) != required_images:
            return openai_error(
                f"Model '{model.id}' requires exactly {required_images} reference "
                f"image{'s' if required_images != 1 else ''}; received {len(images)}",
                400,
                "invalid_value",
                "image",
            )
        if any(not hasattr(image, "read") for image in images):
            return openai_error(
                "image values must be uploaded files", 400, "invalid_value", "image"
            )
        if any(
            image.size is not None and image.size > settings.maximum_request_bytes
            for image in images
        ):
            return openai_error("image is too large", 413, "request_too_large", "image")
        prompt = str(form.get("prompt", ""))
        if not prompt:
            return openai_error(
                "prompt is required", 400, "missing_required_parameter", "prompt"
            )
        try:
            count = integer_parameter(
                form.get("n"), "n", default=1, minimum=1, maximum=4
            )
            seed = integer_parameter(form.get("seed"), "seed", minimum=0)
            steps = integer_parameter(form.get("steps"), "steps", minimum=1)
        except ParameterError as exc:
            return openai_error(str(exc), 400, "invalid_value", exc.parameter)
        response_format = str(form.get("response_format", "b64_json"))
        if response_format not in ("b64_json", "url"):
            return openai_error(
                "response_format must be b64_json or url",
                400,
                "invalid_value",
                "response_format",
            )
        size = str(form.get("size", "")).strip()
        if size and size != "auto":
            return openai_error(
                "size is not supported for image edits", 400, "invalid_value", "size"
            )
        try:
            uploaded = [
                await runtime.comfy.upload(
                    image.filename,
                    image.file,
                    image.content_type or "application/octet-stream",
                )
                for image in images
            ]
        except httpx.HTTPError as exc:
            return openai_error(str(exc), 502, "upload_failed", "image")
        values: dict[str, Any] = {
            "prompt": prompt,
            "seed": seed,
            "steps": steps,
        }
        values.update(zip(model.reference_input_names, uploaded, strict=True))
        try:
            await runtime.reserve_generation()
        except GenerationQueueFull as exc:
            return openai_error(str(exc), 429, "rate_limit_exceeded")
        try:
            started = time.monotonic()
            refs: list[OutputRef] = []
            for index in range(count):
                if seed is not None:
                    values["seed"] = seed + index
                refs.extend(await runtime.run(model, values))
            runtime.generations.inc()
            runtime.generation_duration.observe(time.monotonic() - started)
            return JSONResponse(
                {
                    "created": int(time.time()),
                    "data": await image_data(
                        refs[:count], response_format, request_base_url(request)
                    ),
                }
            )
        except (RuntimeError, TimeoutError, httpx.HTTPError) as exc:
            return openai_error(str(exc), 500, "generation_failed")
        finally:
            await runtime.release_generation()

    async def image_data(
        refs: list[OutputRef], response_format: str, base_url: str
    ) -> list[dict[str, Any]]:
        data = []
        for ref in refs:
            if response_format == "url" and runtime.storage is not None:
                output = await runtime.comfy.fetch_output(ref)
                url = await runtime.storage.upload(
                    ref.filename,
                    output.content,
                    output.headers.get("content-type", ref.media_type),
                    ref.subfolder,
                )
                if url:
                    data.append({"url": url})
                    continue
                data.append({"b64_json": base64.b64encode(output.content).decode()})
            elif response_format == "url":
                query = urlencode(
                    {
                        "filename": ref.filename,
                        "subfolder": ref.subfolder,
                        "type": ref.type,
                    }
                )
                data.append({"url": f"{base_url}/view?{query}"})
            else:
                output = await runtime.comfy.fetch_output(ref)
                data.append({"b64_json": base64.b64encode(output.content).decode()})
        return data

    def request_base_url(request: Request) -> str:
        return settings.public_base_url or str(request.base_url).rstrip("/")

    async def execute_video(
        job: VideoJob, model: WorkflowModel, values: dict[str, Any]
    ) -> None:
        try:
            job.status = "in_progress"
            job.lease_expires_at = int(
                time.time() + runtime.settings.workflow_timeout + 60
            )
            runtime.store_job(job)
            job.output = (await runtime.run(model, values))[0]
            await runtime.upload_output(job)
            job.status = "completed"
            job.lease_expires_at = None
            runtime.store_job(job)
            runtime.video_jobs.labels("completed").inc()
        except Exception as exc:  # noqa: BLE001 - task boundary exposes errors through job status
            job.status = "failed"
            job.error = str(exc)
            job.lease_expires_at = None
            runtime.store_job(job)
            runtime.video_jobs.labels("failed").inc()
        finally:
            await runtime.release_generation()

    def video_response(job: VideoJob) -> JSONResponse:
        return JSONResponse(
            {
                "id": job.id,
                "object": "video",
                "model": job.model,
                "status": job.status,
                "created_at": job.created_at,
                "error": job.error,
                "output_url": runtime.job_output_url(job),
            }
        )

    async def wait_for_video(job: VideoJob) -> VideoJob:
        while job.status in {"queued", "in_progress"}:
            await asyncio.sleep(1)
            job = runtime.get_job(job.id) or job
        return job

    @app.post("/v1/videos")
    async def create_video(request: Request) -> Response:
        if denied := require(request):
            return denied
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            body: dict[str, Any] = {
                key: str(value)
                for key, value in form.items()
                if not hasattr(value, "read")
            }
            image = form.get("image")
            if image is not None and hasattr(image, "read"):
                if (
                    image.size is not None
                    and image.size > settings.maximum_request_bytes
                ):
                    return openai_error(
                        "image is too large", 413, "request_too_large", "image"
                    )
                try:
                    body["image"] = await runtime.comfy.upload(
                        image.filename,
                        image.file,
                        image.content_type or "application/octet-stream",
                    )
                except httpx.HTTPError as exc:
                    return openai_error(str(exc), 502, "upload_failed", "image")
            for field in ("width", "height", "length", "seed", "steps"):
                if field in body:
                    try:
                        body[field] = int(body[field])
                    except (TypeError, ValueError):
                        return openai_error(
                            f"{field} must be an integer", 400, "invalid_value", field
                        )
        else:
            try:
                body = json.loads(await limited_body(request))
                if not isinstance(body, dict):
                    raise TypeError
            except ParameterError:
                return openai_error(
                    "Request body is too large", 413, "request_too_large"
                )
            except (TypeError, ValueError):
                return openai_error(
                    "Request body must be JSON or multipart form", 400, "invalid_json"
                )
        try:
            model = await runtime.model(body.get("model", ""))
        except KeyError:
            return openai_error(
                "Requested model was not found", 404, "model_not_found", "model"
            )
        if model.operation != "video_generation":
            return openai_error(
                f"Model '{model.id}' does not support video generation",
                400,
                "unsupported_operation",
                "model",
            )
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            return openai_error(
                "prompt is required", 400, "missing_required_parameter", "prompt"
            )
        if "image" in model.input_map and not body.get("image"):
            return openai_error(
                "image is required for this video model",
                400,
                "missing_required_parameter",
                "image",
            )
        values = dict(body)
        try:
            width, height = dimensions_parameter(body.get("size"))
            if width is not None:
                values["width"], values["height"] = width, height
            for parameter in ("length", "seed", "steps"):
                if parameter in body:
                    values[parameter] = integer_parameter(
                        body[parameter],
                        parameter,
                        minimum=0 if parameter == "seed" else 1,
                    )
        except ParameterError as exc:
            return openai_error(str(exc), 400, "invalid_value", exc.parameter)
        if body.get("seconds") is not None:
            try:
                seconds = float(body["seconds"])
            except (TypeError, ValueError):
                return openai_error(
                    "seconds must be a number", 400, "invalid_value", "seconds"
                )
            if seconds <= 0 or seconds > 15:
                return openai_error(
                    "seconds must be between 0 and 15", 400, "invalid_value", "seconds"
                )
            frames = max(5, round(seconds * 24))
            values["length"] = frames + (5 - frames % 17) % 17
        requested_id = request.headers.get("x-comfy-job-id", "")
        if requested_id and (
            len(requested_id) > 128
            or any(
                character not in "-0123456789_abcdefghijklmnopqrstuvwxyz"
                for character in requested_id
            )
        ):
            return openai_error("Invalid video job ID", 400, "invalid_job_id")
        if requested_id and (existing := runtime.get_job(requested_id)) is not None:
            if request.query_params.get("wait", "").lower() in {"1", "true", "yes"}:
                existing = await wait_for_video(existing)
            return video_response(existing)
        try:
            await runtime.reserve_generation()
        except GenerationQueueFull as exc:
            return openai_error(str(exc), 429, "rate_limit_exceeded")
        job = VideoJob(
            id=requested_id or f"video_{uuid.uuid4().hex}",
            model=model.id,
            lease_expires_at=int(
                time.time()
                + settings.workflow_timeout * settings.maximum_pending_generations
                + 60
            ),
        )
        runtime.jobs[job.id] = job
        runtime.store_job(job)
        runtime.video_jobs_created.inc()
        task = runtime.start_background_task(execute_video(job, model, values))
        if request.query_params.get("wait", "").lower() in {"1", "true", "yes"}:
            await asyncio.shield(task)
        return video_response(job)

    @app.get("/v1/videos/{job_id}")
    async def get_video(job_id: str, request: Request) -> Response:
        if denied := require(request):
            return denied
        job = runtime.get_job(job_id)
        if not job:
            return openai_error("Video job was not found", 404, "not_found")
        return JSONResponse(
            {
                "id": job.id,
                "object": "video",
                "model": job.model,
                "status": job.status,
                "created_at": job.created_at,
                "error": job.error,
                "output_url": runtime.job_output_url(job),
            }
        )

    @app.get("/v1/videos/{job_id}/content")
    async def video_content(job_id: str, request: Request) -> Response:
        if denied := require(request):
            return denied
        job = runtime.get_job(job_id)
        if not job or not job.output:
            return openai_error("Video is not ready", 409, "video_not_ready")
        if output_url := runtime.job_output_url(job):
            return Response(status_code=302, headers={"Location": output_url})
        return StreamingResponse(
            runtime.comfy.stream_output(job.output),
            media_type=job.output.media_type,
        )

    @app.websocket("/ws")
    async def websocket_proxy(websocket: WebSocket) -> None:
        if not websocket_authorised(websocket, settings):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        target = (
            settings.comfy_url.replace("http://", "ws://").replace("https://", "wss://")
            + "/ws"
        )
        query = [
            (key, value)
            for key, value in parse_qsl(websocket.url.query, keep_blank_values=True)
            if key != "token"
        ]
        if query:
            target += "?" + urlencode(query)
        try:
            async with websockets.connect(target) as upstream:

                async def client_to_upstream() -> None:
                    while True:
                        message = await websocket.receive()
                        if message.get("text") is not None:
                            await upstream.send(message["text"])
                        elif message.get("bytes") is not None:
                            await upstream.send(message["bytes"])

                async def upstream_to_client() -> None:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                await asyncio.gather(client_to_upstream(), upstream_to_client())
        except Exception:  # noqa: BLE001 - websocket proxy boundary
            await websocket.close(code=1011)

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def comfy_proxy(path: str, request: Request) -> Response:
        is_native = any(
            path == prefix or path.startswith(prefix + "/")
            for prefix in NATIVE_API_PREFIXES
        )
        if not settings.ui_enabled and not is_native:
            return JSONResponse(
                {"detail": "ComfyUI frontend is disabled in serverless mode"},
                status_code=404,
            )
        if not request_authorised(request, settings, allow_basic=settings.ui_enabled):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="ComfyUI", Bearer'},
            )
        try:
            body = await limited_body(request)
        except ParameterError:
            return openai_error("Request body is too large", 413, "request_too_large")
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower()
            not in HOP_HEADERS | {"host", "authorization", "content-length"}
        }
        upstream_request = runtime.comfy.http.build_request(
            request.method,
            "/" + path,
            params=request.query_params,
            headers=headers,
            content=body,
        )
        try:
            upstream = await runtime.comfy.http.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"detail": f"ComfyUI request failed: {exc}"}, status_code=502
            )
        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in HOP_HEADERS
        }
        body_iterator = (
            iter([upstream.content])
            if upstream.is_stream_consumed
            else upstream.aiter_raw()
        )
        return StreamingResponse(
            body_iterator,
            status_code=upstream.status_code,
            headers=response_headers,
            background=BackgroundTask(upstream.aclose),
        )

    return app
