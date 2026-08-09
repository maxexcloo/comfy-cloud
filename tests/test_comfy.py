import httpx
import pytest

from comfy_cloud.comfy import ComfyClient


@pytest.mark.asyncio
async def test_cancel_deletes_and_interrupts_running_prompt():
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/queue" and request.method == "GET":
            return httpx.Response(200, json={"queue_running": [[1, "prompt-1"]]})
        return httpx.Response(200)

    client = ComfyClient("http://comfy.internal")
    await client.http.aclose()
    client.http = httpx.AsyncClient(
        base_url="http://comfy.internal", transport=httpx.MockTransport(handler)
    )
    try:
        await client.cancel("prompt-1")
    finally:
        await client.close()

    assert requests == [("GET", "/queue"), ("POST", "/queue"), ("POST", "/interrupt")]
