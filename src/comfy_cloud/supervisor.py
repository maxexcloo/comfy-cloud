from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def main() -> None:
    comfy_dir = os.getenv("COMFYUI_DIR", "/opt/ComfyUI")
    comfy_args = os.getenv("COMFYUI_ARGS", "--listen 127.0.0.1 --port 8188").split()
    comfy = subprocess.Popen([sys.executable, "main.py", *comfy_args], cwd=comfy_dir)
    gateway = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "comfy_cloud.app:create_app",
            "--factory",
            "--host",
            "0.0.0.0",
            "--port",
            os.getenv("PORT", "8000"),
        ]
    )

    def stop(*_: object) -> None:
        for process in (gateway, comfy):
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while True:
            if comfy.poll() is not None or gateway.poll() is not None:
                stop()
                raise SystemExit(comfy.returncode or gateway.returncode or 1)
            time.sleep(0.5)
    finally:
        stop()


if __name__ == "__main__":
    main()
