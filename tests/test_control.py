import asyncio
import re
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from comfy_control.control import create_app
from comfy_control.control_config import ControlFile, ControlSettings

ROOT = Path(__file__).parents[1]


def write_config(path: Path) -> None:
    path.write_text(
        """
models:
  - id: public/image
    operation: image_generation
    targets:
      - model: worker/image
        provider: worker
  - id: public/image-edit
    operation: image_edit
    targets:
      - model: worker/image-edit
        provider: worker
  - id: public/video
    operation: video_generation
    targets:
      - model: worker/video
        provider: worker
providers:
  - id: worker
    api_key: worker-key
    base_url: http://worker
    idle_seconds: 0
""".lstrip()
    )


def settings(tmp_path: Path) -> ControlSettings:
    config = tmp_path / "control.yaml"
    write_config(config)
    return ControlSettings(
        api_key="control-key",
        config_file=config,
        database_path=tmp_path / "control.db",
        maximum_request_bytes=1024 * 1024,
        ui_password="",
        ui_username="comfy",
    )


def worker_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/v1/images/generations")
    async def image(request: Request) -> dict[str, object]:
        body = await request.json()
        assert body["model"] == "worker/image"
        return {"created": 1, "data": [{"b64_json": "aW1hZ2U="}]}

    @app.post("/v1/images/edits")
    async def image_edit(request: Request) -> dict[str, object]:
        form = await request.form()
        images = form.getlist("image")
        assert form["model"] == "worker/image-edit"
        assert [image.filename for image in images] == ["mara.png", "elise.png"]
        assert [await image.read() for image in images] == [b"mara", b"elise"]
        return {"created": 1, "data": [{"b64_json": "ZWRpdA=="}]}

    @app.post("/v1/videos")
    async def video(request: Request) -> dict[str, object]:
        assert request.query_params["wait"] == "true"
        assert request.headers["x-comfy-job-id"].startswith("video_")
        if request.headers["content-type"].startswith("multipart/form-data"):
            form = await request.form()
            assert form["model"] == "worker/video"
            assert await form["image"].read() == b"image"
        else:
            body = await request.json()
            assert body["model"] == "worker/video"
        return {
            "id": "upstream-video",
            "object": "video",
            "model": "worker/video",
            "status": "completed",
            "created_at": 1,
            "error": None,
            "output_url": "https://objects.example/video.mp4",
        }

    return app


async def attach_worker(app: FastAPI) -> None:
    runtime = app.state.controller.providers["worker"]
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=worker_app()), base_url="http://worker"
    )


@pytest.mark.asyncio
async def test_controller_lists_and_routes_models(tmp_path):
    app = create_app(settings(tmp_path))
    await attach_worker(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        health = await client.get("/health")
        denied = await client.get("/v1/models")
        dashboard = await client.get("/", auth=("comfy", "control-key"))
        models = await client.get(
            "/v1/models", headers={"Authorization": "Bearer control-key"}
        )
        image = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "public/image", "prompt": "test"},
        )

    assert health.json() == {
        "status": "ready",
        "models": 3,
        "providers": 1,
        "ready_providers": 1,
    }
    assert denied.status_code == 401
    assert dashboard.status_code == 200
    assert "Action Result" in dashboard.text
    assert [model["id"] for model in models.json()["data"]] == [
        "public/image",
        "public/image-edit",
        "public/video",
    ]
    assert image.status_code == 200
    assert image.headers["x-comfy-provider"] == "worker"
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_preserves_repeated_image_fields_in_order(tmp_path):
    app = create_app(settings(tmp_path))
    await attach_worker(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/edits",
            headers={"Authorization": "Bearer control-key"},
            data={"model": "public/image-edit", "prompt": "Mara and Elise"},
            files=[
                ("image", ("mara.png", b"mara", "image/png")),
                ("image", ("elise.png", b"elise", "image/png")),
            ],
        )

    assert response.status_code == 200
    assert response.headers["x-comfy-provider"] == "worker"
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_owns_video_job_and_output_url(tmp_path):
    app = create_app(settings(tmp_path))
    await attach_worker(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        submitted = await client.post(
            "/v1/videos",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "public/video", "prompt": "test"},
        )
        job_id = submitted.json()["id"]
        for _ in range(20):
            status = await client.get(
                f"/v1/videos/{job_id}",
                headers={"Authorization": "Bearer control-key"},
            )
            if status.json()["status"] == "completed":
                break
            await asyncio.sleep(0)
        content = await client.get(
            f"/v1/videos/{job_id}/content",
            headers={"Authorization": "Bearer control-key"},
            follow_redirects=False,
        )

    assert submitted.status_code == 200
    assert status.json()["model"] == "public/video"
    assert content.status_code == 302
    assert content.headers["location"] == "https://objects.example/video.mp4"
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_persists_multipart_video_until_completion(tmp_path):
    app = create_app(settings(tmp_path))
    await attach_worker(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        submitted = await client.post(
            "/v1/videos",
            headers={"Authorization": "Bearer control-key"},
            data={"model": "public/video", "prompt": "test"},
            files={"image": ("frame.png", b"image", "image/png")},
        )
        job_id = submitted.json()["id"]
        for _ in range(20):
            status = await client.get(
                f"/v1/videos/{job_id}",
                headers={"Authorization": "Bearer control-key"},
            )
            if status.json()["status"] == "completed":
                break
            await asyncio.sleep(0)

    assert status.json()["status"] == "completed", status.json()["error"]
    assert not (tmp_path / "uploads" / job_id).exists()
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_uses_ordered_fallback(tmp_path):
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models:
  - id: public/image
    operation: image_generation
    targets:
      - model: worker/image
        provider: primary
      - model: worker/image
        provider: fallback
providers:
  - id: fallback
    api_key: worker-key
    base_url: http://fallback
    idle_seconds: 0
  - id: primary
    api_key: worker-key
    base_url: http://primary
    idle_seconds: 0
    startup_timeout: 0
""".lstrip()
    )
    app = create_app(configured)

    calls: dict[int, int] = {}

    def provider(status_code: int) -> FastAPI:
        provider_app = FastAPI()

        @provider_app.get("/health/ready")
        async def ready() -> dict[str, str]:
            return {"status": "ready"}

        @provider_app.post("/v1/images/generations")
        async def image() -> JSONResponse:
            calls[status_code] = calls.get(status_code, 0) + 1
            return JSONResponse({"provider": status_code}, status_code=status_code)

        return provider_app

    for provider_id, status_code in (("fallback", 200), ("primary", 503)):
        runtime = app.state.controller.providers[provider_id]
        await runtime.client.aclose()
        runtime.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=provider(status_code)),
            base_url=f"http://{provider_id}",
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "public/image", "prompt": "test"},
        )

    assert response.status_code == 200
    assert response.headers["x-comfy-provider"] == "fallback"
    assert calls == {200: 1, 503: 1}
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_reserves_provider_before_readiness_check(tmp_path):
    app = create_app(settings(tmp_path))
    runtime = app.state.controller.providers["worker"]
    provider_app = FastAPI()

    @provider_app.get("/health/ready")
    async def ready() -> dict[str, str]:
        assert runtime.active_requests == 1
        return {"status": "ready"}

    @provider_app.post("/v1/images/generations")
    async def image() -> dict[str, object]:
        return {"data": []}

    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=provider_app), base_url="http://worker"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "public/image", "prompt": "test"},
        )

    assert response.status_code == 200
    assert runtime.active_requests == 0
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_limits_chunked_requests_while_streaming(tmp_path):
    configured = settings(tmp_path)
    configured = replace(configured, maximum_request_bytes=8)
    app = create_app(configured)

    async def chunks():
        yield b"12345"
        yield b"67890"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            content=chunks(),
        )

    assert response.status_code == 413
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_exposes_authenticated_provider_actions(tmp_path):
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models: []
providers:
  - id: worker
    api_key: worker-key
    base_url: http://worker
    actions:
      deploy:
        confirmation: Deploy the worker?
        url: http://management/deploy
""".lstrip()
    )
    app = create_app(configured)
    management = FastAPI()

    @management.post("/deploy")
    async def deploy() -> dict[str, str]:
        return {"api_key": "must-not-reach-the-browser", "status": "deployed"}

    await app.state.controller.lifecycle_client.aclose()
    app.state.controller.lifecycle_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=management), base_url="http://management"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        status = await client.get("/api/status", auth=("comfy", "control-key"))
        rejected = await client.post(
            "/api/providers/worker/actions/deploy", auth=("comfy", "control-key")
        )
        deployed = await client.post(
            "/api/providers/worker/actions/deploy",
            auth=("comfy", "control-key"),
            headers={"x-comfy-control-action": "worker/deploy"},
        )

    assert status.json()["providers"][0]["actions"] == [
        {"name": "deploy", "confirmation": "Deploy the worker?"}
    ]
    assert rejected.status_code == 400
    assert deployed.json()["body"] == {"api_key": "***", "status": "deployed"}
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_tracks_provider_api_resource(tmp_path):
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models: []
providers:
  - id: worker
    api_key: worker-key
    base_url: https://{resource_id}-8000.example.test
    actions:
      deploy:
        resource_id_path: resource.id
        url: http://management/deploy
      destroy:
        method: DELETE
        url: http://management/resources/{resource_id}
""".lstrip()
    )
    app = create_app(configured)
    management = FastAPI()
    destroyed: list[str] = []

    @management.post("/deploy")
    async def deploy() -> dict[str, object]:
        return {"resource": {"id": "pod-123"}}

    @management.delete("/resources/{resource_id}")
    async def destroy(resource_id: str) -> dict[str, bool]:
        destroyed.append(resource_id)
        return {"deleted": True}

    await app.state.controller.lifecycle_client.aclose()
    app.state.controller.lifecycle_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=management), base_url="http://management"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        deployed = await client.post(
            "/api/providers/worker/actions/deploy",
            auth=("comfy", "control-key"),
            headers={"x-comfy-control-action": "worker/deploy"},
        )
        status = await client.get("/api/status", auth=("comfy", "control-key"))
        destroyed_response = await client.post(
            "/api/providers/worker/actions/destroy",
            auth=("comfy", "control-key"),
            headers={"x-comfy-control-action": "worker/destroy"},
        )

    assert deployed.status_code == 200
    assert status.json()["providers"][0]["resource_id"] == "pod-123"
    assert destroyed_response.status_code == 200
    assert destroyed == ["pod-123"]
    assert app.state.controller.store.provider_resource("worker") is None
    await app.state.controller.close()


def test_control_config_expands_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_TOKEN", "secret")
    config = tmp_path / "control.yaml"
    config.write_text(
        """
models: []
providers:
  - id: worker
    api_key: env.CONTROL_TOKEN
    base_url: http://worker
    lifecycle:
      start:
        headers:
          authorization: Bearer ${CONTROL_TOKEN}
        url: http://control/start
    actions:
      status:
        url: http://control/status
""".lstrip()
    )

    loaded = ControlFile.load(config)

    assert loaded.providers[0].api_key == "secret"
    assert loaded.providers[0].lifecycle.start is not None
    assert loaded.providers[0].actions["status"].url == "http://control/status"
    assert (
        loaded.providers[0].lifecycle.start.headers["authorization"] == "Bearer secret"
    )


def test_control_settings_reject_placeholders(monkeypatch):
    monkeypatch.setenv("CONTROL_API_KEY", "replace-with-sk-media-key")
    monkeypatch.setenv("CONTROL_UI_PASSWORD", "secure-password")

    with pytest.raises(ValueError, match="non-placeholder"):
        ControlSettings.from_env()


def test_control_settings_reject_non_positive_request_limit(monkeypatch):
    monkeypatch.setenv("CONTROL_API_KEY", "secure-key")
    monkeypatch.setenv("CONTROL_MAXIMUM_REQUEST_BYTES", "0")
    monkeypatch.setenv("CONTROL_UI_PASSWORD", "secure-password")

    with pytest.raises(ValueError, match="at least 1"):
        ControlSettings.from_env()


def test_compose_forwards_example_provider_environment():
    example = (ROOT / "config/control.example.yaml").read_text()
    required = set(re.findall(r"(?:env\.|\$\{)([A-Z][A-Z0-9_]*)", example))
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    forwarded = set(compose["services"]["comfy-control"]["environment"])
    env_example = {
        line.partition("=")[0]
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    }

    assert required <= forwarded
    assert required <= env_example


def test_bifrost_compose_healthcheck_uses_authenticated_models_endpoint():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    healthcheck = compose["services"]["bifrost"]["healthcheck"]["test"]
    command = " ".join(healthcheck)

    assert healthcheck[0] == "CMD-SHELL"
    assert "Authorization: Bearer $${BIFROST_API_KEY}" in command
    assert "http://127.0.0.1:8080/v1/models" in command


def test_control_example_is_loadable(monkeypatch):
    monkeypatch.setenv("CLIPROXY_API_KEY", "cliproxy-key")
    monkeypatch.setenv("RUNPOD_API_KEY", "provider-key")
    monkeypatch.setenv("WORKER_API_KEY", "worker-key")

    loaded = ControlFile.load(ROOT / "config/control.example.yaml")

    provider = loaded.providers[1]
    assert provider.base_url == "https://{resource_id}-8000.proxy.runpod.net"
    assert provider.actions["deploy"].resource_id_path == "id"
