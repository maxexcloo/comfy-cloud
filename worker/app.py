from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlencode

import httpx
import websockets
from fastapi import FastAPI, Form, Request, WebSocket
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST
from starlette.background import BackgroundTask

from worker.auth import request_authorised, websocket_authorised
from worker.config import Settings
from worker.execution import (
    ExecutionOutput,
    ExecutionRequest,
    ExecutionResult,
)
from worker.logs import entries as worker_log_entries
from worker.runtime import GenerationQueueFull, Runtime

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
    "api",
    "free",
    "history",
    "interrupt",
    "object_info",
    "prompt",
    "queue",
    "system_stats",
    "upload",
    "userdata",
    "view",
)


def worker_error(message: str, status: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )


def create_app(settings: Settings) -> FastAPI:
    runtime = Runtime(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await runtime.close()

    app = FastAPI(
        title="Comfy Control Worker API",
        description=(
            "Current controller-to-worker execution contract. The OpenAI-compatible "
            "public API is served by the controller."
        ),
        version="current",
        lifespan=lifespan,
    )
    app.state.runtime = runtime

    def require(request: Request) -> JSONResponse | None:
        if request_authorised(request, settings):
            return None
        return JSONResponse(
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
            content={
                "error": {
                    "code": "invalid_api_key",
                    "message": "invalid API key",
                }
            },
        )

    async def limited_body(request: Request) -> bytes:
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > settings.maximum_request_bytes:
                raise ValueError("request body is too large")
        return bytes(body)

    def execution_result(execution_id: str) -> ExecutionResult:
        return ExecutionResult(
            execution_id=execution_id,
            outputs=[
                ExecutionOutput(
                    index=index,
                    content_type=output.media_type,
                    filename=output.filename,
                    url=str(
                        app.url_path_for(
                            "internal_execution_output",
                            execution_id=execution_id,
                            index=index,
                        )
                    ),
                )
                for index, output in enumerate(
                    runtime.execution_outputs.get(execution_id, [])
                )
            ],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            content_length = 0
        if content_length > settings.maximum_request_bytes:
            response = worker_error(
                "request body is too large", 413, "request_too_large"
            )
        else:
            response = await call_next(request)
        response.headers["x-request-id"] = request_id
        runtime.requests.inc()
        runtime.requests_by_status.labels(str(response.status_code)).inc()
        return response

    @app.get("/health/live", tags=["health"], operation_id="worker_liveness")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready", tags=["health"], operation_id="worker_readiness")
    async def ready() -> Response:
        is_ready = await runtime.ready()
        return JSONResponse(
            {
                "phase": runtime.phase,
                "status": "ready" if is_ready else "starting",
                "warmed_model": runtime.warmed_model,
                "warmup_error": runtime.warmup_error,
            },
            status_code=200 if is_ready else 503,
        )

    @app.get("/health", tags=["health"], operation_id="worker_health")
    async def health() -> Response:
        object_info = await runtime.object_info()
        available = runtime.available_models(object_info)
        return JSONResponse(
            {
                "comfyui": "ready" if object_info is not None else "unavailable",
                "models": len(available),
                "phase": runtime.phase,
                "status": ("ready" if await runtime.ready() else runtime.phase),
                "unavailable_models": len(runtime.catalogue.list()) - len(available),
                "warmed_model": runtime.warmed_model,
            },
            status_code=200 if object_info is not None else 503,
        )

    @app.get("/metrics", tags=["health"], operation_id="worker_metrics")
    async def metrics() -> Response:
        return Response(
            generate_latest(runtime.metric_registry),
            media_type=CONTENT_TYPE_LATEST,
        )

    @app.get("/internal/info", tags=["internal"], operation_id="worker_info")
    async def internal_info(request: Request) -> Response:
        if denied := require(request):
            return denied
        object_info = await runtime.object_info()
        return JSONResponse(
            {
                "capabilities": [
                    "cancel",
                    "image_edit",
                    "image_generation",
                    "image_upscale",
                    "video_generation",
                ],
                "deployment_type": settings.deployment_type,
                "models": [model.id for model in runtime.available_models(object_info)],
                "pending_generations": runtime.pending_generations,
                "phase": runtime.phase,
                "queue_limit": settings.generation_queue_limit,
                "ready": await runtime.ready(),
                "warmed_model": runtime.warmed_model,
                "warmup_error": runtime.warmup_error,
            }
        )

    @app.get("/internal/logs", tags=["internal"], operation_id="worker_logs")
    async def internal_logs(request: Request, limit: int = 200) -> Response:
        if denied := require(request):
            return denied
        return JSONResponse(
            {
                "entries": worker_log_entries(min(max(limit, 1), 500)),
                "source": "Worker",
            }
        )

    @app.post(
        "/internal/executions",
        tags=["internal"],
        operation_id="create_worker_execution",
        response_model=ExecutionResult,
    )
    async def internal_execution(
        request: Request, spec: Annotated[str, Form()]
    ) -> Response:
        if denied := require(request):
            return denied
        try:
            execution = ExecutionRequest.model_validate_json(spec)
            cached = runtime.execution_outputs.get(execution.execution_id)
            if cached is not None:
                return JSONResponse(
                    execution_result(execution.execution_id).model_dump()
                )
            model = await runtime.model(execution.model)
            if model.operation != execution.operation:
                return worker_error(
                    f"model '{model.id}' does not support {execution.operation}",
                    400,
                    "unsupported_operation",
                )
            values: dict[str, Any] = dict(execution.parameters)
            uploads: dict[str, list[str]] = {}
            form = await request.form()
            for field_name, value in form.multi_items():
                if field_name == "spec" or not hasattr(value, "read"):
                    continue
                if (
                    value.size is not None
                    and value.size > settings.maximum_request_bytes
                ):
                    return worker_error(
                        f"{field_name} is too large", 413, "request_too_large"
                    )
                uploaded = await runtime.comfy.upload(
                    value.filename or "input",
                    value.file,
                    value.content_type or "application/octet-stream",
                )
                uploads.setdefault(field_name, []).append(uploaded)
            for field_name, uploaded in uploads.items():
                values[field_name] = uploaded[0] if len(uploaded) == 1 else uploaded
            task, owner = await runtime.start_execution(
                execution.execution_id, model, values
            )
        except GenerationQueueFull as exc:
            return worker_error(str(exc), 429, "execution_queue_full")
        except (KeyError, TypeError, ValueError) as exc:
            return worker_error(str(exc), 400, "invalid_execution")

        outputs = None
        try:
            outputs = await task
            if owner:
                runtime.execution_outputs[execution.execution_id] = outputs
        except asyncio.CancelledError:
            return worker_error("execution was cancelled", 409, "execution_cancelled")
        except (RuntimeError, TimeoutError, httpx.HTTPError) as exc:
            return worker_error(str(exc), 502, "execution_failed")
        finally:
            if owner:
                await runtime.finish_execution(execution.execution_id, task, outputs)
        return JSONResponse(execution_result(execution.execution_id).model_dump())

    @app.get(
        "/internal/executions/{execution_id}/outputs/{index}",
        tags=["internal"],
        operation_id="internal_execution_output",
    )
    async def internal_execution_output(
        execution_id: str, index: int, request: Request
    ) -> Response:
        if denied := require(request):
            return denied
        outputs = runtime.execution_outputs.get(execution_id, [])
        if index < 0 or index >= len(outputs):
            return worker_error("execution output was not found", 404, "not_found")
        output = outputs[index]
        return StreamingResponse(
            runtime.comfy.stream_output(output), media_type=output.media_type
        )

    @app.delete(
        "/internal/executions/{execution_id}",
        tags=["internal"],
        operation_id="cancel_worker_execution",
        status_code=204,
    )
    async def cancel_internal_execution(
        execution_id: str, request: Request
    ) -> Response:
        if denied := require(request):
            return denied
        task = runtime.execution_tasks.get(execution_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return Response(status_code=204)

    @app.get("/ping", include_in_schema=False)
    async def ping() -> Response:
        return Response(status_code=200 if await runtime.ready() else 204)

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
        include_in_schema=False,
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
        except ValueError as exc:
            return worker_error(str(exc), 413, "request_too_large")
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower()
            not in HOP_HEADERS | {"authorization", "content-length", "host"}
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

    def current_openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                description=app.description,
                routes=app.routes,
            )
            schema["paths"] = {
                path: value
                for path, value in schema["paths"].items()
                if path.startswith(("/health", "/internal/")) or path == "/metrics"
            }
            components = schema.setdefault("components", {})
            components.setdefault("securitySchemes", {})["bearerAuth"] = {
                "scheme": "bearer",
                "type": "http",
            }
            for path, value in schema["paths"].items():
                if path.startswith("/internal/"):
                    for operation in value.values():
                        operation["security"] = [{"bearerAuth": []}]
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = current_openapi

    return app


def create_pod_app() -> FastAPI:
    return create_app(Settings.from_env("pod"))


def create_serverless_app() -> FastAPI:
    return create_app(Settings.from_env("serverless"))
