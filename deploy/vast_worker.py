"""Vast.ai Serverless PyWorker for the comfy-cloud gateway.

Vast's serverless platform requires a PyWorker ingress in front of the model
server. This worker is a thin aiohttp proxy: it accepts the OpenAI-compatible
and native ComfyUI routes, injects the gateway's bearer API key, and forwards
the request to the comfy-cloud gateway running inside the same container.

Vast wraps serverless requests in an envelope like
``{"auth_data": {...}, "payload": {...}, "session_id": ...}``; the payload is
what clients sent. This worker unwraps that shape when present, otherwise it
forwards the raw body, so it works under both the serverless router and local
debugging. The response is streamed back unchanged, preserving the OpenAI
shapes Bifrost and Open WebUI expect.

Deploy:

1. Build and push the comfy-cloud image.
2. In the Vast template, point the container at that image and set the same
   environment variables as the pod deployment (``API_KEY``, ``MODE=serverless``,
   ...). Run the gateway:
       python -m comfy_cloud.supervisor
3. Run this worker as a second process in the same container:
       pip install aiohttp
       python vast_worker.py
4. Configure the serverless endpoint with ``PYWORKER_REPO`` pointing at a repo
   containing this file (plus a ``requirements.txt`` with ``aiohttp``). Vast's
   engine handles queueing, autoscaling, and readiness; this file only routes.
"""

import json
import logging
import os

from aiohttp import web

log = logging.getLogger(__name__)

GATEWAY_HOST = os.environ.get("VAST_GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.environ.get("VAST_GATEWAY_PORT", "8000"))
GATEWAY_BASE = f"http://{GATEWAY_HOST}:{GATEWAY_PORT}"
API_KEY = os.environ.get("API_KEY", "")

# Routes this worker accepts. Keep this in sync with the gateway routes your
# clients actually use; Vast registers one handler per route.
ROUTES = [
    "/v1/models",
    "/v1/images/generations",
    "/v1/images/edits",
    "/v1/videos",
    "/health",
    "/ping",
    "/prompt",
    "/view",
    "/queue",
    "/object_info",
    "/system_stats",
]


def _unwrapped(body: dict) -> dict:
    """Return the client payload if Vast wrapped the request, else the body."""
    if "payload" in body and isinstance(body["payload"], dict):
        return body["payload"]
    return body


async def proxy(request: web.Request) -> web.StreamResponse:
    try:
        raw = await request.read()
    except Exception:  # noqa: BLE001 - proxy boundary, return client error
        return web.Response(status=400)

    body = raw
    if raw and request.content_type == "application/json":
        try:
            body = json.dumps(_unwrapped(json.loads(raw))).encode()
        except (ValueError, TypeError):
            pass

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": request.content_type or "application/json",
    }
    try:
        async with request.app["client"].post(
            GATEWAY_BASE + request.path, data=body, headers=headers
        ) as upstream:
            response = web.StreamResponse(status=upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() in {"content-length", "transfer-encoding", "connection"}:
                    continue
                response.headers[key] = value
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                if chunk:
                    await response.write(chunk)
            return response
    except Exception:  # noqa: BLE001 - proxy boundary, surface upstream failure
        return web.Response(status=502)


async def on_startup(app: web.Application) -> None:
    import aiohttp  # noqa: PLC0415 - aiohttp is already required

    app["client"] = aiohttp.ClientSession()
    log.info("gateway ready")


async def on_cleanup(app: web.Application) -> None:
    await app["client"].close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    for route in ROUTES:
        app.router.add_route("*", route, proxy)
    host = os.environ.get("VAST_WORKER_HOST", "0.0.0.0")
    port = int(os.environ.get("VAST_WORKER_PORT", "9000"))
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
