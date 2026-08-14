import json

import httpx
import pytest

from comfy_control.providers.cliproxy import CliproxyClient


@pytest.mark.asyncio
async def test_cliproxy_uses_fixed_media_models():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/images/generations":
            return httpx.Response(200, json={"data": [{"b64_json": "image"}]})
        if request.url.path == "/v1/images/edits":
            return httpx.Response(200, json={"data": [{"b64_json": "edit"}]})
        if request.url.path == "/v1/videos/generations":
            return httpx.Response(200, json={"request_id": "upstream-video"})
        return httpx.Response(
            200,
            json={
                "status": "done",
                "video_url": "https://videos.example/result.mp4",
            },
        )

    client = CliproxyClient("http://cliproxy", "proxy-key", 1)
    await client.http.aclose()
    client.http = httpx.AsyncClient(
        base_url="http://cliproxy", transport=httpx.MockTransport(handler)
    )
    try:
        await client.generate_image({"model": "local/image", "prompt": "draw"})
        await client.edit_image(
            {"prompt": "change"}, "source.png", b"image", "image/png"
        )
        output_url = await client.generate_video(
            {
                "aspect_ratio": "16:9",
                "model": "local/video",
                "prompt": "move",
                "resolution": "480p",
                "seconds": 5,
            }
        )
    finally:
        await client.close()

    image_body = json.loads(requests[0].content)
    assert image_body["model"] == "grok-imagine-image-quality"
    assert image_body["response_format"] == "b64_json"
    assert b"grok-imagine-image-quality" in requests[1].content
    assert b"b64_json" in requests[1].content
    video_body = json.loads(requests[2].content)
    assert video_body["model"] == "grok-imagine-video-1.5"
    assert video_body["aspect_ratio"] == "16:9"
    assert video_body["resolution"] == "480p"
    assert video_body["duration"] == 5
    assert output_url == "https://videos.example/result.mp4"
