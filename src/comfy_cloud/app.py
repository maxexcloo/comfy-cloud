from __future__ import annotations

import asyncio
import base64
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from .auth import request_authorized, websocket_authorized
from .catalog import Catalog, WorkflowModel
from .comfy import ComfyClient, OutputRef
from .config import Settings

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


@dataclass
class VideoJob:
    id: str
    model: str
    status: str = "queued"
    created_at: int = field(default_factory=lambda: int(time.time()))
    error: str | None = None
    output: OutputRef | None = None


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.catalog = Catalog.load(settings.catalog_dirs)
        self.comfy = ComfyClient(settings.comfy_url, settings.request_timeout)
        self.jobs: dict[str, VideoJob] = {}
        self.inference_lock = asyncio.Lock()

    async def run(
        self, model: WorkflowModel, values: dict[str, Any]
    ) -> list[OutputRef]:
        graph = model.render(values)
        async with self.inference_lock:
            prompt_id = await self.comfy.submit(graph)
            return await self.comfy.wait(
                prompt_id, model.output.node, self.settings.workflow_timeout
            )

    def model(self, model_id: str) -> WorkflowModel:
        return self.catalog.get_available(model_id, self.settings.models_dir)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    runtime = Runtime(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await runtime.comfy.close()

    app = FastAPI(title="Comfy Cloud", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime

    def require(request: Request) -> JSONResponse | None:
        if not request_authorized(request, settings):
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

    @app.get("/health")
    async def health() -> JSONResponse:
        ready = await runtime.comfy.ready()
        installed = runtime.catalog.list_available(settings.models_dir)
        return JSONResponse(
            {
                "status": "ready" if ready else "starting",
                "models": len(installed),
                "unavailable_models": len(runtime.catalog.list()) - len(installed),
            },
            status_code=200 if ready else 503,
        )

    @app.get("/ping", include_in_schema=False)
    async def ping() -> Response:
        """RunPod load-balancing readiness contract: 204 loading, 200 ready."""
        if await runtime.comfy.ready():
            return Response(status_code=200)
        return Response(status_code=204)

    def model_object(model: WorkflowModel) -> dict[str, Any]:
        return {
            "id": model.id,
            "object": "model",
            "created": 0,
            "owned_by": model.owned_by,
        }

    @app.get("/v1/models")
    async def models(request: Request) -> Response:
        if denied := require(request):
            return denied
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    model_object(model)
                    for model in runtime.catalog.list_available(settings.models_dir)
                ],
            }
        )

    @app.get("/v1/models/{model_id:path}")
    async def retrieve_model(model_id: str, request: Request) -> Response:
        if denied := require(request):
            return denied
        try:
            model = runtime.model(model_id)
        except KeyError:
            return openai_error(
                f"The model '{model_id}' does not exist",
                404,
                "model_not_found",
                "model",
            )
        value = model_object(model)
        value["capabilities"] = {
            "operation": model.operation,
            "input_modalities": ["text", "image"]
            if "image" in model.input_map
            else ["text"],
            "output_modalities": [model.output.type],
            "parameters": sorted(model.input_map),
        }
        return JSONResponse(value)

    @app.post("/v1/images/generations")
    async def image_generations(request: Request) -> Response:
        if denied := require(request):
            return denied
        try:
            body = await request.json()
            model = runtime.model(body.get("model", ""))
        except KeyError:
            return openai_error(
                "Requested model was not found", 404, "model_not_found", "model"
            )
        except ValueError:
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
        size = body.get("size")
        width = height = None
        if size and size != "auto":
            try:
                width, height = (int(part) for part in size.lower().split("x", 1))
            except (ValueError, AttributeError):
                return openai_error(
                    "size must be WIDTHxHEIGHT or auto", 400, "invalid_value", "size"
                )
        values = {
            "prompt": prompt,
            "negative_prompt": body.get("negative_prompt"),
            "width": width,
            "height": height,
            "seed": body.get("seed"),
            "steps": body.get("steps"),
        }
        count = int(body.get("n", 1))
        if count < 1 or count > 4:
            return openai_error("n must be between 1 and 4", 400, "invalid_value", "n")
        response_format = body.get("response_format", "b64_json")
        try:
            refs: list[OutputRef] = []
            for index in range(count):
                if values["seed"] is not None:
                    values["seed"] = int(values["seed"]) + index
                refs.extend(await runtime.run(model, values))
            data = []
            for ref in refs[:count]:
                if response_format == "url":
                    query = urlencode(
                        {
                            "filename": ref.filename,
                            "subfolder": ref.subfolder,
                            "type": ref.type,
                        }
                    )
                    data.append({"url": f"{settings.public_base_url}/view?{query}"})
                else:
                    output = await runtime.comfy.fetch_output(ref)
                    data.append({"b64_json": base64.b64encode(output.content).decode()})
            return JSONResponse({"created": int(time.time()), "data": data})
        except (RuntimeError, TimeoutError, httpx.HTTPError) as exc:
            return openai_error(str(exc), 500, "generation_failed")

    @app.post("/v1/images/edits")
    async def image_edits(request: Request) -> Response:
        if denied := require(request):
            return denied
        form = await request.form()
        try:
            model = runtime.model(str(form.get("model", "")))
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
        image = form.get("image")
        if image is None or not hasattr(image, "read"):
            return openai_error(
                "image is required", 400, "missing_required_parameter", "image"
            )
        prompt = str(form.get("prompt", ""))
        if not prompt:
            return openai_error(
                "prompt is required", 400, "missing_required_parameter", "prompt"
            )
        count = int(form.get("n", 1))
        if count < 1 or count > 4:
            return openai_error("n must be between 1 and 4", 400, "invalid_value", "n")
        try:
            seed = int(form["seed"]) if form.get("seed") is not None else None
        except (TypeError, ValueError):
            return openai_error("seed must be an integer", 400, "invalid_value", "seed")
        try:
            steps = int(form["steps"]) if form.get("steps") is not None else None
        except (TypeError, ValueError):
            return openai_error(
                "steps must be an integer", 400, "invalid_value", "steps"
            )
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
        uploaded = await runtime.comfy.upload(
            image.filename,
            await image.read(),
            image.content_type or "application/octet-stream",
        )
        values: dict[str, Any] = {
            "prompt": prompt,
            "image": uploaded,
            "seed": seed,
            "steps": steps,
        }
        try:
            refs: list[OutputRef] = []
            for index in range(count):
                if seed is not None:
                    values["seed"] = seed + index
                refs.extend(await runtime.run(model, values))
            data = []
            for ref in refs[:count]:
                if response_format == "url":
                    query = urlencode(
                        {
                            "filename": ref.filename,
                            "subfolder": ref.subfolder,
                            "type": ref.type,
                        }
                    )
                    data.append({"url": f"{settings.public_base_url}/view?{query}"})
                else:
                    output = await runtime.comfy.fetch_output(ref)
                    data.append({"b64_json": base64.b64encode(output.content).decode()})
            return JSONResponse({"created": int(time.time()), "data": data})
        except (RuntimeError, TimeoutError, httpx.HTTPError) as exc:
            return openai_error(str(exc), 500, "generation_failed")

    async def execute_video(
        job: VideoJob, model: WorkflowModel, values: dict[str, Any]
    ) -> None:
        try:
            job.status = "in_progress"
            job.output = (await runtime.run(model, values))[0]
            job.status = "completed"
        except Exception as exc:  # noqa: BLE001 - task boundary exposes errors through job status
            job.status = "failed"
            job.error = str(exc)

    @app.post("/v1/videos")
    async def create_video(request: Request) -> Response:
        if denied := require(request):
            return denied
        body = await request.json()
        try:
            model = runtime.model(body.get("model", ""))
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
        values = dict(body)
        size = body.get("size")
        if size and size != "auto":
            try:
                values["width"], values["height"] = (
                    int(part) for part in size.lower().split("x", 1)
                )
            except (AttributeError, ValueError):
                return openai_error(
                    "size must be WIDTHxHEIGHT or auto", 400, "invalid_value", "size"
                )
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
        job = VideoJob(id=f"video_{uuid.uuid4().hex}", model=model.id)
        runtime.jobs[job.id] = job
        asyncio.create_task(execute_video(job, model, values))
        return JSONResponse(
            {
                "id": job.id,
                "object": "video",
                "model": job.model,
                "status": job.status,
                "created_at": job.created_at,
            }
        )

    @app.get("/v1/videos/{job_id}")
    async def get_video(job_id: str, request: Request) -> Response:
        if denied := require(request):
            return denied
        job = runtime.jobs.get(job_id)
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
            }
        )

    @app.get("/v1/videos/{job_id}/content")
    async def video_content(job_id: str, request: Request) -> Response:
        if denied := require(request):
            return denied
        job = runtime.jobs.get(job_id)
        if not job or not job.output:
            return openai_error("Video is not ready", 409, "video_not_ready")
        output = await runtime.comfy.fetch_output(job.output)
        return Response(
            output.content,
            media_type=output.headers.get("content-type", job.output.media_type),
        )

    @app.websocket("/ws")
    async def websocket_proxy(websocket: WebSocket) -> None:
        if not websocket_authorized(websocket, settings):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        target = (
            settings.comfy_url.replace("http://", "ws://").replace("https://", "wss://")
            + "/ws"
        )
        if websocket.url.query:
            target += "?" + websocket.url.query
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
        if not request_authorized(request, settings, allow_basic=settings.ui_enabled):
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="ComfyUI", Bearer'},
            )
        body = await request.body()
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower()
            not in HOP_HEADERS | {"host", "authorization", "content-length"}
        }
        upstream = await runtime.comfy.http.request(
            request.method,
            "/" + path,
            params=request.query_params,
            headers=headers,
            content=body,
        )
        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in HOP_HEADERS | {"content-length", "content-encoding"}
        }
        return Response(
            upstream.content, status_code=upstream.status_code, headers=response_headers
        )

    return app


app = create_app()
