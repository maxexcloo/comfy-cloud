import asyncio
import re
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from comfy_control.control import create_app
from comfy_control.control_config import ControlFile, ControlSettings
from comfy_control.controller import (
    history_parameters,
    normalise_usage,
    normalise_xai_quota,
)

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
        ui_password="ui-password",
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
        assert "provider" not in body
        return {"created": 1, "data": [{"b64_json": "aW1hZ2U="}]}

    @app.post("/v1/images/edits")
    async def image_edit(request: Request) -> dict[str, object]:
        form = await request.form()
        images = form.getlist("image")
        assert form["model"] == "worker/image-edit"
        assert "provider" not in form
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
            assert "provider" not in body
        return {
            "id": "upstream-video",
            "object": "video",
            "model": "worker/video",
            "status": "completed",
            "created_at": 1,
            "error": None,
            "output_url": "https://objects.example/video.mp4",
        }

    @app.get("/v1/videos/upstream-video/content")
    async def video_content() -> Response:
        return Response(b"video", media_type="video/mp4")

    return app


async def attach_worker(app: FastAPI) -> None:
    runtime = app.state.controller.providers["worker"]
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=worker_app()), base_url="http://worker"
    )


async def sign_in(client: httpx.AsyncClient) -> httpx.Response:
    response = await client.post(
        "/login",
        data={"password": "ui-password", "username": "comfy"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    return response


@pytest.mark.asyncio
async def test_controller_lists_and_routes_models(tmp_path):
    app = create_app(settings(tmp_path))
    await attach_worker(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        health = await client.get("/health")
        denied = await client.get("/v1/models")
        basic = await client.get("/", auth=("comfy", "ui-password"))
        login_page = await client.get("/login")
        invalid_login = await client.post(
            "/login", data={"password": "incorrect", "username": "comfy"}
        )
        await sign_in(client)
        dashboard = await client.get("/")
        models = await client.get(
            "/v1/models", headers={"Authorization": "Bearer control-key"}
        )
        image = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "worker/worker/image", "prompt": "test"},
        )
        status = await client.get("/api/status")
        history = status.json()["history"][0]
        media = await client.get(
            f"/api/history/{history['id']}/media/{history['media'][0]['id']}"
        )
        logout = await client.post("/logout", follow_redirects=False)
        denied_media = await client.get(
            f"/api/history/{history['id']}/media/{history['media'][0]['id']}"
        )

    assert health.json() == {
        "status": "ready",
        "models": 3,
        "providers": 1,
    }
    assert denied.status_code == 401
    assert basic.status_code == 303
    assert basic.headers["location"] == "/login"
    assert login_page.status_code == 200
    assert 'autocomplete="current-password"' in login_page.text
    assert invalid_login.status_code == 401
    assert dashboard.status_code == 200
    assert "Action Result" in dashboard.text
    assert "Send test request" in dashboard.text
    assert "Sign out" in dashboard.text
    assert [model["id"] for model in models.json()["data"]] == [
        "public/image",
        "public/image-edit",
        "public/video",
        "worker/worker/image",
        "worker/worker/image-edit",
        "worker/worker/video",
    ]
    assert image.status_code == 200
    assert image.headers["x-comfy-provider"] == "worker"
    assert image.headers["x-comfy-history-id"] == history["id"]
    assert history["operation"] == "image_generation"
    assert history["parameters"] == {
        "model": "worker/worker/image",
        "prompt": "test",
    }
    assert history["status"] == "completed"
    assert media.content == b"image"
    assert media.headers["content-type"] == "image/png"
    assert logout.status_code == 303
    assert logout.headers["location"] == "/login"
    assert denied_media.status_code == 401
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_dashboard_tests_provider_and_shows_control_logs(tmp_path):
    app = create_app(settings(tmp_path))
    await attach_worker(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        await sign_in(client)
        tested = await client.post(
            "/api/providers/worker/test",
            json={
                "model": "public/image",
                "prompt": "test from the dashboard",
                "size": "512x512",
            },
        )
        logs = await client.get("/api/providers/worker/logs")
        media = await client.get(
            "/api/history/"
            f"{tested.json()['history_id']}/media/{tested.json()['media'][0]['id']}"
        )

    assert tested.status_code == 200
    assert tested.json()["provider"] == "worker"
    assert tested.json()["status"] == "completed"
    assert logs.status_code == 200
    assert logs.json()["source"] == "Comfy Control"
    assert logs.json()["entries"][0]["message"] == "dashboard test completed"
    assert media.content == b"image"
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_rewrites_worker_image_url_to_archived_media(tmp_path):
    provider_app = FastAPI()

    @provider_app.get("/health/ready")
    async def provider_ready() -> dict[str, str]:
        return {"status": "ready"}

    @provider_app.post("/v1/images/generations")
    async def provider_generation() -> dict[str, object]:
        return {"created": 1, "data": [{"url": "/generated.png"}]}

    @provider_app.get("/generated.png")
    async def provider_media() -> Response:
        return Response(b"archived-image", media_type="image/png")

    app = create_app(settings(tmp_path))
    runtime = app.state.controller.providers["worker"]
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=provider_app), base_url="http://worker"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        generated = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "public/image", "prompt": "test"},
        )
        archived_url = generated.json()["data"][0]["url"]
        archived = await client.get(
            archived_url, headers={"Authorization": "Bearer control-key"}
        )

    assert archived_url.startswith("http://control/api/history/")
    assert archived.content == b"archived-image"
    assert archived.headers["content-type"] == "image/png"
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_rejects_provider_outside_selected_model(tmp_path):
    app = create_app(settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={
                "model": "public/image",
                "prompt": "test",
                "provider": "missing",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert "unknown provider: missing" in response.json()["error"]["message"]
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
            data={
                "model": "worker/worker/image-edit",
                "prompt": "Mara and Elise",
            },
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
            json={"model": "worker/worker/video", "prompt": "test"},
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
async def test_controller_archives_video_from_worker_content(tmp_path):
    app = create_app(settings(tmp_path))
    await attach_worker(app)
    controller = app.state.controller
    controller.store.save_history(
        "video_history",
        "video_generation",
        "public/video",
        '{"model":"public/video"}',
    )

    await controller.archive_video("video_history", "worker", {"id": "upstream-video"})

    archived = controller.store.media_for_history("video_history")
    assert len(archived) == 1
    assert Path(archived[0].path).read_bytes() == b"video"
    assert archived[0].content_type == "video/mp4"
    await controller.close()


@pytest.mark.asyncio
async def test_history_and_media_survive_controller_restart(tmp_path):
    configured = settings(tmp_path)
    first = create_app(configured)
    await attach_worker(first)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first), base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "public/image", "prompt": "persistent"},
        )
    history_id = response.headers["x-comfy-history-id"]
    await first.state.controller.close()

    second = create_app(configured)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=second), base_url="http://control"
    ) as client:
        await sign_in(client)
        history = await client.get("/api/history")
        item = history.json()["data"][0]
        media = await client.get(
            f"/api/history/{history_id}/media/{item['media'][0]['id']}"
        )

    assert item["id"] == history_id
    assert item["parameters"]["prompt"] == "persistent"
    assert media.content == b"image"
    await second.state.controller.close()


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
        bearer = {"Authorization": "Bearer control-key"}
        status = await client.get("/api/status", headers=bearer)
        rejected = await client.post(
            "/api/providers/worker/actions/deploy", headers=bearer
        )
        deployed = await client.post(
            "/api/providers/worker/actions/deploy",
            headers={
                **bearer,
                "x-comfy-control-action": "worker/deploy",
            },
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
        await sign_in(client)
        deployed = await client.post(
            "/api/providers/worker/actions/deploy",
            headers={"x-comfy-control-action": "worker/deploy"},
        )
        status = await client.get("/api/status")
        destroyed_response = await client.post(
            "/api/providers/worker/actions/destroy",
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


def test_compose_forwards_control_provider_environment():
    control = (ROOT / "config/control.yaml").read_text()
    required = set(re.findall(r"(?:env\.|\$\{)([A-Z][A-Z0-9_]*)", control))
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert set(compose["services"]) == {"comfy-control"}
    forwarded = set(compose["services"]["comfy-control"]["environment"])
    env_example = {
        line.partition("=")[0]
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    }

    assert required <= forwarded
    assert required <= env_example


def test_control_file_enables_configured_providers(monkeypatch):
    monkeypatch.setenv("CLIPROXY_API_KEY", "cliproxy-key")
    monkeypatch.setenv("CLIPROXY_MANAGEMENT_KEY", "management-key")
    monkeypatch.setenv("CLIPROXY_URL", "http://cliproxy")
    monkeypatch.setenv("RUNPOD_POD_ID", "pod-id")
    monkeypatch.setenv("RUNPOD_POD_URL", "https://pod.example")
    monkeypatch.setenv("RUNPOD_API_KEY", "provider-key")
    monkeypatch.setenv("WORKER_API_KEY", "worker-key")

    loaded = ControlFile.load(ROOT / "config/control.yaml")

    assert [provider.id for provider in loaded.providers] == [
        "cliproxyapi",
        "runpod-pod",
        "runpod-serverless",
    ]
    assert loaded.models[0].targets[-1].provider == "cliproxyapi"
    assert loaded.providers[1].type == "pod"


def test_control_file_enables_managed_providers_from_credentials(monkeypatch):
    monkeypatch.setenv("CLIPROXY_API_KEY", "cliproxy-key")
    monkeypatch.setenv("CLIPROXY_MANAGEMENT_KEY", "management-key")
    monkeypatch.setenv("CLIPROXY_URL", "http://cliproxy")
    monkeypatch.setenv("MODAL_TOKEN_ID", "modal-id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "modal-secret")
    monkeypatch.setenv("RUNPOD_API_KEY", "runpod-key")
    monkeypatch.setenv("SALAD_API_KEY", "salad-key")
    monkeypatch.setenv("VAST_API_KEY", "vast-key")
    monkeypatch.setenv("WORKER_API_KEY", "worker-key")

    loaded = ControlFile.load(ROOT / "config/control.yaml")

    assert [provider.id for provider in loaded.providers] == [
        "cliproxyapi",
        "modal-serverless",
        "runpod-pod",
        "runpod-serverless",
        "salad-serverless",
        "vast-pod",
        "vast-serverless",
    ]
    assert all(
        provider.base_url is None
        for provider in loaded.providers
        if provider.management
    )


@pytest.mark.asyncio
async def test_controller_discovers_managed_runpod_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "provider-key")
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models: []
providers:
  - id: runpod-pod
    api_key: worker-key
    management:
      kind: runpod-pod
      name: comfy-control
""".lstrip()
    )
    app = create_app(configured)
    management = FastAPI()

    @management.get("/v1/pods")
    async def pods(request: Request) -> list[dict[str, str]]:
        assert request.headers["authorization"] == "Bearer provider-key"
        return [{"id": "pod-123", "name": "comfy-control"}]

    await app.state.controller.lifecycle_client.aclose()
    app.state.controller.lifecycle_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=management), base_url="https://rest.runpod.io"
    )
    runtime = app.state.controller.providers["runpod-pod"]

    await app.state.controller.refresh_endpoint(runtime)

    assert runtime.base_url == "https://pod-123-8000.proxy.runpod.net"
    assert app.state.controller.resource_id("runpod-pod") == "pod-123"
    assert sorted(app.state.controller.available_actions("runpod-pod")) == [
        "start",
        "stop",
    ]
    await app.state.controller.close()


def test_usage_normalisers():
    assert normalise_usage("runpod", [{"amount": 1.25}, {"amount": 2}])[0] == {
        "label": "Reported spend",
        "unit": "USD",
        "value": 3.25,
    }
    assert normalise_usage("vast", {"credit": 25}) == [
        {"label": "Credit", "unit": "USD", "value": 25}
    ]
    assert normalise_usage(
        "salad",
        {
            "container_groups_quotas": {
                "container_replicas_quota": 10,
                "container_replicas_used": 2,
            }
        },
    ) == [
        {"label": "Replicas used", "value": 2},
        {"label": "Replica quota", "value": 10},
    ]
    assert normalise_xai_quota(
        {
            "config": {
                "creditUsagePercent": 25,
                "monthlyLimit": {"val": 5000},
                "onDemandCap": {"val": 2000},
                "onDemandUsed": {"val": 500},
                "productUsage": [{"product": "Grok Imagine", "usagePercent": 40}],
                "used": {"val": 1250},
            }
        },
        1,
    ) == [
        {"label": "Grok account 1 weekly remaining", "unit": "%", "value": 75},
        {
            "label": "Grok account 1 Grok Imagine remaining",
            "unit": "%",
            "value": 60,
        },
        {
            "label": "Grok account 1 monthly included remaining",
            "unit": "USD",
            "value": 37.5,
        },
        {
            "label": "Grok account 1 on-demand remaining",
            "unit": "USD",
            "value": 15.0,
        },
    ]


def test_history_parameters_omit_embedded_media_and_secrets():
    assert history_parameters(
        {
            "api_key": "secret",
            "image": "data:image/png;base64,aW1hZ2U=",
            "prompt": "test",
        }
    ) == {
        "api_key": "***",
        "image": "<embedded media omitted: 30 characters>",
        "prompt": "test",
    }
