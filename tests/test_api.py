import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from comfy_cloud.app import create_app
from comfy_cloud.comfy import OutputRef
from comfy_cloud.config import Settings

ROOT = Path(__file__).parents[1]


def settings(deployment_type: str = "serverless") -> Settings:
    return Settings(
        api_key="test-key",
        catalog_dirs=(ROOT / "catalog",),
        comfy_url="http://comfy.internal",
        deployment_type=deployment_type,
        models_dir=ROOT / "models",
        public_base_url="http://test",
        request_timeout=1,
        ui_password="ui-key",
        ui_username="comfy",
        workflow_timeout=1,
    )


@pytest.mark.asyncio
async def test_models_are_workflows_and_require_auth():
    app = create_app(settings())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.get("/v1/models")
        response = await client.get(
            "/v1/models", headers={"Authorization": "Bearer test-key"}
        )
        detail = await client.get(
            "/v1/models/example/checkpoint-text-to-image",
            headers={"Authorization": "Bearer test-key"},
        )

    assert denied.status_code == 401
    assert response.json()["data"][0]["id"] == "example/checkpoint-text-to-image"
    assert detail.json()["capabilities"]["operation"] == "image_generation"


@pytest.mark.asyncio
async def test_openai_image_generation_uses_workflow_model():
    app = create_app(settings())
    app.state.runtime.run = AsyncMock(return_value=[OutputRef("result.png")])
    app.state.runtime.comfy.fetch_output = AsyncMock(
        return_value=httpx.Response(
            200, content=b"png-bytes", headers={"content-type": "image/png"}
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "example/checkpoint-text-to-image",
                "prompt": "a clean test",
                "size": "768x512",
            },
        )

    assert response.status_code == 200
    assert response.json()["data"] == [{"b64_json": "cG5nLWJ5dGVz"}]
    values = app.state.runtime.run.await_args.args[1]
    assert values["prompt"] == "a clean test"
    assert (values["width"], values["height"]) == (768, 512)


@pytest.mark.asyncio
async def test_openai_image_edit_uses_multipart_and_workflow():
    app = create_app(settings())
    app.state.runtime.catalog.get("flux-2-klein-4b-edit").required_files = []
    run_values: list[dict] = []
    app.state.runtime.run = AsyncMock(
        side_effect=lambda model, values: (
            run_values.append(dict(values))
            or [OutputRef("edited.png"), OutputRef("edited_1.png")]
        )
    )
    app.state.runtime.comfy.fetch_output = AsyncMock(
        side_effect=[
            httpx.Response(
                200, content=b"edit-1", headers={"content-type": "image/png"}
            ),
            httpx.Response(
                200, content=b"edit-2", headers={"content-type": "image/png"}
            ),
        ]
    )
    app.state.runtime.comfy.upload = AsyncMock(return_value="uploaded.png")
    transport = httpx.ASGITransport(app=app)
    files = {"image": ("source.png", b"image-bytes", "image/png")}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/images/edits",
            headers={"Authorization": "Bearer test-key"},
            data={
                "model": "flux-2-klein-4b-edit",
                "prompt": "make it blue",
                "n": "2",
                "seed": "10",
                "steps": "8",
            },
            files=files,
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data == [{"b64_json": "ZWRpdC0x"}, {"b64_json": "ZWRpdC0y"}]
    uploaded_name, content, content_type = (
        app.state.runtime.comfy.upload.await_args.args
    )
    assert uploaded_name == "source.png"
    assert content == b"image-bytes"
    assert content_type == "image/png"
    assert app.state.runtime.run.call_count == 2
    first_values = run_values[0]
    assert first_values["image"] == "uploaded.png"
    assert first_values["prompt"] == "make it blue"
    assert first_values["seed"] == 10
    assert first_values["steps"] == 8
    assert run_values[1]["seed"] == 11


@pytest.mark.asyncio
async def test_image_edit_rejects_unsupported_parameters():
    app = create_app(settings())
    app.state.runtime.catalog.get("flux-2-klein-4b-edit").required_files = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        no_prompt = await client.post(
            "/v1/images/edits",
            headers={"Authorization": "Bearer test-key"},
            data={"model": "flux-2-klein-4b-edit"},
            files={"image": ("source.png", b"image-bytes", "image/png")},
        )
        bad_size = await client.post(
            "/v1/images/edits",
            headers={"Authorization": "Bearer test-key"},
            data={"model": "flux-2-klein-4b-edit", "prompt": "ok", "size": "512x512"},
            files={"image": ("source.png", b"image-bytes", "image/png")},
        )

    assert no_prompt.status_code == 400
    assert no_prompt.json()["error"]["code"] == "missing_required_parameter"
    assert bad_size.status_code == 400
    assert bad_size.json()["error"]["code"] == "invalid_value"


@pytest.mark.asyncio
async def test_image_to_video_accepts_multipart_upload():
    app = create_app(settings())
    app.state.runtime.catalog.get("minimax-h3-i2v").required_files = []
    run_values: list[dict] = []
    app.state.runtime.run = AsyncMock(
        side_effect=lambda model, values: (
            run_values.append(dict(values))
            or [OutputRef("result.mp4", media_type="video/mp4")]
        )
    )
    app.state.runtime.comfy.upload = AsyncMock(return_value="frame.png")
    transport = httpx.ASGITransport(app=app)
    files = {"image": ("source.png", b"image-bytes", "image/png")}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/videos",
            headers={"Authorization": "Bearer test-key"},
            data={
                "model": "minimax-h3-i2v",
                "prompt": "walk forward",
                "seconds": "3",
                "seed": "5",
            },
            files=files,
        )
        await asyncio.sleep(0)

    assert response.status_code == 200
    assert app.state.runtime.comfy.upload.await_args.args[0] == "source.png"
    values = run_values[0]
    assert values["image"] == "frame.png"
    assert values["prompt"] == "walk forward"
    assert values["seed"] == 5
    assert values["length"] == 73


@pytest.mark.asyncio
async def test_image_to_video_requires_uploaded_image():
    app = create_app(settings())
    app.state.runtime.catalog.get("minimax-h3-i2v").required_files = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/videos",
            headers={"Authorization": "Bearer test-key"},
            json={"model": "minimax-h3-i2v", "prompt": "walk forward"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_required_parameter"


@pytest.mark.asyncio
async def test_model_with_unregistered_nodes_is_not_advertised():
    app = create_app(settings())

    async def comfy_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/object_info":
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"system": "ok"})

    await app.state.runtime.comfy.http.aclose()
    app.state.runtime.comfy.http = httpx.AsyncClient(
        base_url="http://comfy.internal",
        transport=httpx.MockTransport(comfy_handler),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listing = await client.get(
            "/v1/models", headers={"Authorization": "Bearer test-key"}
        )
        detail = await client.get(
            "/v1/models/example/checkpoint-text-to-image",
            headers={"Authorization": "Bearer test-key"},
        )
    await app.state.runtime.comfy.http.aclose()

    assert listing.json()["data"] == []
    assert detail.status_code == 404


@pytest.mark.asyncio
async def test_health_live_ready_and_metrics():
    app = create_app(settings())
    app.state.runtime.comfy.ready = AsyncMock(return_value=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        live = await client.get("/health/live")
        ready = await client.get("/health/ready")
        metrics = await client.get("/metrics")
        first = await client.get(
            "/v1/models", headers={"Authorization": "Bearer test-key"}
        )
        await client.get("/v1/models", headers={"Authorization": "Bearer test-key"})

    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert first.headers.get("x-request-id")
    body = metrics.text
    assert "comfy_cloud_requests_total 2" in body
    assert 'comfy_cloud_requests_by_status{status="200"}' in body
    assert "comfy_cloud_generations_total 0" in body


@pytest.mark.asyncio
async def test_runpod_ping_reports_comfy_readiness():
    app = create_app(settings())
    app.state.runtime.comfy.ready = AsyncMock(side_effect=[False, True])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        loading = await client.get("/ping")
        ready = await client.get("/ping")

    assert loading.status_code == 204
    assert ready.status_code == 200


@pytest.mark.asyncio
async def test_video_request_maps_openai_size_and_seconds_to_workflow():
    app = create_app(settings())
    app.state.runtime.catalog.get("minimax-h3").required_files = []
    app.state.runtime.run = AsyncMock(
        return_value=[OutputRef("result.mp4", media_type="video/mp4")]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/videos",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "minimax-h3",
                "prompt": "a clean video test",
                "seconds": 5,
                "size": "1344x768",
            },
        )
        await asyncio.sleep(0)

    assert response.status_code == 200
    values = app.state.runtime.run.await_args.args[1]
    assert values["length"] == 124
    assert (values["width"], values["height"]) == (1344, 768)


@pytest.mark.asyncio
async def test_serverless_blocks_frontend_but_proxies_native_api():
    app = create_app(settings())

    async def comfy_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/system_stats"
        return httpx.Response(200, json={"system": "ok"})

    await app.state.runtime.comfy.http.aclose()
    app.state.runtime.comfy.http = httpx.AsyncClient(
        base_url="http://comfy.internal",
        transport=httpx.MockTransport(comfy_handler),
    )
    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer test-key"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        frontend = await client.get("/", headers=headers)
        native = await client.get("/system_stats", headers=headers)
    await app.state.runtime.comfy.http.aclose()

    assert frontend.status_code == 404
    assert native.json() == {"system": "ok"}


@pytest.mark.asyncio
async def test_pod_proxies_frontend_with_basic_auth():
    app = create_app(settings("pod"))

    async def comfy_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="ComfyUI", headers={"content-type": "text/html"}
        )

    await app.state.runtime.comfy.http.aclose()
    app.state.runtime.comfy.http = httpx.AsyncClient(
        base_url="http://comfy.internal",
        transport=httpx.MockTransport(comfy_handler),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/", auth=("comfy", "ui-key"))
    await app.state.runtime.comfy.http.aclose()

    assert response.status_code == 200
    assert response.text == "ComfyUI"
