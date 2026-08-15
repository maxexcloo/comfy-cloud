from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

API_KEY = os.environ.get("API_KEY", "")
BACKEND_BASE = "http://127.0.0.1:8000"
BACKEND_LOG = Path("/tmp/comfy-control-vast-backend.log")
BACKEND_STARTUP_TIMEOUT = 1800


def backend_headers() -> dict[str, str]:
    if not API_KEY:
        raise RuntimeError("API_KEY must be set")
    return {"Authorization": f"Bearer {API_KEY}"}


async def execute(
    *,
    files: list[dict[str, str]] | None = None,
    healthcheck: bool = False,
    spec: dict[str, object] | None = None,
) -> dict[str, object]:
    async with httpx.AsyncClient(
        base_url=BACKEND_BASE,
        headers=backend_headers(),
        timeout=None,
    ) as client:
        if healthcheck:
            response = await client.get("/health/ready")
            response.raise_for_status()
            return {"status": "ready"}
        if spec is None:
            raise ValueError("spec is required")
        uploads = []
        for item in files or []:
            uploads.append(
                (
                    item["field"],
                    (
                        item["filename"],
                        base64.b64decode(item["content"]),
                        item["content_type"],
                    ),
                )
            )
        response = await client.post(
            "/internal/executions",
            data={"spec": json.dumps(spec, separators=(",", ":"))},
            files=uploads,
        )
        response.raise_for_status()
        result = response.json()
        manifests = result.get("outputs") if isinstance(result, dict) else None
        if not isinstance(manifests, list):
            raise TypeError("worker returned an invalid execution manifest")
        outputs = []
        for manifest in manifests:
            if not isinstance(manifest, dict) or not manifest.get("url"):
                raise RuntimeError("worker returned an invalid output manifest")
            output = await client.get(str(manifest["url"]))
            output.raise_for_status()
            outputs.append(
                {
                    "content": base64.b64encode(output.content).decode(),
                    "content_type": output.headers.get("content-type")
                    or str(manifest.get("content_type") or "application/octet-stream"),
                    "filename": str(manifest.get("filename") or "output"),
                }
            )
        return {
            "execution_id": str(result.get("execution_id") or ""),
            "outputs": outputs,
        }


async def wait_for_backend(process: asyncio.subprocess.Process) -> None:
    deadline = time.monotonic() + BACKEND_STARTUP_TIMEOUT
    async with httpx.AsyncClient(base_url=BACKEND_BASE, timeout=5) as client:
        while time.monotonic() < deadline:
            return_code = process.returncode
            if return_code is not None:
                raise RuntimeError(
                    f"worker backend stopped during startup with status {return_code}"
                )
            try:
                response = await client.get("/health/ready")
                if response.is_success:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2)
    raise TimeoutError("worker backend did not become ready")


@asynccontextmanager
async def backend_lifecycle():
    BACKEND_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = await asyncio.to_thread(BACKEND_LOG.open, "ab", buffering=0)
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "command.cli",
            "serverless",
            stderr=asyncio.subprocess.STDOUT,
            stdout=log,
        )
        try:
            await wait_for_backend(process)
            yield
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 30)
                except TimeoutError:
                    process.kill()
                    await process.wait()
    finally:
        await asyncio.to_thread(log.close)


def build_worker():
    from vastai import (
        BenchmarkConfig,
        HandlerConfig,
        Worker,
        WorkerConfig,
    )

    return Worker(
        WorkerConfig(
            handlers=[
                HandlerConfig(
                    route="/internal/executions",
                    allow_parallel_requests=False,
                    benchmark_config=BenchmarkConfig(
                        concurrency=1,
                        do_warmup=False,
                        generator=lambda: {"healthcheck": True},
                        runs=1,
                    ),
                    max_queue_time=30,
                    remote_function=execute,
                )
            ],
            lifecycle=backend_lifecycle(),
            max_sessions=1,
            model_healthcheck_url="/health/ready",
            model_log_file=str(BACKEND_LOG),
            model_server_port=8000,
            model_server_url="http://127.0.0.1",
        )
    )


def main() -> None:
    os.environ.setdefault("WORKER_PORT", os.getenv("VAST_WORKER_PORT", "9000"))
    build_worker().run(host=os.getenv("VAST_WORKER_HOST", "0.0.0.0"))
