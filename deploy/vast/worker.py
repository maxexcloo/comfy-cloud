"""Vast.ai Serverless PyWorker for the comfy-control gateway.

Vast's serverless platform requires a PyWorker ingress in front of the model
server. This worker is a thin aiohttp proxy: it accepts the OpenAI-compatible
and native ComfyUI routes, injects the gateway's bearer API key, and forwards
the request to the comfy-control gateway running inside the same container.

Vast wraps serverless requests in an envelope like
``{"auth_data": {...}, "payload": {...}, "session_id": ...}``; the payload is
what clients sent. This worker unwraps that shape when present, otherwise it
forwards the raw body, so it works under both the serverless router and local
debugging. The response is streamed back unchanged, preserving the OpenAI
shapes Bifrost and Open WebUI expect.

Deploy:

1. Build and push the comfy-control image.
2. In the Vast template, point the container at that image and set the same
   environment variables as the pod deployment (``API_KEY``, ``MODE=serverless``,
   ...). Run the gateway:
       comfy-control worker
3. Run this worker as a second process in the same container:
       python /opt/comfy-control/deploy/vast/worker.py
4. Configure the serverless endpoint with ``PYWORKER_REPO`` pointing at a repo
   containing this file. Vast's engine handles queueing, autoscaling, and
   readiness; this file only routes.
"""

import json
import logging
import os

from aiohttp import ClientSession, web

log = logging.getLogger(__name__)

API_KEY = os.environ.get("API_KEY", "")
CLIENT_KEY = web.AppKey("client", ClientSession)
GATEWAY_HOST = os.environ.get("VAST_GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.environ.get("VAST_GATEWAY_PORT", "8000"))
GATEWAY_BASE = f"http://{GATEWAY_HOST}:{GATEWAY_PORT}"
HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


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
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_HEADERS | {"authorization"}
    }
    headers["Authorization"] = f"Bearer {API_KEY}"
    try:
        async with request.app[CLIENT_KEY].request(
            request.method,
            GATEWAY_BASE + request.rel_url.raw_path_qs,
            allow_redirects=False,
            data=body,
            headers=headers,
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
    if not API_KEY or API_KEY.lower() in {"change-me", "replace_me"}:
        raise RuntimeError("API_KEY must be set to a non-placeholder value")
    app[CLIENT_KEY] = ClientSession()
    log.info("gateway ready")


async def on_cleanup(app: web.Application) -> None:
    await app[CLIENT_KEY].close()


def create_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_route("*", "/{path:.*}", proxy)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    host = os.environ.get("VAST_WORKER_HOST", "0.0.0.0")
    port = int(os.environ.get("VAST_WORKER_PORT", "9000"))
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
