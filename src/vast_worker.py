from __future__ import annotations

import json
import logging
import os

from aiohttp import ClientSession, web

log = logging.getLogger(__name__)

API_KEY = os.environ.get("API_KEY", "")
CLIENT_KEY = web.AppKey("client", ClientSession)
GATEWAY_BASE = "http://127.0.0.1:8000"
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
    if "payload" in body and isinstance(body["payload"], dict):
        return body["payload"]
    return body


async def proxy(request: web.Request) -> web.StreamResponse:
    raw = await request.read()
    body = raw
    if raw and request.content_type == "application/json":
        try:
            body = json.dumps(_unwrapped(json.loads(raw))).encode()
        except (TypeError, ValueError):
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
                if key.lower() not in {
                    "connection",
                    "content-length",
                    "transfer-encoding",
                }:
                    response.headers[key] = value
            await response.prepare(request)
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
            return response
    except Exception:  # noqa: BLE001 - proxy boundary
        return web.Response(status=502)


async def on_startup(app: web.Application) -> None:
    if not API_KEY:
        raise RuntimeError("API_KEY must be set")
    app[CLIENT_KEY] = ClientSession()
    log.info("Vast.ai ingress ready")


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
    web.run_app(
        create_app(),
        host=os.getenv("VAST_WORKER_HOST", "0.0.0.0"),
        port=int(os.getenv("VAST_WORKER_PORT", "9000")),
    )
