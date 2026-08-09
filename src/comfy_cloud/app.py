from __future__ import annotations

import asyncio
import base64
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .auth import request_authorized, websocket_authorized
from .catalogue import Catalogue, WorkflowModel
from .comfy import ComfyClient, OutputRef
from .config import Settings
from .jobs import JobStore
from .storage import ObjectStorage

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


@dataclass
class VideoJob:
    id: str
    model: str
    status: str = "queued"
    created_at: int = field(default_factory=lambda: int(time.time()))
    error: str | None = None
    output: OutputRef | None = None
    output_key: str | None = None
    output_url: str | None = None

    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "status": self.status,
            "created_at": self.created_at,
            "error": self.error,
            "output": {
                "filename": self.output.filename,
                "subfolder": self.output.subfolder,
                "type": self.output.type,
                "media_type": self.output.media_type,
            }
            if self.output is not None
            else None,
            "output_key": self.output_key,
            "output_url": self.output_url,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> VideoJob:
        output_data = record.get("output")
        output = (
            OutputRef(
                filename=output_data["filename"],
                subfolder=output_data.get("subfolder", ""),
                type=output_data.get("type", "output"),
                media_type=output_data.get("media_type", "video/mp4"),
            )
            if output_data
            else None
        )
        return cls(
            id=record["id"],
            model=record["model"],
            status=record.get("status", "queued"),
            created_at=record.get("created_at", 0),
            error=record.get("error"),
            output=output,
            output_key=record.get("output_key"),
            output_url=record.get("output_url"),
        )


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.catalogue = Catalogue.load(settings.catalogue_dirs)
        self.comfy = ComfyClient(settings.comfy_url, settings.request_timeout)
        self.storage = ObjectStorage.from_env(settings.storage_env)
        self.jobs: dict[str, VideoJob] = {}
        self.job_store = JobStore(settings.jobs_dir)
        self.inference_lock = asyncio.Lock()
        self.background_tasks: set[asyncio.Task[None]] = set()
        self.metrics: dict[str, int | float] = {
            "requests_total": 0,
            "requests_by_status": {},
            "generations_total": 0,
            "generation_seconds_total": 0.0,
            "video_jobs_created": 0,
            "video_jobs_completed": 0,
            "video_jobs_failed": 0,
        }
        for record in self.job_store.load():
            try:
                job = VideoJob.from_record(record)
            except (KeyError, TypeError, ValueError):
                continue
            if job.status in {"queued", "in_progress"}:
                job.status = "failed"
                job.error = "worker restarted before the job completed"
                self.job_store.save(job.record())
            self.jobs[job.id] = job

    async def run(
        self, model: WorkflowModel, values: dict[str, Any]
    ) -> list[OutputRef]:
        graph = model.render(values)
        async with self.inference_lock:
            prompt_id = await self.comfy.submit(graph)
            return await self.comfy.wait(
                prompt_id, model.output.node, self.settings.workflow_timeout
            )

    def store_job(self, job: VideoJob) -> None:
        self.job_store.save(job.record())

    def start_background_task(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    def job_output_url(self, job: VideoJob) -> str | None:
        if self.storage is not None and job.output_key:
            return self.storage.url(job.output_key)
        return job.output_url

    async def upload_output(self, job: VideoJob) -> None:
        if self.storage is None or job.output is None:
            return
        with tempfile.NamedTemporaryFile(prefix="comfy-cloud-", delete=False) as file:
            temporary = Path(file.name)
        try:
            content_type = await self.comfy.save_output(job.output, temporary)
            output_url = await self.storage.upload_path(
                job.output.filename,
                temporary,
                content_type,
                job.output.subfolder,
            )
            if output_url:
                job.output_key = self.storage.key(
                    job.output.filename, job.output.subfolder
                )
        finally:
            temporary.unlink(missing_ok=True)

    async def object_info(self) -> dict[str, Any] | None:
        try:
            return await self.comfy.object_info()
        except (httpx.HTTPError, RuntimeError):
            return None

    async def model(self, model_id: str) -> WorkflowModel:
        object_info = await self.object_info()
        if object_info is None:
            raise KeyError("ComfyUI node information is unavailable")
        return self.catalogue.get_available(
            model_id, self.settings.models_dir, object_info
        )

    def available_models(
        self, object_info: dict[str, Any] | None
    ) -> list[WorkflowModel]:
        if object_info is None:
            return []
        return self.catalogue.list_available(self.settings.models_dir, object_info)


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

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        status = str(response.status_code)
        runtime.metrics["requests_total"] = int(runtime.metrics["requests_total"]) + 1
        by_status: dict = runtime.metrics["requests_by_status"]
        by_status[status] = int(by_status.get(status, 0)) + 1
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
        m = runtime.metrics
        lines = [
            "# HELP comfy_cloud_requests_total Total HTTP requests served.",
            "# TYPE comfy_cloud_requests_total counter",
            f"comfy_cloud_requests_total {m['requests_total']}",
            "# HELP comfy_cloud_requests_by_status HTTP requests by response status.",
            "# TYPE comfy_cloud_requests_by_status counter",
        ]
        for status, count in sorted(m["requests_by_status"].items()):
            lines.append(f'comfy_cloud_requests_by_status{{status="{status}"}} {count}')
        lines += [
            "# HELP comfy_cloud_generations_total Completed image generations.",
            "# TYPE comfy_cloud_generations_total counter",
            f"comfy_cloud_generations_total {m['generations_total']}",
            "# HELP comfy_cloud_generation_seconds_total Cumulative generation time.",
            "# TYPE comfy_cloud_generation_seconds_total counter",
            f"comfy_cloud_generation_seconds_total {m['generation_seconds_total']:.3f}",
            "# HELP comfy_cloud_video_jobs Video jobs by terminal state.",
            "# TYPE comfy_cloud_video_jobs gauge",
            f'comfy_cloud_video_jobs{{state="completed"}} {m["video_jobs_completed"]}',
            f'comfy_cloud_video_jobs{{state="failed"}} {m["video_jobs_failed"]}',
        ]
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

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
            if not isinstance(body, dict):
                raise TypeError
            model = await runtime.model(body.get("model", ""))
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
            started = time.monotonic()
            refs: list[OutputRef] = []
            for index in range(count):
                if seed is not None:
                    values["seed"] = seed + index
                refs.extend(await runtime.run(model, values))
            runtime.metrics["generations_total"] = (
                int(runtime.metrics["generations_total"]) + 1
            )
            runtime.metrics["generation_seconds_total"] = float(
                runtime.metrics["generation_seconds_total"]
            ) + (time.monotonic() - started)
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
        uploaded = await runtime.comfy.upload(
            image.filename,
            image.file,
            image.content_type or "application/octet-stream",
        )
        values: dict[str, Any] = {
            "prompt": prompt,
            "image": uploaded,
            "seed": seed,
            "steps": steps,
        }
        try:
            started = time.monotonic()
            refs: list[OutputRef] = []
            for index in range(count):
                if seed is not None:
                    values["seed"] = seed + index
                refs.extend(await runtime.run(model, values))
            runtime.metrics["generations_total"] = (
                int(runtime.metrics["generations_total"]) + 1
            )
            runtime.metrics["generation_seconds_total"] = float(
                runtime.metrics["generation_seconds_total"]
            ) + (time.monotonic() - started)
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
            runtime.store_job(job)
            job.output = (await runtime.run(model, values))[0]
            await runtime.upload_output(job)
            job.status = "completed"
            runtime.store_job(job)
            runtime.metrics["video_jobs_completed"] = (
                int(runtime.metrics["video_jobs_completed"]) + 1
            )
        except Exception as exc:  # noqa: BLE001 - task boundary exposes errors through job status
            job.status = "failed"
            job.error = str(exc)
            runtime.store_job(job)
            runtime.metrics["video_jobs_failed"] = (
                int(runtime.metrics["video_jobs_failed"]) + 1
            )

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
                body["image"] = await runtime.comfy.upload(
                    image.filename,
                    image.file,
                    image.content_type or "application/octet-stream",
                )
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
                body = await request.json()
                if not isinstance(body, dict):
                    raise TypeError
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
        job = VideoJob(id=f"video_{uuid.uuid4().hex}", model=model.id)
        runtime.jobs[job.id] = job
        runtime.store_job(job)
        runtime.metrics["video_jobs_created"] = (
            int(runtime.metrics["video_jobs_created"]) + 1
        )
        runtime.start_background_task(execute_video(job, model, values))
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
                "output_url": runtime.job_output_url(job),
            }
        )

    @app.get("/v1/videos/{job_id}/content")
    async def video_content(job_id: str, request: Request) -> Response:
        if denied := require(request):
            return denied
        job = runtime.jobs.get(job_id)
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
        if not websocket_authorized(websocket, settings):
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
