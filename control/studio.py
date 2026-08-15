from __future__ import annotations

import asyncio
import json

import httpx
from fastapi import FastAPI

from control.store import GenerationRequest


class StudioRunner:
    def __init__(self, app: FastAPI):
        self.app = app
        self.tasks: set[asyncio.Task[None]] = set()

    def start(self) -> None:
        store = self.app.state.controller.store
        for request in store.pending_generation_requests():
            if request.history_id and store.history(request.history_id) is not None:
                store.update_generation_request(
                    request.id,
                    "failed",
                    error="generation was interrupted by a controller restart",
                )
            else:
                self._schedule(request)

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def submit(
        self,
        identifier: str,
        kind: str,
        model: str,
        payload: dict[str, object],
        history_id: str | None,
    ) -> GenerationRequest:
        store = self.app.state.controller.store
        store.save_generation_request(identifier, kind, model, payload, history_id)
        request = store.generation_request(identifier)
        assert request is not None
        self._schedule(request)
        return request

    def _schedule(self, request: GenerationRequest) -> None:
        task = asyncio.create_task(self._run(request))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run(self, request: GenerationRequest) -> None:
        store = self.app.state.controller.store
        settings = self.app.state.settings
        payload = json.loads(request.payload_json)
        source_asset_id = payload.pop("_source_asset_id", None)
        path = "/v1/images/generations" if request.kind == "image" else "/v1/videos"
        headers = {"Authorization": f"Bearer {settings.api_key}"}
        if request.history_id:
            headers["x-comfy-history-id"] = request.history_id
        store.update_generation_request(request.id, "submitting")
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://studio"
            ) as client:
                response = await client.post(path, json=payload, headers=headers)
            if not response.is_success:
                try:
                    message = response.json()["error"]["message"]
                except (KeyError, TypeError, ValueError):
                    message = response.text or f"HTTP {response.status_code}"
                store.update_generation_request(request.id, "failed", error=message)
                return
            history_id = request.history_id
            if request.kind == "video":
                history_id = str(response.json()["id"])
            store.update_generation_request(
                request.id, "submitted", history_id=history_id
            )
            if history_id and isinstance(source_asset_id, int):
                store.link_input_asset(history_id, source_asset_id, "studio_source")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - durable task boundary
            store.update_generation_request(request.id, "failed", error=str(exc))
