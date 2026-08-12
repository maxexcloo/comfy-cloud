from __future__ import annotations

import asyncio
from typing import Any

import httpx
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from .catalogue import Catalogue, WorkflowModel
from .comfy import ComfyClient, OutputRef
from .config import Settings


class GenerationQueueFull(RuntimeError):
    pass


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.catalogue = Catalogue.load(settings.catalogue_dirs)
        self.comfy = ComfyClient(settings.comfy_url, settings.request_timeout)
        self.inference_lock = asyncio.Lock()
        self.admission_lock = asyncio.Lock()
        self.execution_lock = asyncio.Lock()
        self.pending_generations = 0
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
            "Completed generations.",
            registry=self.metric_registry,
        )
        self.pending = Gauge(
            "comfy_control_pending_generations",
            "Admitted executions, including the active execution.",
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

    async def close(self) -> None:
        for task in self.execution_tasks.values():
            task.cancel()
        if self.execution_tasks:
            await asyncio.gather(*self.execution_tasks.values(), return_exceptions=True)
        await self.comfy.close()

    async def ready(self) -> bool:
        return await self.comfy.ready()

    async def run(
        self, model: WorkflowModel, values: dict[str, Any]
    ) -> list[OutputRef]:
        graph = model.render(values)
        with self.generation_duration.time():
            async with self.inference_lock:
                prompt_id = await self.comfy.submit(graph)
                try:
                    outputs = await self.comfy.wait(
                        prompt_id, model.output.node, self.settings.workflow_timeout
                    )
                except (TimeoutError, asyncio.CancelledError):
                    await self.comfy.cancel(prompt_id)
                    raise
        self.generations.inc()
        return outputs

    async def start_execution(
        self,
        execution_id: str,
        model: WorkflowModel,
        values: dict[str, Any],
    ) -> tuple[asyncio.Task[list[OutputRef]], bool]:
        async with self.execution_lock:
            existing = self.execution_tasks.get(execution_id)
            if existing is not None:
                return existing, False
            await self.reserve_generation()
            task = asyncio.create_task(self.run(model, values))
            self.execution_tasks[execution_id] = task
            return task, True

    async def finish_execution(
        self,
        execution_id: str,
        task: asyncio.Task[list[OutputRef]],
        outputs: list[OutputRef] | None,
    ) -> None:
        async with self.execution_lock:
            if self.execution_tasks.get(execution_id) is task:
                self.execution_tasks.pop(execution_id, None)
                if outputs is not None:
                    self.execution_outputs[execution_id] = outputs
                await self.release_generation()

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
