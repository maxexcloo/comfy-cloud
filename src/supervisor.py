from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml

from .config import DeploymentType
from .fetch import fetch_profile
from .worker_logs import capture_process_logs

MODEL_DIRECTORY_KEYS = ("checkpoints", "diffusion_models", "text_encoders", "vae")
SHUTDOWN_TIMEOUT = 10


def _prepare_models() -> None:
    profiles = [
        profile.strip()
        for profile in os.getenv("MODEL_PROFILES", "").split(",")
        if profile.strip()
    ]
    if not profiles:
        return
    models_dir = Path(os.getenv("MODELS_DIR", "/opt/ComfyUI/models"))
    profiles_dir = Path(os.getenv("PROFILES_DIR", "/opt/comfy-control/profiles"))
    for profile in profiles:
        if any(
            character not in "-0123456789_abcdefghijklmnopqrstuvwxyz"
            for character in profile
        ):
            raise ValueError(f"invalid model profile: {profile}")
        profile_path = profiles_dir / f"{profile}.yaml"
        if not profile_path.is_file():
            raise ValueError(f"unknown model profile: {profile}")
        print(f"Preparing model profile {profile}", flush=True)
        files = fetch_profile(profile_path, models_dir)
        print(f"Prepared model profile {profile} ({len(files)} files)", flush=True)


def _comfy_arguments(comfy_dir: Path) -> list[str]:
    arguments = shlex.split(os.getenv("COMFYUI_ARGS", "--listen 127.0.0.1 --port 8188"))
    models_dir = Path(os.getenv("MODELS_DIR", str(comfy_dir / "models")))
    if models_dir.resolve() == (comfy_dir / "models").resolve():
        return arguments
    config = {
        "comfy_control": {
            "base_path": str(models_dir),
            "is_default": True,
            **{key: key for key in MODEL_DIRECTORY_KEYS},
        }
    }
    config_path = Path(tempfile.gettempdir()) / "comfy-control-model-paths.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    return [*arguments, "--extra-model-paths-config", str(config_path)]


def _stop_processes(processes: tuple[subprocess.Popen, ...]) -> None:
    running = [process for process in processes if process.poll() is None]
    for process in running:
        process.terminate()
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT
    for process in running:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
    for process in running:
        process.wait()


def run(
    deployment_type: DeploymentType, host: str = "0.0.0.0", port: int = 8000
) -> None:
    comfy_dir = Path(os.getenv("COMFYUI_DIR", "/opt/ComfyUI"))
    _prepare_models()
    comfy = subprocess.Popen(
        [sys.executable, "main.py", *_comfy_arguments(comfy_dir)],
        bufsize=1,
        cwd=comfy_dir,
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE,
        text=True,
    )
    log_thread: threading.Thread = capture_process_logs(comfy, sys.stdout)
    gateway = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            f"comfy_control.worker:create_{deployment_type}_app",
            "--factory",
            "--host",
            host,
            "--port",
            str(port),
        ]
    )

    processes = (gateway, comfy)
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        _stop_processes(processes)

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
        log_thread.join(timeout=1)
