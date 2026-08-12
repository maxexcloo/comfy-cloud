from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from .catalogue import Catalogue, WorkflowModel
from .cliproxy import CliproxyClient
from .comfy import ComfyClient, OutputRef
from .config import Settings
from .jobs import JobStore


class GenerationQueueFull(RuntimeError):
    pass


@dataclass
class VideoJob:
    id: str
    model: str
    status: str = "queued"
    created_at: int = field(default_factory=lambda: int(time.time()))
    error: str | None = None
    lease_expires_at: int | None = None
    output: OutputRef | None = None
    output_url: str | None = None

    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "status": self.status,
            "created_at": self.created_at,
            "error": self.error,
            "lease_expires_at": self.lease_expires_at,
            "output": {
                "filename": self.output.filename,
                "subfolder": self.output.subfolder,
                "type": self.output.type,
                "media_type": self.output.media_type,
            }
            if self.output is not None
            else None,
            "output_url": self.output_url,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> VideoJob:
        output_data = record.get("output")
        output = (
            OutputRef(
                filename=output_data["filename"],
                subfolder=output_data.get("subfolder", ""),
                type=output_data.get("type", "output"),
                media_type=output_data.get("media_type", "video/mp4"),
            )
            if output_data
            else None
        )
        return cls(
            id=record["id"],
            model=record["model"],
            status=record.get("status", "queued"),
            created_at=record.get("created_at", 0),
            error=record.get("error"),
            lease_expires_at=record.get("lease_expires_at"),
            output=output,
            output_url=record.get("output_url"),
        )


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.catalogue = Catalogue.load(settings.catalogue_dirs)
        self.cliproxy = (
            CliproxyClient(
                settings.cliproxy_url,
                settings.cliproxy_api_key,
                settings.workflow_timeout,
            )
            if settings.cliproxy_url and settings.cliproxy_api_key
            else None
        )
        self.comfy = ComfyClient(settings.comfy_url, settings.request_timeout)
        self.jobs: dict[str, VideoJob] = {}
        self.job_store = JobStore(settings.jobs_dir)
        self.inference_lock = asyncio.Lock()
        self.admission_lock = asyncio.Lock()
        self.pending_generations = 0
        self.background_tasks: set[asyncio.Task[None]] = set()
        self.execution_outputs: dict[str, list[OutputRef]] = {}
        self.execution_tasks: dict[str, asyncio.Task[list[OutputRef]]] = {}
        self.metric_registry = CollectorRegistry()
        self.generation_duration = Histogram(
            "comfy_control_generation_seconds",
            "Generation duration in seconds.",
            registry=self.metric_registry,
        )
        self.generations = Counter(
            "comfy_control_generations",
            "Completed image generations.",
            registry=self.metric_registry,
        )
        self.pending = Gauge(
            "comfy_control_pending_generations",
            "Admitted generation requests, including the active request.",
            registry=self.metric_registry,
        )
        self.requests = Counter(
            "comfy_control_requests",
            "HTTP requests served.",
            registry=self.metric_registry,
        )
        self.requests_by_status = Counter(
            "comfy_control_requests_by_status",
            "HTTP requests by response status.",
            ("status",),
            registry=self.metric_registry,
        )
        self.video_jobs = Gauge(
            "comfy_control_video_jobs",
            "Video jobs by terminal state.",
            ("state",),
            registry=self.metric_registry,
        )
        self.video_jobs_created = Counter(
            "comfy_control_video_jobs_created",
            "Video jobs created.",
            registry=self.metric_registry,
        )
        self.video_jobs.labels("completed").set(0)
        self.video_jobs.labels("failed").set(0)
        for record in self.job_store.load():
            try:
                job = VideoJob.from_record(record)
            except (KeyError, TypeError, ValueError):
                continue
            if job.status in {"queued", "in_progress"} and (
                job.lease_expires_at is None or job.lease_expires_at <= time.time()
            ):
                job.status = "failed"
                job.error = "worker restarted before the job completed"
                job.lease_expires_at = None
                self.job_store.save(job.record())
            self.jobs[job.id] = job

    async def close(self) -> None:
        await self.comfy.close()
        if self.cliproxy is not None:
            await self.cliproxy.close()

    async def ready(self) -> bool:
        if await self.comfy.ready():
            return True
        return self.cliproxy is not None and await self.cliproxy.ready()

    async def run(
        self, model: WorkflowModel, values: dict[str, Any]
    ) -> list[OutputRef]:
        graph = model.render(values)
        async with self.inference_lock:
            prompt_id = await self.comfy.submit(graph)
            try:
                return await self.comfy.wait(
                    prompt_id, model.output.node, self.settings.workflow_timeout
                )
            except (TimeoutError, asyncio.CancelledError):
                await self.comfy.cancel(prompt_id)
                raise

    async def reserve_generation(self) -> None:
        async with self.admission_lock:
            if self.pending_generations >= self.settings.maximum_pending_generations:
                raise GenerationQueueFull("generation queue is full")
            self.pending_generations += 1
            self.pending.set(self.pending_generations)

    async def release_generation(self) -> None:
        async with self.admission_lock:
            self.pending_generations -= 1
            self.pending.set(self.pending_generations)

    def store_job(self, job: VideoJob) -> None:
        self.job_store.save(job.record())

    def get_job(self, job_id: str) -> VideoJob | None:
        record = self.job_store.get(job_id)
        if record is not None:
            try:
                self.jobs[job_id] = VideoJob.from_record(record)
            except (KeyError, TypeError, ValueError):
                pass
        job = self.jobs.get(job_id)
        if (
            job is not None
            and job.status in {"queued", "in_progress"}
            and job.lease_expires_at is not None
            and job.lease_expires_at <= time.time()
        ):
            job.status = "failed"
            job.error = "worker lease expired before the job completed"
            job.lease_expires_at = None
            self.store_job(job)
        return job

    def start_background_task(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    async def object_info(self) -> dict[str, Any] | None:
        try:
            return await self.comfy.object_info()
        except (httpx.HTTPError, RuntimeError):
            return None

    async def model(self, model_id: str) -> WorkflowModel:
        object_info = await self.object_info()
        if object_info is None:
            raise KeyError("ComfyUI node information is unavailable")
        return self.catalogue.get_available(
            model_id, self.settings.models_dir, object_info
        )

    def available_models(
        self, object_info: dict[str, Any] | None
    ) -> list[WorkflowModel]:
        if object_info is None:
            return []
        return self.catalogue.list_available(self.settings.models_dir, object_info)
