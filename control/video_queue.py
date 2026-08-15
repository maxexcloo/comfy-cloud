from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from control.http import exception_message
from control.store import Job

if TYPE_CHECKING:
    from control.service import Controller


class VideoQueueFull(RuntimeError):
    pass


class VideoQueue:
    def __init__(self, controller: Controller, limit: int = 20):
        self.controller = controller
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=limit)
        self.task: asyncio.Task[None] | None = None

    def submit(self, job: Job) -> None:
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull as exc:
            raise VideoQueueFull("video queue is full") from exc
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def run(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self.controller.run_video(job)
            finally:
                self.queue.task_done()
            if self.queue.empty():
                completed = self.controller.store.job(job.id)
                if completed is not None and completed.provider:
                    await self.release(completed.provider, job.id)

    async def release(self, provider: str, request_id: str) -> None:
        runtime = self.controller.providers.get(provider)
        if runtime is None or runtime.active_requests:
            return
        actions = self.controller.available_actions(provider)
        action_name = "scale-down" if "scale-down" in actions else "stop"
        if action_name not in actions:
            return
        try:
            await self.controller.run_provider_action(
                provider, action_name, request_id.removeprefix("video_")[:16]
            )
            self.controller.store.event(
                "info",
                "video capacity released after queue drained",
                provider=provider,
                request_id=request_id,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort economy policy
            self.controller.store.event(
                "warning",
                f"video capacity release failed: {exception_message(exc)}",
                provider=provider,
                request_id=request_id,
            )
