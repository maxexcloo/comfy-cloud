from __future__ import annotations

import asyncio
from typing import Any

import httpx

IMAGE_MODEL = "grok-imagine-image-quality"
VIDEO_MODEL = "grok-imagine-video-1.5"


class CliproxyClient:
    def __init__(self, base_url: str, api_key: str, timeout: float):
        self.http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        self.timeout = timeout

    async def close(self) -> None:
        await self.http.aclose()

    async def ready(self) -> bool:
        try:
            response = await self.http.get("/v1/models")
            return response.is_success
        except httpx.HTTPError:
            return False

    async def generate_image(self, body: dict[str, Any]) -> httpx.Response:
        payload = {
            key: body[key]
            for key in ("n", "prompt", "response_format", "size")
            if body.get(key) is not None
        }
        payload["model"] = IMAGE_MODEL
        payload.setdefault("response_format", "b64_json")
        response = await self.http.post("/v1/images/generations", json=payload)
        response.raise_for_status()
        return response

    async def edit_image(
        self,
        fields: dict[str, str],
        filename: str,
        content: bytes,
        content_type: str,
    ) -> httpx.Response:
        response = await self.http.post(
            "/v1/images/edits",
            data=fields
            | {
                "model": IMAGE_MODEL,
                "response_format": fields.get("response_format", "b64_json"),
            },
            files={"image": (filename, content, content_type)},
        )
        response.raise_for_status()
        return response

    async def generate_video(self, body: dict[str, Any]) -> str:
        payload: dict[str, Any] = {
            "model": VIDEO_MODEL,
            "prompt": body["prompt"],
        }
        for parameter in ("aspect_ratio", "resolution"):
            if body.get(parameter) is not None:
                payload[parameter] = body[parameter]
        if body.get("seconds") is not None:
            payload["duration"] = body["seconds"]
        if body.get("image") is not None:
            payload["image"] = body["image"]
        response = await self.http.post("/v1/videos/generations", json=payload)
        response.raise_for_status()
        request_id = response.json().get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise RuntimeError("CLI Proxy API returned no video request ID")
        deadline = asyncio.get_running_loop().time() + self.timeout
        while asyncio.get_running_loop().time() < deadline:
            response = await self.http.get(f"/v1/videos/{request_id}")
            response.raise_for_status()
            result = response.json()
            status = result.get("status")
            if status in {"completed", "done"}:
                video = result.get("video") or {}
                output_url = (
                    video.get("url")
                    or result.get("video_url")
                    or result.get("output_url")
                )
                if isinstance(output_url, str) and output_url:
                    return output_url
                raise RuntimeError(
                    "CLI Proxy API completed video without an output URL"
                )
            if status in {"error", "failed"}:
                raise RuntimeError(
                    f"CLI Proxy API video generation failed: {result.get('error', status)}"
                )
            await asyncio.sleep(1)
        raise TimeoutError(
            f"CLI Proxy API video generation exceeded {self.timeout} seconds"
        )
