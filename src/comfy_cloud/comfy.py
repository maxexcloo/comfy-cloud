from __future__ import annotations

import asyncio
import mimetypes
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class OutputRef:
    filename: str
    subfolder: str = ""
    type: str = "output"
    media_type: str = "image/png"


class ComfyClient:
    def __init__(self, base_url: str, request_timeout: float = 60):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(base_url=self.base_url, timeout=request_timeout)
        self._object_info: dict[str, Any] | None = None

    async def close(self) -> None:
        await self.http.aclose()

    async def ready(self) -> bool:
        try:
            response = await self.http.get("/system_stats")
            return response.is_success
        except httpx.HTTPError:
            return False

    async def object_info(self) -> dict[str, Any]:
        if self._object_info is None:
            response = await self.http.get("/object_info")
            response.raise_for_status()
            self._object_info = response.json()
        return self._object_info

    async def submit(self, prompt: dict[str, Any]) -> str:
        response = await self.http.post("/prompt", json={"prompt": prompt})
        response.raise_for_status()
        data = response.json()
        if "prompt_id" not in data:
            raise RuntimeError(f"ComfyUI rejected workflow: {data}")
        return data["prompt_id"]

    async def upload(self, filename: str, content: bytes, content_type: str) -> str:
        response = await self.http.post(
            "/upload/image",
            files={"image": (filename, content, content_type)},
            data={"type": "input", "overwrite": "true"},
        )
        response.raise_for_status()
        return response.json()["name"]

    async def wait(
        self, prompt_id: str, output_node: str, timeout: float
    ) -> list[OutputRef]:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            response = await self.http.get(f"/history/{prompt_id}")
            response.raise_for_status()
            history = response.json().get(prompt_id)
            if history:
                status = history.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI execution failed: {status}")
                outputs = history.get("outputs", {}).get(output_node, {})
                refs = self._output_refs(outputs)
                if refs:
                    return refs
                if status.get("completed"):
                    raise RuntimeError(
                        f"workflow completed without output from node {output_node}"
                    )
            await asyncio.sleep(0.25)
        raise TimeoutError(f"workflow {prompt_id} exceeded {timeout} seconds")

    @staticmethod
    def _output_refs(outputs: dict[str, Any]) -> list[OutputRef]:
        refs: list[OutputRef] = []
        for key, default_media in (
            ("audio", "audio/wav"),
            ("gifs", "video/mp4"),
            ("images", "image/png"),
            ("videos", "video/mp4"),
        ):
            for item in outputs.get(key, []):
                media_type = (
                    item.get("format")
                    or mimetypes.guess_type(item["filename"])[0]
                    or default_media
                )
                refs.append(
                    OutputRef(
                        filename=item["filename"],
                        media_type=media_type,
                        subfolder=item.get("subfolder", ""),
                        type=item.get("type", "output"),
                    )
                )
        return refs

    async def fetch_output(self, ref: OutputRef) -> httpx.Response:
        response = await self.http.get(
            "/view",
            params={
                "filename": ref.filename,
                "subfolder": ref.subfolder,
                "type": ref.type,
            },
        )
        response.raise_for_status()
        return response
