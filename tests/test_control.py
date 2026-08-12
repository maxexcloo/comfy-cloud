import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from comfy_control.cliproxy import CliproxyClient
from comfy_control.control import create_app
from comfy_control.control_config import ControlFile, ControlSettings
from comfy_control.control_dashboard_routes import (
    provider_logs as dashboard_provider_logs,
)
from comfy_control.control_inference import normalise_grok_image_options
from comfy_control.control_preferences import ControlPreferences
from comfy_control.control_registry import control_file as registry_control_file
from comfy_control.control_store import ControlStore
from comfy_control.controller import history_parameters
from comfy_control.provider_telemetry import normalise_usage, normalise_xai_quota

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    ("prompt", "aspect_ratio", "resolution"),
    [
        ("Photorealistic portrait, 2K", "9:16", "2k"),
        ("1k landscape studio photograph", "16:9", "1k"),
        ("Square composition at 2K", "1:1", "2k"),
        ("Studio photograph, 3:4, 1K", "3:4", "1k"),
    ],
)
def test_grok_image_options_are_inferred_from_prompt_in_any_order(
    prompt: str, aspect_ratio: str, resolution: str
) -> None:
    result = normalise_grok_image_options(
        {"model": "grok-imagine-image-quality", "prompt": prompt}
    )

    assert result["aspect_ratio"] == aspect_ratio
    assert result["resolution"] == resolution


def test_grok_image_options_preserve_structured_values_and_default_to_1k() -> None:
    explicit = normalise_grok_image_options(
        {
            "aspect_ratio": "2:3",
            "model": "cliproxyapi/grok-imagine-image-quality",
            "prompt": "portrait 2K",
            "resolution": "1k",
        }
    )
    defaulted = normalise_grok_image_options(
        {"model": "grok-imagine-image-quality", "prompt": "A studio photograph"}
    )
    unrelated = {"model": "another-image-model", "prompt": "portrait 2K"}

    assert explicit["aspect_ratio"] == "2:3"
    assert explicit["resolution"] == "1k"
    assert defaulted["resolution"] == "1k"
    assert "aspect_ratio" not in defaulted
    assert normalise_grok_image_options(unrelated) is unrelated


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
    outputs: dict[str, tuple[bytes, str, str]] = {}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/internal/logs")
    async def logs() -> dict[str, object]:
        return {
            "entries": [
                {
                    "created_at": 1,
                    "level": "error",
                    "message": "ComfyUI worker traceback",
                    "source": "Worker",
                }
            ],
            "source": "Worker",
        }

    @app.post("/internal/executions")
    async def execute(request: Request) -> dict[str, object]:
        form = await request.form()
        spec = json.loads(str(form["spec"]))
        execution_id = str(spec["execution_id"])
        operation = spec["operation"]
        if operation == "image_generation":
            assert spec["model"] == "worker/image"
            output = (b"image", "image/png", "image.png")
        elif operation == "image_edit":
            assert spec["model"] == "worker/image-edit"
            images = form.getlist("image")
            assert [image.filename for image in images] == ["mara.png", "elise.png"]
            assert [await image.read() for image in images] == [b"mara", b"elise"]
            output = (b"edit", "image/png", "edit.png")
        else:
            assert spec["model"] == "worker/video"
            images = form.getlist("image")
            if images:
                assert await images[0].read() == b"image"
            output = (b"video", "video/mp4", "video.mp4")
        outputs[execution_id] = output
        return {
            "execution_id": execution_id,
            "outputs": [
                {
                    "content_type": output[1],
                    "filename": output[2],
                    "index": 0,
                    "url": f"http://worker/internal/executions/{execution_id}/outputs/0",
                }
            ],
            "status": "completed",
        }

    @app.get("/internal/executions/{execution_id}/outputs/0")
    async def output(execution_id: str) -> Response:
        content, content_type, _ = outputs[execution_id]
        return Response(content, media_type=content_type)

    return app


async def attach_worker(app: FastAPI) -> None:
    runtime = app.state.controller.providers["worker"]
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=worker_app()), base_url="http://worker"
    )


@pytest.mark.asyncio
async def test_controller_prefers_internal_execution_contract(tmp_path: Path):
    provider = FastAPI()

    @provider.get("/health/ready")
    async def ready() -> dict[str, str]:
        return {"status": "ready"}

    @provider.post("/internal/executions")
    async def execute(request: Request) -> dict[str, object]:
        form = await request.form()
        spec = json.loads(str(form["spec"]))
        assert spec["model"] == "worker/image"
        assert spec["operation"] == "image_generation"
        assert spec["parameters"]["prompt"] == "internal contract"
        return {
            "execution_id": spec["execution_id"],
            "outputs": [
                {
                    "content_type": "image/png",
                    "filename": "internal.png",
                    "index": 0,
                    "url": "http://worker/internal/executions/output",
                }
            ],
            "status": "completed",
        }

    @provider.get("/internal/executions/output")
    async def output() -> Response:
        return Response(b"internal-image", media_type="image/png")

    app = create_app(settings(tmp_path))
    runtime = app.state.controller.providers["worker"]
    runtime.base_url = "http://worker/"
    await runtime.client.aclose()
    runtime.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=provider), base_url="http://worker"
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://control"
    ) as client:
        response = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "public/image", "prompt": "internal contract"},
        )

    assert response.status_code == 200
    assert response.json()["data"] == [{"b64_json": "aW50ZXJuYWwtaW1hZ2U="}]
    assert (
        app.state.controller.store.histories()[0]["media"][0]["filename"]
        == "internal.png"
    )
    assert (
        app.state.controller.store.histories()[0]["attempts"][0]["status"]
        == "completed"
    )
    await app.state.controller.close()


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
        home = await client.get("/", follow_redirects=False)
        dashboard = await client.get("/providers")
        models = await client.get(
            "/v1/models", headers={"Authorization": "Bearer control-key"}
        )
        image = await client.post(
            "/v1/images/generations",
            headers={"Authorization": "Bearer control-key"},
            json={"model": "worker/worker/image", "prompt": "test"},
        )
        status = await client.get("/ops/status")
        history = status.json()["history"][0]
        media = await client.get(
            f"/ops/history/{history['id']}/media/{history['media'][0]['id']}"
        )
        logout = await client.post("/logout", follow_redirects=False)
        denied_media = await client.get(
            f"/ops/history/{history['id']}/media/{history['media'][0]['id']}"
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
    assert home.status_code == 303
    assert home.headers["location"] == "/media"
    assert dashboard.status_code == 200
    assert "<table>" in dashboard.text
    assert 'href="/history"' in dashboard.text
    assert 'href="/settings"' in dashboard.text
    assert 'href="/media"' in dashboard.text
    assert 'action="/logout"' in dashboard.text
    assert "setInterval" not in dashboard.text
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
    assert history["model"] == "worker/worker/image"
    assert history["provider_model"] == "worker/image"
    assert history["parameters"] == {
        "model": "worker/worker/image",
        "prompt": "test",
    }
    assert history["attempts"][0]["model"] == "worker/image"
    assert history["status"] == "completed"
    assert status.json()["history_pagination"] == {
        "count": 1,
        "page": 1,
        "pages": 1,
    }
    assert media.content == b"image"
    assert media.headers["content-type"] == "image/png"
    assert logout.status_code == 303
    assert logout.headers["location"] == "/login"
    assert denied_media.status_code == 401
    assert app.state.controller.store.history_usage("worker") == {
        "failed_requests": 0,
        "successful_requests": 1,
        "total_requests": 1,
    }
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_settings_are_admin_only_versioned_and_encrypted(tmp_path):
    app = create_app(settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        denied = await client.get(
            "/ops/settings", headers={"Authorization": "Bearer control-key"}
        )
        await sign_in(client)
        initial = await client.get("/ops/settings")
        revision = initial.json()["revision"]
        missing_confirmation = await client.patch(
            "/ops/settings", json={"revision": revision, "values": {}}
        )
        updated = await client.patch(
            "/ops/settings",
            headers={"x-comfy-control-settings": "update"},
            json={
                "revision": revision,
                "values": {
                    "hf_token": "hf-secret-value",
                    "worker_image": "registry.example/comfy-control:worker",
                },
            },
        )
        invalid = await client.patch(
            "/ops/settings",
            headers={"x-comfy-control-settings": "update"},
            json={
                "revision": updated.json()["revision"],
                "values": {
                    "modal_token_id": "must-not-leak",
                    "modal_token_secret": None,
                },
            },
        )
        stale = await client.patch(
            "/ops/settings",
            headers={"x-comfy-control-settings": "update"},
            json={"revision": revision, "values": {"worker_image": "stale"}},
        )

    fields = {field["name"]: field for field in updated.json()["fields"]}
    stored_secret = app.state.controller.store.connection.execute(
        "SELECT encrypted_value FROM control_secrets WHERE name = 'hf_token'"
    ).fetchone()[0]
    assert denied.status_code == 401
    assert missing_confirmation.status_code == 400
    assert updated.status_code == 200
    assert fields["hf_token"]["configured"] is True
    assert fields["hf_token"]["value"] is None
    assert fields["worker_image"]["value"] == "registry.example/comfy-control:worker"
    assert "hf-secret-value" not in stored_secret
    assert invalid.status_code == 400
    assert "must-not-leak" not in invalid.text
    assert stale.status_code == 409
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_environment_overrides_database_and_locks_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "environment-secret")
    monkeypatch.setenv("WORKER_IMAGE", "registry.example/environment:worker")
    configured = settings(tmp_path)
    app = create_app(configured)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        await sign_in(client)
        initial = await client.get("/ops/settings")
        fields = {field["name"]: field for field in initial.json()["fields"]}
        rejected = await client.patch(
            "/ops/settings",
            headers={"x-comfy-control-settings": "update"},
            json={
                "revision": initial.json()["revision"],
                "values": {"hf_token": "database-secret"},
            },
        )
        updated = await client.patch(
            "/ops/settings",
            headers={"x-comfy-control-settings": "update"},
            json={
                "revision": initial.json()["revision"],
                "values": {"public_base_url": "https://control.example"},
            },
        )
        page = await client.get("/settings")

    assert fields["hf_token"]["configured"] is True
    assert fields["hf_token"]["locked"] is True
    assert fields["worker_image"]["locked"] is True
    assert fields["worker_image"]["value"] == "registry.example/environment:worker"
    assert rejected.status_code == 400
    assert "environment-controlled" in rejected.text
    assert updated.status_code == 200
    assert "Worker Image" in page.text
    assert "Controlled By Environment" in page.text
    assert "disabled" in page.text
    assert (
        app.state.controller.store.connection.execute(
            "SELECT encrypted_value FROM control_secrets WHERE name = 'hf_token'"
        ).fetchone()
        is None
    )
    await app.state.controller.close()

    monkeypatch.setenv("HF_TOKEN", "replacement-secret")
    monkeypatch.setenv("WORKER_IMAGE", "registry.example/replacement:worker")
    replacement = create_app(configured)
    assert replacement.state.controller.preferences.hf_token == "replacement-secret"
    assert replacement.state.controller.preferences.worker_image == (
        "registry.example/replacement:worker"
    )
    await replacement.state.controller.close()

    monkeypatch.delenv("HF_TOKEN")
    monkeypatch.delenv("WORKER_IMAGE")
    restored = create_app(configured)
    assert restored.state.controller.preferences.hf_token == ""
    assert restored.state.controller.preferences.worker_image == (
        "ghcr.io/maxexcloo/comfy-control:worker"
    )
    assert restored.state.controller.preferences.public_base_url == (
        "https://control.example"
    )
    await restored.state.controller.close()


def test_packaged_revision_locks_matching_worker_image(monkeypatch):
    monkeypatch.delenv("WORKER_IMAGE", raising=False)
    monkeypatch.setenv(
        "COMFY_CONTROL_REVISION", "cd208c90d415720d783711fa6cbc7b14d1d71c05"
    )

    configured = ControlPreferences.from_environment()
    overrides = ControlPreferences.environment_overrides()

    assert configured.worker_image == (
        "ghcr.io/maxexcloo/comfy-control:sha-cd208c9-worker"
    )
    assert overrides["worker_image"] == configured.worker_image


@pytest.mark.asyncio
async def test_server_rendered_settings_require_csrf(tmp_path):
    app = create_app(settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        await sign_in(client)
        page = await client.get("/settings")
        rejected = await client.post(
            "/settings", data={"csrf_token": "invalid", "revision": "1"}
        )

    assert page.status_code == 200
    assert 'action="/settings"' in page.text
    assert 'name="csrf_token"' in page.text
    assert rejected.status_code == 403
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_resolves_shared_explicit_video_route(tmp_path):
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models:
  - id: public/image-to-video
    operation: video_generation
    targets:
      - { model: upstream/video, provider: worker }
  - id: public/text-to-video
    operation: video_generation
    targets:
      - { model: upstream/video, provider: worker }
providers:
  - id: worker
    api_key: worker-key
    base_url: http://worker
""".lstrip()
    )
    app = create_app(configured)

    model, targets = app.state.controller.resolve_model(
        "worker/upstream/video", "video_generation"
    )

    assert model.id == "public/image-to-video"
    assert [(target.model, target.provider) for target in targets] == [
        ("upstream/video", "worker")
    ]
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
            "/ops/providers/worker/test",
            json={
                "model": "public/image",
                "prompt": "test from the dashboard",
                "size": "512x512",
            },
        )
        logs = await client.get("/ops/providers/worker/logs")
        media = await client.get(
            "/ops/history/"
            f"{tested.json()['history_id']}/media/{tested.json()['media'][0]['id']}"
        )

    assert tested.status_code == 200
    assert tested.json()["provider"] == "worker"
    assert tested.json()["status"] == "completed"
    assert logs.status_code == 200
    assert logs.json()["source"] == "Controller and Worker"
    assert logs.json()["entries"][0]["message"] == "dashboard test completed"
    assert any(
        entry["message"] == "ComfyUI worker traceback" and entry["source"] == "Worker"
        for entry in logs.json()["entries"]
    )
    assert media.content == b"image"
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_rewrites_worker_image_url_to_archived_media(tmp_path):
    provider_app = FastAPI()

    @provider_app.get("/health/ready")
    async def provider_ready() -> dict[str, str]:
        return {"status": "ready"}

    @provider_app.post("/internal/executions")
    async def provider_generation(request: Request) -> dict[str, object]:
        spec = json.loads(str((await request.form())["spec"]))
        return {
            "execution_id": spec["execution_id"],
            "outputs": [
                {
                    "content_type": "image/png",
                    "filename": "generated.png",
                    "index": 0,
                    "url": "http://worker/generated.png",
                }
            ],
            "status": "completed",
        }

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
            json={
                "model": "public/image",
                "prompt": "test",
                "response_format": "url",
            },
        )
        archived_url = generated.json()["data"][0]["url"]
        archived = await client.get(
            archived_url, headers={"Authorization": "Bearer control-key"}
        )

    assert archived_url.startswith("http://control/ops/history/")
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
    assert content.status_code == 200
    assert content.content == b"video"
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_polls_cliproxy_video_instead_of_worker_wait_endpoint(
    tmp_path, monkeypatch
):
    control_settings = settings(tmp_path)
    config = control_settings.config_file
    config.write_text(
        """
models:
  - id: public/video
    operation: video_generation
    targets:
      - model: grok-imagine-video-1.5
        provider: cliproxyapi
providers:
  - id: cliproxyapi
    api_key: proxy-key
    base_url: http://cliproxy
    idle_seconds: 0
    type: proxy
""".lstrip()
    )
    received: list[dict[str, object]] = []

    async def generate_video(self: CliproxyClient, body: dict[str, object]) -> str:
        received.append(body)
        return "https://videos.example/grok.mp4"

    monkeypatch.setattr(CliproxyClient, "generate_video", generate_video)
    app = create_app(control_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        submitted = await client.post(
            "/v1/videos",
            headers={"Authorization": "Bearer control-key"},
            json={
                "aspect_ratio": "16:9",
                "model": "public/video",
                "prompt": "test",
                "provider": "cliproxyapi",
                "resolution": "480p",
                "seconds": 6,
            },
        )
        assert submitted.status_code == 200, submitted.text
        job_id = submitted.json()["id"]
        for _ in range(20):
            status = await client.get(
                f"/v1/videos/{job_id}",
                headers={"Authorization": "Bearer control-key"},
            )
            if status.json()["status"] == "completed":
                break
            await asyncio.sleep(0)

    assert received == [
        {
            "aspect_ratio": "16:9",
            "model": "public/video",
            "prompt": "test",
            "resolution": "480p",
            "seconds": 6,
        }
    ]
    assert status.json()["model"] == "public/video"
    assert status.json()["output_url"] == "https://videos.example/grok.mp4"
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
        history = await client.get("/ops/history")
        item = history.json()["data"][0]
        media = await client.get(
            f"/ops/history/{history_id}/media/{item['media'][0]['id']}"
        )

    assert item["id"] == history_id
    assert item["parameters"]["prompt"] == "persistent"
    assert media.content == b"image"
    await second.state.controller.close()


@pytest.mark.asyncio
async def test_dashboard_paginates_persisted_history_and_events(tmp_path):
    app = create_app(settings(tmp_path))
    for index in range(21):
        app.state.controller.store.save_history(
            f"history-{index}",
            "image_generation",
            "public/image",
            '{"model":"public/image"}',
        )
        app.state.controller.store.event("info", f"event {index}", provider="worker")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        response = await client.get(
            "/ops/status?event_page=2&history_page=2",
            headers={"Authorization": "Bearer control-key"},
        )

    assert response.status_code == 200
    assert len(response.json()["events"]) == 1
    assert len(response.json()["history"]) == 1
    assert response.json()["event_pagination"] == {
        "count": 21,
        "page": 2,
        "pages": 2,
    }
    assert response.json()["history_pagination"] == {
        "count": 21,
        "page": 2,
        "pages": 2,
    }
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_dashboard_pages_filter_link_and_stream_current_data(tmp_path):
    app = create_app(settings(tmp_path))
    store = app.state.controller.store
    for index in range(30):
        store.save_history(
            f"other-{index}",
            "video_generation",
            "public/video",
            json.dumps({"prompt": f"Unrelated Prompt {index}"}),
        )
        store.event("info", f"Unrelated Event {index}", provider="other")
    history_id = "image-wombat"
    store.save_history(
        history_id,
        "image_generation",
        "public/image",
        '{"prompt":"Wombat Needle Portrait","seed":42}',
    )
    store.update_history(history_id, "completed", provider="worker")
    output = tmp_path / "wombat.png"
    output.write_bytes(b"image")
    store.save_media(
        history_id,
        "image/png",
        "wombat.png",
        output,
        output.stat().st_size,
    )
    store.event(
        "warning",
        "Wombat Needle Event",
        provider="worker",
        request_id=history_id,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        await sign_in(client)
        providers = await client.get("/providers")
        events = await client.get(
            "/events",
            params={"level": "warning", "provider": "worker", "q": "wombat needle"},
        )
        history = await client.get(
            "/history",
            params={
                "operation": "image_generation",
                "provider": "worker",
                "q": "wombat needle",
                "status": "completed",
            },
        )
        media = await client.get(
            "/media", params={"filter": f"history_id|equals|{history_id}"}
        )
        detail = await client.get(
            f"/media/{store.media_library()['data'][0]['asset_id']}"
        )
        stylesheet = await client.get("/assets/dashboard.css")
        javascript = await client.get("/assets/dashboard.js")

        cookie = "; ".join(f"{name}={value}" for name, value in client.cookies.items())
        request = Request(
            {
                "app": app,
                "headers": [(b"cookie", cookie.encode())],
                "method": "GET",
                "path": "/providers/worker/logs",
                "query_string": b"",
                "scheme": "http",
                "server": ("control", 80),
                "type": "http",
            }
        )
        log_stream = await dashboard_provider_logs("worker", request)
        first_log_update = await anext(log_stream.body_iterator)
        await log_stream.body_iterator.aclose()

    assert providers.status_code == 200
    assert (
        providers.text.index('href="/media"')
        < providers.text.index('href="/providers"')
        < providers.text.index('href="/events"')
        < providers.text.index('href="/history"')
        < providers.text.index('href="/settings"')
    )
    assert 'aria-current="page">Providers' in providers.text
    assert 'class="skip-link"' in providers.text
    assert 'href="/assets/dashboard.css?current"' in providers.text
    assert 'src="/assets/dashboard.js?current"' in providers.text
    assert 'data-log-url="/providers/worker/logs"' in providers.text
    assert events.text.count("Wombat Needle Event") == 1
    assert "Unrelated Event" not in events.text
    assert f"/history?q={history_id}" in events.text
    assert history.text.count(history_id) >= 1
    assert "Unrelated Prompt" not in history.text
    assert 'data-media-id="1"' in history.text
    assert 'id="media-dialog"' in media.text
    assert 'aria-labelledby="media-title"' in media.text
    assert 'data-media-id="' in media.text
    assert detail.status_code == 200
    assert detail.json()["uses"][0]["prompt"] == "Wombat Needle Portrait"
    assert "path" not in detail.json()
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert stylesheet.headers["cache-control"] == "no-store"
    assert "@media (max-width: 760px)" in stylesheet.text
    assert "body > nav" in stylesheet.text
    assert javascript.headers["content-type"].startswith("text/javascript")
    assert javascript.headers["cache-control"] == "no-store"
    assert "if (!mediaDialog.open) mediaDialog.showModal()" in javascript.text
    assert 'labelledValue("Generation Time"' in javascript.text
    assert '["KiB", "MiB", "GiB"]' in javascript.text
    assert log_stream.media_type == "text/event-stream"
    assert str(first_log_update).startswith("data:")
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

        @provider_app.post("/internal/executions")
        async def image(request: Request) -> JSONResponse:
            calls[status_code] = calls.get(status_code, 0) + 1
            spec = json.loads(str((await request.form())["spec"]))
            return JSONResponse(
                {
                    "execution_id": spec["execution_id"],
                    "outputs": [
                        {
                            "content_type": "image/png",
                            "filename": "image.png",
                            "index": 0,
                            "url": f"http://fallback/output/{status_code}",
                        }
                    ],
                    "status": "completed",
                },
                status_code=status_code,
            )

        @provider_app.get("/output/{code}")
        async def output(code: int) -> Response:
            return Response(str(code).encode(), media_type="image/png")

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

    @provider_app.post("/internal/executions")
    async def image(request: Request) -> dict[str, object]:
        spec = json.loads(str((await request.form())["spec"]))
        return {
            "execution_id": spec["execution_id"],
            "outputs": [],
            "status": "completed",
        }

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
        status = await client.get("/ops/status", headers=bearer)
        rejected = await client.post(
            "/ops/providers/worker/actions/deploy", headers=bearer
        )
        deployed = await client.post(
            "/ops/providers/worker/actions/deploy",
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
    messages = [
        entry["message"]
        for entry in (await app.state.controller.provider_logs("worker"))["entries"]
    ]
    assert messages == [
        "provider action deploy succeeded",
        "provider action deploy started",
    ]
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
            "/ops/providers/worker/actions/deploy",
            headers={"x-comfy-control-action": "worker/deploy"},
        )
        status = await client.get("/ops/status")
        destroyed_response = await client.post(
            "/ops/providers/worker/actions/destroy",
            headers={"x-comfy-control-action": "worker/destroy"},
        )

    assert deployed.status_code == 200
    assert status.json()["providers"][0]["resource_id"] == "pod-123"
    assert destroyed_response.status_code == 200
    assert destroyed == ["pod-123"]
    assert app.state.controller.store.provider_resource("worker") is None
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_controller_clears_resource_when_provider_already_deleted(tmp_path):
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
      terminate:
        method: DELETE
        url: http://management/resources/{resource_id}
""".lstrip()
    )
    app = create_app(configured)
    management = FastAPI()

    @management.post("/deploy")
    async def deploy() -> dict[str, object]:
        return {"resource": {"id": "pod-123"}}

    @management.delete("/resources/{resource_id}")
    async def terminate(resource_id: str) -> Response:
        assert resource_id == "pod-123"
        return Response(status_code=404)

    await app.state.controller.lifecycle_client.aclose()
    app.state.controller.lifecycle_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=management), base_url="http://management"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://control"
    ) as client:
        await sign_in(client)
        await client.post(
            "/ops/providers/worker/actions/deploy",
            headers={"x-comfy-control-action": "worker/deploy"},
        )
        terminated = await client.post(
            "/ops/providers/worker/actions/terminate",
            headers={"x-comfy-control-action": "worker/terminate"},
        )

    assert terminated.status_code == 200
    assert terminated.json()["status"] == 404
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


def test_control_settings_use_packaged_registry_by_default(monkeypatch):
    monkeypatch.setenv("CONTROL_API_KEY", "secure-key")
    monkeypatch.setenv("CONTROL_SECRET_KEY", "secure-secret-key-at-least-32-characters")
    monkeypatch.setenv("CONTROL_UI_PASSWORD", "secure-password")
    monkeypatch.delenv("CONTROL_CONFIG", raising=False)

    assert ControlSettings.from_env().config_file is None


def test_compose_forwards_only_bootstrap_environment():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert set(compose["services"]) == {"comfy-control"}
    service = compose["services"]["comfy-control"]
    forwarded = set(service["environment"])
    env_example = {
        line.partition("=")[0]
        for line in (ROOT / ".env.example").read_text().splitlines()
        if line and not line.startswith("#")
    }

    assert service["env_file"] == [{"path": ".env", "required": False}]
    assert env_example <= forwarded
    assert env_example == forwarded - {"CONTROL_CONFIG", "CONTROL_DATABASE"}
    for name in env_example:
        assert str(
            compose["services"]["comfy-control"]["environment"][name]
        ).startswith(f"${{{name}")


def test_control_file_enables_configured_providers(monkeypatch):
    monkeypatch.setenv("CLIPROXY_API_KEY", "cliproxy-key")
    monkeypatch.setenv("CLIPROXY_MANAGEMENT_KEY", "management-key")
    monkeypatch.setenv("CLIPROXY_URL", "http://cliproxy")
    monkeypatch.setenv("RUNPOD_POD_ID", "pod-id")
    monkeypatch.setenv("RUNPOD_POD_URL", "https://pod.example")
    monkeypatch.setenv("RUNPOD_API_KEY", "provider-key")
    monkeypatch.setenv("WORKER_API_KEY", "worker-key")

    loaded = registry_control_file(os.environ)

    assert [model.id for model in loaded.models] == [
        "image-edit",
        "image-generation",
        "image-to-video",
        "text-to-video",
    ]
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
    monkeypatch.setenv("SALAD_ORGANISATION", "salad-organisation")
    monkeypatch.setenv("SALAD_PROJECT", "salad-project")
    monkeypatch.setenv("VAST_API_KEY", "vast-key")
    monkeypatch.setenv("WORKER_API_KEY", "worker-key")

    loaded = registry_control_file(os.environ)

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
    providers = {provider.id: provider for provider in loaded.providers}
    assert providers["modal-serverless"].actions["deploy"].internal == "modal-deploy"
    assert (
        providers["modal-serverless"].actions["terminate"].internal == "modal-terminate"
    )
    assert sorted(providers["runpod-serverless"].actions) == [
        "deploy",
        "scale-down",
        "scale-up",
        "terminate",
    ]
    assert sorted(providers["salad-serverless"].actions) == ["deploy", "terminate"]
    assert sorted(providers["vast-serverless"].actions) == ["deploy", "terminate"]


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


@pytest.mark.asyncio
async def test_missing_managed_resource_is_not_deployed(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "provider-key")
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models: []
providers:
  - id: runpod-pod
    actions:
      deploy:
        internal: provider-deploy
        resource_id_path: id
    api_key: worker-key
    management:
      kind: runpod-pod
      name: comfy-control
""".lstrip()
    )
    app = create_app(configured)
    await app.state.controller.lifecycle_client.aclose()
    app.state.controller.lifecycle_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))
    )
    runtime = app.state.controller.providers["runpod-pod"]

    status = await app.state.controller.provider_status(runtime)

    assert status["state"] == "not-deployed"
    assert status["status"] == "ok"
    assert sorted(app.state.controller.available_actions("runpod-pod")) == ["deploy"]
    await app.state.controller.close()


@pytest.mark.asyncio
async def test_request_auto_deploys_missing_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "provider-key")
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models: []
providers:
  - id: worker
    actions:
      deploy:
        internal: provider-deploy
        resource_id_path: id
    api_key: worker-key
    management:
      kind: runpod-pod
      name: comfy-control
""".lstrip()
    )
    app = create_app(configured)
    controller = app.state.controller
    runtime = controller.providers["worker"]
    controller.check_ready = AsyncMock(side_effect=[False, False, True])
    controller.action = AsyncMock(return_value=httpx.Response(201, json={"id": "pod"}))

    await controller.ensure_ready(runtime, "request-1")

    controller.action.assert_awaited_once()
    assert controller.action.await_args.args[2] == "deploy"
    assert controller.check_ready.await_count == 3
    await controller.close()


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
        "cliproxyapi",
        {"failed_requests": 1, "successful_requests": 2, "total_requests": 3},
    ) == [
        {"label": "Requests", "value": 3},
        {"label": "Successful", "value": 2},
        {"label": "Failed", "value": 1},
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


def test_media_library_fuzzy_search_filters_and_lineage(tmp_path: Path):
    store = ControlStore(tmp_path / "control.db")
    store.save_history(
        "image_1",
        "image_generation",
        "public/image",
        '{"prompt":"A wombat operating a GPU server","seed":42,"steps":8}',
    )
    source = tmp_path / "source.png"
    source.write_bytes(b"source-image")
    output = tmp_path / "output.png"
    output.write_bytes(b"output-image")
    source_id = store.save_input_media(
        "image_1",
        "image/png",
        "source.png",
        source,
        source.stat().st_size,
        field_name="image",
    )
    store.save_media(
        "image_1",
        "image/png",
        "output.png",
        output,
        output.stat().st_size,
    )

    result = store.media_library(
        query="wombt GPU",
        filters=[{"path": "seed", "operator": "equals", "value": 42}],
    )
    history_result = store.media_library(
        filters=[{"path": "history_id", "operator": "equals", "value": "image_1"}]
    )
    lineage = store.media_lineage(source_id)

    assert result["count"] == 1
    assert result["data"][0]["parameters"]["steps"] == 8
    assert history_result["count"] == 1
    assert [item["filename"] for item in lineage["derivatives"]] == ["output.png"]
    store.close()


def test_media_metadata_and_schema_migrations_are_persisted(tmp_path: Path):
    store = ControlStore(tmp_path / "control.db")
    store.save_history("image_1", "image_generation", "public/image", "{}")
    image = tmp_path / "dimensions.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\0" * 8
        + (640).to_bytes(4, "big")
        + (480).to_bytes(4, "big")
    )
    store.save_media(
        "image_1", "image/png", "dimensions.png", image, image.stat().st_size
    )
    item = store.media_library()["data"][0]
    versions = store.connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall()

    assert (item["width"], item["height"]) == (640, 480)
    assert [row["version"] for row in versions] == [1, 2]
    store.close()


def test_historical_routes_and_unique_input_lineage_are_reconciled(tmp_path: Path):
    configured = settings(tmp_path)
    first = create_app(configured)
    store = first.state.controller.store
    store.save_history(
        "source",
        "image_generation",
        "public/image",
        '{"model":"public/image","prompt":"source"}',
    )
    store.update_history(
        "source",
        "completed",
        provider="worker",
        provider_model="worker/image",
    )
    source = tmp_path / "source.png"
    source.write_bytes(b"unique-source")
    source_id = store.save_media(
        "source", "image/png", "source.png", source, source.stat().st_size
    )
    store.save_history(
        "edit",
        "image_edit",
        "public/image-edit",
        json.dumps(
            {
                "input_media": [
                    {
                        "content_type": "image/png",
                        "filename": "reference.png",
                        "size": source.stat().st_size,
                    }
                ],
                "model": "worker/worker/image-edit",
                "provider": "worker",
                "prompt": "edit",
            }
        ),
    )
    store.update_history("edit", "completed", provider="worker")
    output = tmp_path / "output.png"
    output.write_bytes(b"edited-output-with-a-different-size")
    output_id = store.save_media(
        "edit", "image/png", "output.png", output, output.stat().st_size
    )
    store.close()

    second = create_app(configured)
    history = next(
        item
        for item in second.state.controller.store.histories()
        if item["id"] == "edit"
    )

    assert history["model"] == "worker/worker/image-edit"
    assert history["provider_model"] == "worker/image-edit"
    assert history["attempts"][0]["model"] == "worker/image-edit"
    assert second.state.controller.store.media_lineage(output_id)["sources"] == [
        {
            "content_type": "image/png",
            "filename": "reference.png",
            "history_id": "edit",
            "id": source_id,
        }
    ]
    second.state.controller.store.close()


def test_historical_worker_model_maps_to_selected_fallback_provider(tmp_path: Path):
    configured = settings(tmp_path)
    configured.config_file.write_text(
        """
models:
  - id: public/video
    operation: video_generation
    targets:
      - { model: local/minimax, provider: local }
      - { model: proxy/grok, provider: proxy }
providers:
  - id: local
    api_key: local-key
    base_url: http://local
  - id: proxy
    api_key: proxy-key
    base_url: http://proxy
    type: proxy
""".lstrip()
    )
    first = create_app(configured)
    first.state.controller.store.save_history(
        "legacy-video",
        "video_generation",
        "local/minimax",
        '{"model":"local/minimax","provider":"proxy"}',
    )
    first.state.controller.store.update_history(
        "legacy-video", "completed", provider="proxy"
    )
    first.state.controller.store.close()

    second = create_app(configured)
    history = second.state.controller.store.histories()[0]
    _, direct_targets = second.state.controller.resolve_model(
        "proxy/grok", "video_generation", "proxy"
    )

    assert history["model"] == "local/minimax"
    assert history["provider"] == "proxy"
    assert history["provider_model"] == "proxy/grok"
    assert history["attempts"][0]["model"] == "proxy/grok"
    assert direct_targets[0].model == "proxy/grok"
    second.state.controller.store.close()


def test_controller_openapi_is_current_and_has_no_legacy_api(tmp_path: Path):
    app = create_app(settings(tmp_path))
    schema = app.openapi()

    assert schema["info"]["version"] == "current"
    assert "/ops/media" in schema["paths"]
    assert not any(path.startswith("/api/") for path in schema["paths"])
    assert schema["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    status_schema = schema["paths"]["/ops/status"]["get"]["responses"]["200"]
    settings_body = schema["paths"]["/ops/settings"]["patch"]["requestBody"]
    test_body = schema["paths"]["/ops/providers/{provider_id}/test"]["post"][
        "requestBody"
    ]
    image_create = schema["paths"]["/v1/images/generations"]["post"]
    image_edit = schema["paths"]["/v1/images/edits"]["post"]
    video_create = schema["paths"]["/v1/videos"]["post"]
    assert status_schema["content"]["application/json"]["schema"]["$ref"].endswith(
        "/OperationsStatus"
    )
    assert settings_body["content"]["application/json"]["schema"]["required"] == [
        "revision",
        "values",
    ]
    assert test_body["content"]["application/json"]["schema"]["required"] == [
        "model",
        "prompt",
    ]
    assert image_create["operationId"] == "create_image"
    assert image_create["requestBody"]["content"]["application/json"]["schema"][
        "required"
    ] == ["model", "prompt"]
    assert image_create["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/ImageGenerationResponse")
    assert image_edit["operationId"] == "edit_image"
    assert "multipart/form-data" in image_edit["requestBody"]["content"]
    assert video_create["operationId"] == "create_video"
    assert set(video_create["requestBody"]["content"]) == {
        "application/json",
        "multipart/form-data",
    }
    assert schema["paths"]["/v1/models"]["get"]["operationId"] == "list_models"
    assert schema["paths"]["/v1/videos/{job_id}"]["get"]["operationId"] == ("get_video")
    assert schema["paths"]["/ops/history"]["get"]["operationId"] == (
        "generation_history"
    )
    assert (
        schema["paths"]["/ops/history/{history_id}/media/{media_id}"]["get"][
            "operationId"
        ]
        == "generation_media"
    )
