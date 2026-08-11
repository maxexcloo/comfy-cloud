import json

import pytest
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

from comfy_control import vast_worker


@pytest.mark.asyncio
async def test_proxy_preserves_method_query_and_multipart(monkeypatch):
    received = []

    async def upstream(request):
        item = {
            "method": request.method,
            "path_qs": request.rel_url.path_qs,
            "content_type": request.headers.get("Content-Type", ""),
            "authorization": request.headers.get("Authorization"),
        }
        if request.content_type == "multipart/form-data":
            form = await request.post()
            item["image"] = form["image"].file.read()
        received.append(item)
        return web.json_response({"ok": True})

    upstream_app = web.Application()
    upstream_app.router.add_route("*", "/{path:.*}", upstream)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()
    monkeypatch.setattr(vast_worker, "API_KEY", "test-key")
    monkeypatch.setattr(
        vast_worker, "GATEWAY_BASE", str(upstream_server.make_url("/")).rstrip("/")
    )

    client = TestClient(TestServer(vast_worker.create_app()))
    await client.start_server()
    try:
        response = await client.get("/view?filename=a%20b.png&type=output")
        assert response.status == 200

        form = FormData()
        form.add_field(
            "image", b"image-bytes", filename="source.png", content_type="image/png"
        )
        response = await client.post("/v1/images/edits", data=form)
        assert response.status == 200
    finally:
        await client.close()
        await upstream_server.close()

    assert received[0] == {
        "authorization": "Bearer test-key",
        "content_type": "application/octet-stream",
        "method": "GET",
        "path_qs": "/view?filename=a b.png&type=output",
    }
    assert received[1]["authorization"] == "Bearer test-key"
    assert received[1]["content_type"].startswith("multipart/form-data; boundary=")
    assert received[1]["image"] == b"image-bytes"
    assert received[1]["method"] == "POST"


@pytest.mark.asyncio
async def test_proxy_unwraps_vast_json_envelope(monkeypatch):
    async def upstream(request):
        return web.json_response(await request.json())

    upstream_app = web.Application()
    upstream_app.router.add_post("/v1/videos", upstream)
    upstream_server = TestServer(upstream_app)
    await upstream_server.start_server()
    monkeypatch.setattr(vast_worker, "API_KEY", "test-key")
    monkeypatch.setattr(
        vast_worker, "GATEWAY_BASE", str(upstream_server.make_url("/")).rstrip("/")
    )

    client = TestClient(TestServer(vast_worker.create_app()))
    await client.start_server()
    try:
        response = await client.post(
            "/v1/videos",
            json={"payload": {"model": "minimax-h3", "prompt": "test"}},
        )
        assert json.loads(await response.text()) == {
            "model": "minimax-h3",
            "prompt": "test",
        }
    finally:
        await client.close()
        await upstream_server.close()
