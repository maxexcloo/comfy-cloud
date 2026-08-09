from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from comfy_cloud.app import VideoJob, create_app
from comfy_cloud.comfy import OutputRef
from comfy_cloud.config import Settings
from comfy_cloud.jobs import JobStore
from comfy_cloud.storage import ObjectStorage

ROOT = Path(__file__).parents[1]


def settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "api_key": "test-key",
        "catalogue_dirs": (ROOT / "catalogue",),
        "comfy_url": "http://comfy.internal",
        "deployment_type": "serverless",
        "models_dir": ROOT / "models",
        "public_base_url": "http://test",
        "request_timeout": 1,
        "ui_password": "ui-key",
        "ui_username": "comfy",
        "workflow_timeout": 1,
        "jobs_dir": tmp_path / "jobs",
    }
    values.update(overrides)
    return Settings(**values)


def test_object_storage_from_env_requires_bucket():
    assert ObjectStorage.from_env({}) is None
    assert ObjectStorage.from_env({"S3_BUCKET": "b"}) is None  # boto3 not installed


def test_job_store_roundtrips_and_overwrites(tmp_path):
    store = JobStore(tmp_path / "jobs")
    first = VideoJob(id="video_1", model="minimax-h3", status="queued")
    store.save(first.record())
    second = VideoJob(
        id="video_1",
        model="minimax-h3",
        status="completed",
        output=OutputRef("result.mp4", media_type="video/mp4"),
        output_url="https://bucket/outputs/result.mp4",
    )
    store.save(second.record())

    loaded = store.load()
    assert len(loaded) == 1
    restored = VideoJob.from_record(loaded[0])
    assert restored.status == "completed"
    assert restored.output is not None
    assert restored.output.filename == "result.mp4"
    assert restored.output_url == "https://bucket/outputs/result.mp4"


def test_runtime_loads_persisted_jobs_and_fails_stale(tmp_path):
    store = JobStore(tmp_path / "jobs")
    store.save(
        VideoJob(
            id="video_done",
            model="minimax-h3",
            status="completed",
            output=OutputRef("done.mp4"),
        ).record()
    )
    store.save(
        VideoJob(id="video_stale", model="minimax-h3", status="in_progress").record()
    )

    app = create_app(settings(tmp_path))
    try:
        done = app.state.runtime.jobs["video_done"]
        stale = app.state.runtime.jobs["video_stale"]
    finally:
        pass

    assert done.status == "completed"
    assert done.output is not None
    assert stale.status == "failed"
    assert "restarted" in stale.error


@pytest.mark.asyncio
async def test_completed_video_uploads_to_storage(tmp_path):
    app = create_app(settings(tmp_path))

    class FakeStorage:
        async def upload(self, filename, content, content_type, subfolder=""):
            return f"https://bucket/{subfolder}/{filename}"

    app.state.runtime.storage = FakeStorage()
    app.state.runtime.comfy.fetch_output = AsyncMock(
        return_value=httpx.Response(
            200, content=b"video-bytes", headers={"content-type": "video/mp4"}
        )
    )
    job = VideoJob(
        id="video_1",
        model="minimax-h3",
        status="completed",
        output=OutputRef("result.mp4", media_type="video/mp4"),
    )
    app.state.runtime.jobs[job.id] = job
    await app.state.runtime.upload_output(job)

    assert job.output_url == "https://bucket//result.mp4"


@pytest.mark.asyncio
async def test_video_content_redirects_to_storage_url(tmp_path):
    app = create_app(settings(tmp_path))
    job = VideoJob(
        id="video_1",
        model="minimax-h3",
        status="completed",
        output=OutputRef("result.mp4", media_type="video/mp4"),
        output_url="https://bucket/outputs/result.mp4",
    )
    app.state.runtime.jobs[job.id] = job
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        response = await client.get(
            "/v1/videos/video_1/content", headers={"Authorization": "Bearer test-key"}
        )

    assert response.status_code == 302
    assert response.headers["location"] == "https://bucket/outputs/result.mp4"


@pytest.mark.asyncio
async def test_video_content_streams_when_no_storage(tmp_path):
    app = create_app(settings(tmp_path))
    job = VideoJob(
        id="video_1",
        model="minimax-h3",
        status="completed",
        output=OutputRef("result.mp4", media_type="video/mp4"),
    )
    app.state.runtime.jobs[job.id] = job
    app.state.runtime.comfy.fetch_output = AsyncMock(
        return_value=httpx.Response(
            200, content=b"video-bytes", headers={"content-type": "video/mp4"}
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/v1/videos/video_1/content", headers={"Authorization": "Bearer test-key"}
        )

    assert response.status_code == 200
    assert response.content == b"video-bytes"


@pytest.mark.asyncio
async def test_image_url_response_uses_storage_when_configured(tmp_path):
    app = create_app(settings(tmp_path))

    class FakeStorage:
        async def upload(self, filename, content, content_type, subfolder=""):
            return f"https://bucket/images/{filename}"

    app.state.runtime.storage = FakeStorage()
    app.state.runtime.catalogue.get("example").required_files = []
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
                "response_format": "url",
            },
        )

    assert response.status_code == 200
    assert response.json()["data"] == [{"url": "https://bucket/images/result.png"}]
